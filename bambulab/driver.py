"""FilaMan entry point and MQTT orchestration for the Bambu Lab plugin.

The Driver class intentionally keeps only lifecycle management, MQTT callbacks,
printer commands and FilaMan's public driver API. Slot parsing, spool database
synchronization and catalog enrichment live in focused sibling modules.
"""

import asyncio
import json
import logging
import threading
from concurrent.futures import Future, TimeoutError
from itertools import count
from typing import Any, Callable

from sqlalchemy import select

from app.core.database import async_session_maker
from app.models.printer import Printer, PrinterSlot
from app.plugins.base import BaseDriver

from .catalog import CatalogMixin
from .catalog_enrichment import CatalogEnrichmentMixin
from .slots import SlotSupportMixin
from .spool_sync import SpoolSyncMixin
from .state import PendingSpool

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 60
DEFAULT_RECONNECT_INTERVAL = 5
FILAMENT_SETTING_TIMEOUT = 5
_FILAMENT_SETTING_SEQUENCES = count(10000)


class Driver(
    CatalogEnrichmentMixin,
    CatalogMixin,
    SpoolSyncMixin,
    SlotSupportMixin,
    BaseDriver,
):
    """Connect a Bambu printer to FilaMan and coordinate plugin services."""

    driver_key = "bambulab"

    # Sending filament settings requires LAN-only and Developer Mode. Reading
    # MQTT state remains available without these state-changing capabilities.
    def __init__(
        self,
        printer_id: int,
        config: dict[str, Any],
        emitter: Callable[[dict[str, Any]], None],
    ):
        """Initialize configuration and all per-printer runtime state."""
        super().__init__(printer_id, config, emitter)
        self._printer: Any = None  # bambulabs_api.Printer
        self._pending: PendingSpool | None = None
        self._filament_acks: dict[str, Future[dict]] = {}
        self._filament_acks_lock = threading.Lock()
        self._timeout_seconds = (
            DEFAULT_TIMEOUT  # Can be overridden per assign_pending_spool call
        )
        self._host = config.get("host", "")
        self._serial = config.get("serial", "")
        self._access_code = config.get("access_code", "")
        self._read_only = bool(config.get("read_only", False))
        self._auto_import_spools = bool(config.get("auto_import_spools", False))
        self._sync_spool_weight = bool(config.get("sync_spool_weight", False))
        self._resolve_shop_images = bool(config.get("resolve_shop_images", False))
        self._connected = False
        self._reconnect_interval = (
            config.get("reconnect_interval_minutes", DEFAULT_RECONNECT_INTERVAL) * 60
        )
        self._current_slots: list[dict[str, Any]] = []
        self._saved_ams_slots: dict[str, str] = {}
        self._current_ams_units: list[dict[str, Any]] = []
        self._tray_exist_bits: int | None = None
        self._ams_units_awaiting_occupancy: set[int] = set()
        self._ams_state_presence: dict[str, bool] = {}
        self._confirmed_tray_uuids: dict[str, str] = {}
        self._needs_slot_sync = False
        self._printer_model = config.get("printer_model", "P1S")
        self._is_ams_lite = self._printer_model in ("A1", "A1_MINI")
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ams_serials: dict[str, str] = {}  # ams_id -> serial number
        self._printer_name: str | None = None  # Wird in start() aus DB geladen
        self._auto_import_lock: asyncio.Lock | None = None
        self._auto_import_last_attempt: dict[str, float] = {}
        self._shop_slot_last_scheduled: dict[str, float] = {}
        self._shop_page_cache: dict[str, tuple[float, dict[str, str]]] = {}
        self._store_search_cache: dict[
            str, tuple[float, dict[str, str] | None]
        ] = {}
        self._slot_display_metadata: dict[str, dict[str, Any]] = {}
        self._inventory_enrichment_registered = False
        self._spool_ids_by_tray_uuid: dict[str, int] = {}
        self._slot_spool_ids: dict[str, int] = {}
        self._explicit_slot_assignments: set[str] = set()
        self._slot_location_lock = asyncio.Lock()
        self._bambu_filaments = self._load_bambu_filaments()

    async def start(self) -> None:
        """Start MQTT connectivity and optional background services."""
        from bambulabs_api import Printer as BambuPrinter

        self._running = True
        self._loop = asyncio.get_running_loop()
        self._auto_import_lock = asyncio.Lock()

        # Load the display name before MQTT can deliver the first tray update.
        # Otherwise a fast push_status message may create a temporary
        # "Printer <id>" location before the real name is available.
        self._printer_name = await self._load_printer_name()
        self._saved_ams_slots = await self._load_saved_ams_slots()

        self._printer = BambuPrinter(
            ip_address=self._host,
            access_code=self._access_code,
            serial=self._serial,
        )

        # Callbacks laufen im paho-Thread
        self._printer.mqtt_client.on_connect_handler = self._on_connect
        self._printer.mqtt_client.on_disconnect_handler = self._on_disconnect
        # The library's unused status cache rejects flat printer acknowledgments.
        self._printer.mqtt_client._client.on_message = self._on_message

        # Reconnect-Backoff konfigurieren (paho auto-reconnect via loop_start)
        self._printer.mqtt_client._client.reconnect_delay_set(
            min_delay=1,
            max_delay=self._reconnect_interval,
        )

        # MQTT starten (non-blocking: connect_async + loop_start in paho-Thread)
        # pushall wird automatisch beim Connect gesendet (pushall_on_connect=True)
        self._printer.mqtt_start()
        logger.info(
            f"Bambu driver started for printer {self.printer_id} at {self._host}"
        )

        await self._ensure_shared_extra_fields()
        if self._resolve_shop_images:
            await self._register_inventory_enrichment()

    async def _load_printer_name(self) -> str:
        """Der Anzeigename des Druckers, sonst seine Id.

        Steht in einer eigenen Methode, damit der Name des Datenbankmodells hier
        nicht mit dem gleichnamigen Client aus bambulabs_api kollidieren kann.
        """
        fallback = f"Printer {self.printer_id}"
        try:
            async with async_session_maker() as db:
                printer = await db.get(Printer, self.printer_id)
                return printer.name if printer else fallback
        except Exception as e:
            logger.warning(f"Failed to load printer name: {e}")
            return fallback

    async def _load_saved_ams_slots(self) -> dict[str, str]:
        """Load persisted identities for topology retirement, not tray presence."""
        try:
            async with async_session_maker() as db:
                rows = await db.execute(
                    select(PrinterSlot.custom_fields, PrinterSlot.name)
                    .where(PrinterSlot.printer_id == self.printer_id, PrinterSlot.is_active.is_(True))
                    .order_by(PrinterSlot.slot_no)
                )
                slots = {}
                for fields, name in rows:
                    sid = fields.get("slot_index") if isinstance(fields, dict) else None
                    parsed = self._parse_slot_index(sid) if isinstance(sid, str) else None
                    if parsed and sid == f"{parsed[0]}-{parsed[1]}" and self._ams_tray_bit_index(*parsed) is not None:
                        slots[sid] = name or sid
                return slots
        except Exception:
            logger.exception("Failed to load saved AMS slots for printer %s", self.printer_id)
            return {}

    async def stop(self) -> None:
        """Stop background services, pending assignments and MQTT connectivity."""
        self._running = False
        if self._pending and self._pending.timer:
            self._pending.timer.cancel()
        self._pending = None
        await self._unregister_inventory_enrichment()
        if self._printer:
            try:
                self._printer.mqtt_client._client.disconnect()
            except Exception:
                pass
            self._printer.mqtt_stop()
            self._printer = None
        self._connected = False

    async def refresh_status(self) -> dict[str, Any]:
        """Reconcile catalog images when FilaMan requests an explicit refresh.

        FilaMan may run multiple web workers, while its event bus is local to a
        worker process. The explicit health refresh is proxied to the primary
        worker and therefore provides a reliable reconciliation path for
        inventory changes emitted by another plugin such as FilaScan.
        """
        if not self._running or not self._resolve_shop_images:
            return {}
        try:
            stats = await self._refresh_inventory_shop_images()
        except Exception as exc:
            # Driver health and the inventory gallery must remain available
            # when the printer or an external image service is offline.
            logger.warning("Bambu inventory image refresh failed: %s", exc)
            return {}
        logger.info(
            "Bambu inventory image refresh checked %s filaments; %s have images",
            stats["filaments"],
            stats["images"],
        )
        return {"catalog_images": stats}

    def _emit_slots_with_spool_ids(self) -> None:
        """Attach known FilaMan spool IDs and emit one complete slot snapshot."""
        updated_slots: list[dict[str, Any]] = []
        for current in self._current_slots:
            slot = dict(current)
            slot_index = str(slot.get("slot_index") or "")
            if not slot.get("present"):
                # FilaMan clears persisted assignments only for an explicit null.
                slot["spool_id"] = None
                self._slot_spool_ids.pop(slot_index, None)
                self._explicit_slot_assignments.discard(slot_index)
            elif slot_index in self._slot_spool_ids:
                slot["spool_id"] = self._slot_spool_ids[slot_index]
            updated_slots.append(slot)
        self._current_slots = updated_slots
        self.emit(
            {
                "event_type": "slots_update",
                "slots": updated_slots,
                "ams_info": self._build_ams_info(updated_slots),
            }
        )

    def _on_connect(self, mqtt_client, client, userdata, flags, rc, properties):
        """Wird im paho-Thread aufgerufen wenn MQTT verbunden ist."""
        self._connected = True
        if self._loop:
            self._loop.call_soon_threadsafe(self._begin_slot_sync)
        logger.info(
            f"Bambu driver connected to printer {self.printer_id} at {self._host}"
        )
        self.log_debug("event", "mqtt", {"event": "connected", "rc": str(rc)})

    def _begin_slot_sync(self) -> None:
        # Keep confirmed loads until a fresh mask or state can prove removal.
        occupied = {}
        for slot in self._current_slots:
            sid = slot["slot_index"]
            address = self._parse_slot_index(sid)
            if address and self._ams_reported_presence(*address) is True:
                occupied[sid] = True
        self._tray_exist_bits = None
        self._ams_state_presence = occupied
        self._needs_slot_sync = True

    def _on_disconnect(
        self, mqtt_client, client, userdata, disconnect_flags, rc, properties
    ):
        """Wird im paho-Thread aufgerufen wenn MQTT getrennt wird."""
        self._connected = False
        logger.warning(
            f"Bambu driver disconnected from printer {self.printer_id}: {rc}"
        )
        self.log_debug("event", "mqtt", {"event": "disconnected", "rc": str(rc)})
        # paho auto-reconnect via loop_start() (reconnect_on_failure=True)

    def _on_message(self, client, userdata, msg):
        """Wird im paho-Thread für jede MQTT-Nachricht aufgerufen."""
        try:
            payload = json.loads(msg.payload.decode())
            self.log_debug("in", str(msg.topic), payload)

            print_report = payload.get("print", {})
            report = payload if payload.get("command") == "ams_filament_setting" else print_report
            if (
                isinstance(report, dict)
                and report.get("command") == "ams_filament_setting"
                and report.get("result")
            ):
                with self._filament_acks_lock:
                    ack = self._filament_acks.pop(str(report.get("sequence_id")), None)
                if ack is not None:
                    ack.set_result(report)
                return

            # push_status Nachrichten verarbeiten
            if isinstance(print_report, dict) and print_report.get("command") == "push_status":
                if self._loop:
                    self._loop.call_soon_threadsafe(self._handle_slot_status, payload)
                return

            # get_version / push_info: AMS Seriennummern extrahieren
            info_cmd = payload.get("info", {}).get("command", "")
            if info_cmd in ("get_version", "push_info"):
                if self._loop:
                    self._loop.call_soon_threadsafe(self._process_version_info, payload)
                return

        except json.JSONDecodeError as e:
            logger.error(f"Failed to decode MQTT message: {e}")
        except Exception as e:
            logger.error(f"Error handling MQTT message: {e}")

    def _handle_slot_status(self, payload: dict) -> None:
        # Keep MQTT, pending assignments and RFID results on the same event loop.
        if self._running:
            try:
                self._process_slots(payload)
            except Exception:
                logger.exception("Error handling Bambu slot status")

    def _send_filament_setting(
        self, ams_id: int, tray_id: int, filament_data: dict
    ) -> bool:
        """Filament-Setting an Drucker senden. Läuft in separatem Thread,
        da wait_for_publish() blockiert."""
        if self._read_only:
            logger.info(
                f"Read-only mode: skipped filament setting for slot {ams_id}-{tray_id}"
            )
            return False
        threading.Thread(
            target=self._do_send_filament_setting,
            args=(ams_id, tray_id, filament_data),
            daemon=True,
        ).start()
        return True

    def _do_send_filament_setting(
        self, ams_id: int, tray_id: int, filament_data: dict
    ) -> None:
        """Publish the setting and wait briefly for the printer's reply."""
        if self._read_only:
            logger.warning(
                f"Read-only mode: blocked filament setting for slot {ams_id}-{tray_id}"
            )
            return
        if not self._printer:
            logger.error("Cannot send filament setting: not connected")
            return

        color = filament_data.get("color", "")
        if len(color) == 8:
            color = color[:6]  # Alpha-Kanal entfernen, bambulabs_api erwartet 6 Zeichen
        elif len(color) != 6:
            color = "FFFFFF"  # Default weiß

        sequence = str(next(_FILAMENT_SETTING_SEQUENCES))
        try:
            mqtt_ams_id, mqtt_tray_id = self._wire_tray_address(ams_id, tray_id)
            settings = {
                "command": "ams_filament_setting",
                "sequence_id": sequence,
                "ams_id": mqtt_ams_id,
                "tray_id": mqtt_tray_id,
                "tray_info_idx": filament_data.get("tray_info_idx", "GFL99"),
                "tray_color": f"{color.upper()}FF",
                "nozzle_temp_min": filament_data.get("nozzle_temp_min", 190),
                "nozzle_temp_max": filament_data.get("nozzle_temp_max", 230),
                "tray_type": filament_data.get("material_type", "PLA"),
            }
            if filament_data.get("setting_id"):
                settings["setting_id"] = filament_data["setting_id"]

            # bambulabs_api omits setting_id when serializing filament settings.
            mqtt = self._printer.mqtt_client
            if not mqtt._client.is_connected():
                logger.error("Cannot send filament setting: MQTT disconnected")
                return
            ack = Future()
            with self._filament_acks_lock:
                self._filament_acks[sequence] = ack
            command = mqtt._client.publish(mqtt.command_topic, json.dumps({"print": settings}))
            command.wait_for_publish(timeout=FILAMENT_SETTING_TIMEOUT)
            published = command.is_published()
            result, reason = "not_published", ""
            if published:
                try:
                    report = ack.result(timeout=FILAMENT_SETTING_TIMEOUT)
                    result, reason = report["result"], report.get("reason", "")
                except TimeoutError:
                    result = "timeout"

            success = str(result).lower() == "success"
            self.log_debug(
                "out",
                mqtt.command_topic,
                {
                    **settings, "mqtt_published": published, "printer_ack": result,
                    "printer_reason": reason, "success": success,
                },
            )
            (logger.info if success else logger.warning)(
                "Filament setting for printer %s slot %s-%s: %s, reason=%r",
                self.printer_id, ams_id, tray_id, result, reason,
            )
        except Exception as e:
            logger.error(f"Failed to send filament setting: {e}")
        finally:
            with self._filament_acks_lock:
                self._filament_acks.pop(sequence, None)

    async def reconnect(self) -> None:
        """Reconnect: MQTT stoppen und neu starten."""
        logger.info(f"Reconnecting Bambu driver for printer {self.printer_id}")
        if self._printer:
            try:
                self._printer.mqtt_client._client.disconnect()
            except Exception:
                pass
            self._printer.mqtt_stop()
            self._connected = False
            self._printer.mqtt_start()
            logger.info(f"Bambu driver reconnected for printer {self.printer_id}")

    async def send_filament_to_tray(
        self, ams_id: int, tray_id: int, filament_data: dict,
        spool_id: int | None = None,
    ) -> None:
        """Assign a FilaMan spool directly; read-only suppresses only MQTT writes."""
        ams_id, tray_id = self._wire_tray_address(ams_id, tray_id)
        filaman_spool_id = spool_id or filament_data.get("filaman_spool_id") or filament_data.get("id")
        if filaman_spool_id:
            filaman_spool_id = int(filaman_spool_id)
            slot_index = f"{ams_id}-{tray_id}"
            present = self._ams_reported_presence(ams_id, tray_id)
            if present is False:
                raise TypeError("Cannot assign a spool to an empty Bambu slot")
            occupied = present or any(
                slot["slot_index"] == slot_index and slot["present"]
                for slot in self._current_slots
            )
            if not occupied:
                if self._read_only:
                    raise TypeError(
                        "Cannot verify spool occupancy in this Bambu slot in read-only mode"
                    )
                self._arm_pending_spool(
                    PendingSpool(filaman_spool_id, filament_data, slot_index, send_setting=False),
                    self._timeout_seconds,
                )
            else:
                self._slot_spool_ids[slot_index] = filaman_spool_id
                self._explicit_slot_assignments.add(slot_index)
                await self._sync_slot_location(slot_index, filaman_spool_id)
                if self._slot_spool_ids.get(slot_index) != filaman_spool_id:
                    return
                self._emit_slots_with_spool_ids()
        self._send_filament_setting(ams_id, tray_id, filament_data)

    # -- Pending-Spool API ----------------------------------------------------

    async def assign_pending_spool(
        self,
        spool_id: int,
        filament_data: dict,
        slot_index: str | None = None,
        timeout_seconds: int | None = None,
        *,
        claim: Callable[[], bool] | None = None,
    ) -> None:
        """Spule für automatische Zuweisung vormerken.

        Read-only stops MQTT commands, not bookkeeping. The assignment itself
        is written to FilaMan's own database, so it is kept here and only the
        filament setting further down is suppressed - the same rule the RFID
        auto-import already follows.
        """
        if claim is not None and not callable(claim):
            raise TypeError("claim must be callable")
        if slot_index is not None:
            parsed_slot = self._parse_slot_index(slot_index)
            if parsed_slot:
                ams_id, tray_id = self._wire_tray_address(*parsed_slot)
                slot_index = f"{ams_id}-{tray_id}"
        pending = PendingSpool(spool_id, filament_data, slot_index, claim=claim)
        effective_timeout = (
            timeout_seconds if timeout_seconds is not None else self._timeout_seconds
        )
        self._arm_pending_spool(pending, effective_timeout)

    def _arm_pending_spool(self, pending: PendingSpool, timeout: int) -> None:
        if self._pending and self._pending.timer:
            self._pending.timer.cancel()
        pending.timer = asyncio.create_task(self._timeout_task(pending, timeout))
        self._pending = pending
        logger.info(
            "Pending spool %s for printer %s (slot: %s, timeout: %ss)",
            pending.spool_id, self.printer_id, pending.slot_index, timeout,
        )

    async def _timeout_task(self, pending: PendingSpool, timeout: int) -> None:
        """Expire only the pending request that started this timer."""
        await asyncio.sleep(timeout)
        if self._pending is pending:
            logger.info("Pending spool %s timed out", pending.spool_id)
            self._pending = None

    # -- Health ---------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """Return the current printer, AMS and optional catalog-image status."""
        ams_info = self._build_ams_info(self._current_slots)
        display_slots: list[dict[str, Any]] = []
        for current in self._current_slots:
            slot = dict(current)
            slot_index = str(slot.get("slot_index") or "")
            metadata = self._slot_display_metadata.get(slot_index)
            if metadata and metadata.get("_slot_identity") == self._slot_identity(slot):
                slot.update(
                    {
                        key: value
                        for key, value in metadata.items()
                        if not key.startswith("_")
                    }
                )
            display_slots.append(slot)
        return {
            "driver_key": self.driver_key,
            "printer_id": self.printer_id,
            "running": self._running,
            "connected": self._connected,
            "pending": self._pending is not None,
            "read_only": self._read_only,
            "auto_import_spools": self._auto_import_spools,
            "sync_spool_weight": self._sync_spool_weight,
            "resolve_shop_images": self._resolve_shop_images,
            "printer_model": self._printer_model,
            "ams_type": ams_info["ams_type"],
            "ams_count": ams_info["ams_count"],
            "slot_count": ams_info["slot_count"],
            "external_spool": ams_info["external_spool"],
            "ams_units": ams_info["ams_units"],
            "shop_image_count": sum(
                bool(slot.get("shop_image_url")) for slot in display_slots
            ),
            "slots": display_slots,
        }
