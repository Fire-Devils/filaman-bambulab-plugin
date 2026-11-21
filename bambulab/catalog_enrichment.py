"""Coordinate catalog enrichment across all running Bambu driver instances.

FilaMan's event bus is process-local. This module owns the shared listener,
debounces inventory events and keeps a periodic scan as a recovery path.
"""

import asyncio
import json
import logging
from typing import Any

from sqlalchemy import func, select

from app.core.database import async_session_maker
from app.core.event_bus import event_bus
from app.models.filament import Filament, Manufacturer
from app.models.spool import Spool

logger = logging.getLogger(__name__)

INVENTORY_IMAGE_SCAN_SECONDS = 6 * 60 * 60
INVENTORY_IMAGE_EVENT_DEBOUNCE_SECONDS = 0.5
INVENTORY_IMAGE_EVENTS = frozenset({"spools_changed", "filaments_changed"})
# Rate-limits concurrent lookups against the external Bambu shop API; the
# per-filament lock in CatalogMixin already keeps writes to one filament safe,
# so different filaments can resolve in parallel up to this bound.
INVENTORY_SCAN_CONCURRENCY = 4


class CatalogEnrichmentMixin:
    """Share one event listener and image-enrichment worker per process."""

    _inventory_enrichment_instances: set["CatalogEnrichmentMixin"] = set()
    _inventory_enrichment_event: asyncio.Event | None = None
    _inventory_enrichment_listener_task: asyncio.Task | None = None
    _inventory_enrichment_worker_task: asyncio.Task | None = None
    async def _refresh_inventory_shop_images(self) -> dict[str, int]:
        """Resolve images for every Bambu filament that has a physical spool."""
        async with async_session_maker() as db:
            result = await db.execute(
                select(Filament.id)
                .join(Manufacturer, Filament.manufacturer_id == Manufacturer.id)
                .join(Spool, Spool.filament_id == Filament.id)
                .where(func.lower(Manufacturer.name).in_(("bambu", "bambu lab")))
                .distinct()
                .order_by(Filament.id)
            )
            filament_ids = list(result.scalars().all())

        semaphore = asyncio.Semaphore(INVENTORY_SCAN_CONCURRENCY)

        async def _bounded(filament_id: int) -> dict[str, Any]:
            async with semaphore:
                # Abort once this driver has stopped (stop() sets _running
                # False before unregistering, see driver.py), so a departing
                # owner doesn't keep resolving the whole remaining list
                # against now-stale state. Deliberately not also checking
                # _inventory_enrichment_instances membership: refresh_status()
                # calls this directly on drivers that were never registered
                # as the shared scan's owner, and that path must keep working.
                if not self._running:
                    return {}
                return await self._cache_shop_image_for_filament(filament_id)

        results = await asyncio.gather(*(_bounded(fid) for fid in filament_ids))
        image_count = sum(bool(r.get("shop_image_url")) for r in results)
        return {"filaments": len(filament_ids), "images": image_count}

    @classmethod
    def _inventory_enrichment_owner(cls) -> "CatalogEnrichmentMixin | None":
        """Select one running driver to own the next shared inventory scan."""
        candidates = [
            driver
            for driver in cls._inventory_enrichment_instances
            if driver._running and driver._resolve_shop_images
        ]
        return min(candidates, key=lambda driver: driver.printer_id) if candidates else None

    async def _register_inventory_enrichment(self) -> None:
        """Register this driver and ensure the shared listener and worker run."""
        if self._inventory_enrichment_registered:
            return
        cls = type(self)
        cls._inventory_enrichment_instances.add(self)
        self._inventory_enrichment_registered = True

        listener_running = (
            cls._inventory_enrichment_listener_task is not None
            and not cls._inventory_enrichment_listener_task.done()
        )
        worker_running = (
            cls._inventory_enrichment_worker_task is not None
            and not cls._inventory_enrichment_worker_task.done()
        )
        if not listener_running or not worker_running:
            stale_tasks = []
            for task in (
                cls._inventory_enrichment_listener_task,
                cls._inventory_enrichment_worker_task,
            ):
                if task is not None and not task.done():
                    task.cancel()
                    stale_tasks.append(task)
            if stale_tasks:
                await asyncio.gather(*stale_tasks, return_exceptions=True)
            cls._inventory_enrichment_event = asyncio.Event()
            cls._inventory_enrichment_listener_task = asyncio.create_task(
                cls._inventory_enrichment_event_loop()
            )
            cls._inventory_enrichment_worker_task = asyncio.create_task(
                cls._inventory_enrichment_worker_loop()
            )

        # A newly started driver must reconcile inventory immediately, even if
        # relevant events were emitted while the Bambu integration was offline.
        if cls._inventory_enrichment_event is not None:
            cls._inventory_enrichment_event.set()

    async def _unregister_inventory_enrichment(self) -> None:
        """Remove this driver and stop shared tasks when the last owner exits."""
        if not self._inventory_enrichment_registered:
            return
        cls = type(self)
        cls._inventory_enrichment_instances.discard(self)
        self._inventory_enrichment_registered = False
        if cls._inventory_enrichment_instances:
            if cls._inventory_enrichment_event is not None:
                cls._inventory_enrichment_event.set()
            return

        tasks = [
            task
            for task in (
                cls._inventory_enrichment_listener_task,
                cls._inventory_enrichment_worker_task,
            )
            if task is not None
        ]
        cls._inventory_enrichment_listener_task = None
        cls._inventory_enrichment_worker_task = None
        cls._inventory_enrichment_event = None
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @classmethod
    async def _consume_inventory_events(cls, events) -> None:
        async for raw_event in events:
            if not cls._inventory_enrichment_instances:
                return
            try:
                event_name = json.loads(raw_event).get("event")
            except (json.JSONDecodeError, AttributeError, TypeError):
                continue
            if (
                event_name in INVENTORY_IMAGE_EVENTS
                and cls._inventory_enrichment_event is not None
            ):
                cls._inventory_enrichment_event.set()

    @classmethod
    async def _inventory_enrichment_event_loop(cls) -> None:
        """Wake the shared worker when any FilaMan source changes inventory."""
        try:
            subscription = event_bus.subscribe()
            if hasattr(subscription, "__aenter__"):
                # FilaMan >= 2.2.2: subscribe() is an async context manager.
                async with subscription as events:
                    await cls._consume_inventory_events(events)
            else:
                # Older FilaMan: subscribe() is an async generator.
                await cls._consume_inventory_events(subscription)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # The periodic worker remains a fallback if event subscription fails.
            logger.warning("Bambu inventory event listener failed: %s", exc)

    @classmethod
    async def _inventory_enrichment_worker_loop(cls) -> None:
        """Run one debounced image enrichment scan for all Bambu drivers."""
        while cls._inventory_enrichment_instances:
            event = cls._inventory_enrichment_event
            if event is None:
                return
            triggered_by_event = False
            try:
                await asyncio.wait_for(
                    event.wait(), timeout=INVENTORY_IMAGE_SCAN_SECONDS
                )
                triggered_by_event = True
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                raise

            event.clear()
            if triggered_by_event:
                await asyncio.sleep(INVENTORY_IMAGE_EVENT_DEBOUNCE_SECONDS)
                # Coalesce the paired spools/filaments events and any burst of
                # bulk-import notifications into this single reconciliation.
                event.clear()

            owner = cls._inventory_enrichment_owner()
            if owner is None:
                continue
            try:
                stats = await owner._refresh_inventory_shop_images()
                logger.info(
                    "Bambu inventory image scan checked %s filaments; %s have images",
                    stats["filaments"],
                    stats["images"],
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Bambu inventory image scan failed: %s", exc)
