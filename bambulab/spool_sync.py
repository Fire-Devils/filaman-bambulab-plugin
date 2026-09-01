"""Synchronize Bambu RFID trays with FilaMan spool records.

This module owns safe filament matching, idempotent spool upserts, estimated
weight updates, RFID extra fields and managed AMS-slot locations.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.attributes import flag_modified

from app.core.database import async_session_maker
from app.core.event_bus import event_bus
from app.models.filament import Color, Filament, FilamentColor, Manufacturer
from app.models.location import Location
from app.models.printer_params import FilamentPrinterParam
from app.models.spool import Spool, SpoolStatus
from app.models.system_extra_field import SystemExtraField
from app.services.spool_service import SpoolService

from .catalog import ARTICLE_NUMBER_FIELD, _evict_expired

logger = logging.getLogger(__name__)

AUTO_IMPORT_RETRY_SECONDS = 60
BAMBU_RFID_TAG_1_FIELD = "bambu_rfid_tag_1"
BAMBU_RFID_TAG_2_FIELD = "bambu_rfid_tag_2"
BAMBU_EXTERNAL_ID_PREFIX = "bambulab:"


class SpoolSyncMixin:
    """Provide RFID matching, database synchronization and location updates."""
    async def _ensure_shared_extra_fields(self) -> None:
        """Expose the filament article number and physical Bambu RFID tags."""
        field_defs = (
            (
                "filament",
                ARTICLE_NUMBER_FIELD,
                "Bambu Color Code (Article Number)",
                {"max_length": 5},
            ),
            (
                "spool",
                BAMBU_RFID_TAG_1_FIELD,
                "Bambu RFID Tag 1",
                {"max_length": 32},
            ),
            (
                "spool",
                BAMBU_RFID_TAG_2_FIELD,
                "Bambu RFID Tag 2",
                {"max_length": 32},
            ),
        )
        try:
            async with async_session_maker() as db:
                for target_type, key, label, config in field_defs:
                    result = await db.execute(
                        select(SystemExtraField).where(
                            SystemExtraField.target_type == target_type,
                            SystemExtraField.key == key,
                        )
                    )
                    field = result.scalar_one_or_none()
                    if field is None:
                        db.add(
                            SystemExtraField(
                                target_type=target_type,
                                key=key,
                                label=label,
                                field_type="text",
                                source=self.driver_key,
                                config=config,
                            )
                        )
                await db.commit()
        except Exception as e:
            logger.error(f"Failed to ensure shared Bambu extra fields: {e}")

    async def _find_matching_filament(self, db, slot: dict[str, Any]) -> Filament | None:
        """Find one existing filament; never create catalog data implicitly."""
        tray_info_idx = str(slot.get("tray_info_idx") or "").strip().upper()
        tray_index_candidate_ids: set[int] = set()
        if tray_info_idx:
            result = await db.execute(
                select(Filament)
                .join(
                    FilamentPrinterParam,
                    FilamentPrinterParam.filament_id == Filament.id,
                )
                .where(
                    FilamentPrinterParam.printer_id == self.printer_id,
                    FilamentPrinterParam.param_key == "bambu_tray_idx",
                    func.lower(FilamentPrinterParam.param_value)
                    == tray_info_idx.lower(),
                )
            )
            matches = list(result.scalars().unique().all())
            tray_index_candidate_ids = {item.id for item in matches}
            if len(matches) > 1:
                logger.warning(
                    f"Tray index {tray_info_idx} is not unique; continuing with "
                    "Bambu material/subtype/color matching"
                )

        tray_type = str(slot.get("tray_type") or "").strip()
        tray_color = self._normalize_tray_color(slot.get("tray_color"))
        if not tray_type or not tray_color:
            return None

        result = await db.execute(
            select(Filament)
            .join(Manufacturer, Filament.manufacturer_id == Manufacturer.id)
            .join(FilamentColor, FilamentColor.filament_id == Filament.id)
            .join(Color, Color.id == FilamentColor.color_id)
            .where(
                func.lower(Manufacturer.name).in_(("bambu", "bambu lab")),
                func.lower(Filament.material_type) == tray_type.lower(),
                func.lower(Color.hex_code) == f"#{tray_color}".lower(),
            )
        )
        candidates = list(result.scalars().unique().all())
        if tray_index_candidate_ids:
            indexed_candidates = [
                item for item in candidates if item.id in tray_index_candidate_ids
            ]
            selected = self._select_bambu_candidate(indexed_candidates, slot)
            if selected is not None:
                return selected
        mapped_name = self._bambu_filaments.get(tray_info_idx)
        if mapped_name:
            exact = [
                item
                for item in candidates
                if item.designation.strip().lower() == mapped_name.lower()
                or item.designation.strip().lower()
                == mapped_name.removeprefix("Bambu ").lower()
            ]
            if len(exact) == 1:
                return exact[0]
        selected = self._select_bambu_candidate(candidates, slot)
        if selected is None and candidates:
            logger.warning(
                "Bambu filament match remains ambiguous after subtype/color "
                f"filtering: tray_info_idx={tray_info_idx}, "
                f"tray_sub_brands={slot.get('tray_sub_brands', '')}, "
                f"candidate_ids={[item.id for item in candidates]}"
            )
        return selected

    @staticmethod
    def _spoolman_import_tags(custom_fields: Any) -> tuple[Any, ...]:
        """Possible tray UUIDs left by different Spoolman import versions.

        Depending on the age of the import it sits directly under ``tag`` or
        nested under ``spoolman_extra.tag``. Return both because one field may
        contain unrelated legacy data while the other carries the tray UUID.
        """
        if not isinstance(custom_fields, dict):
            return ()
        extra = custom_fields.get("spoolman_extra")
        return tuple(
            value
            for value in (
                custom_fields.get("tag"),
                extra.get("tag") if isinstance(extra, dict) else None,
            )
            if value
        )

    def _pick_oldest_match(
        self, matches: list[Spool], tray_uuid: str, carried_in: str
    ) -> Spool | None:
        """One spool out of the candidates, oldest first, with a word about it."""
        if not matches:
            return None
        if len(matches) > 1:
            logger.warning(
                "Multiple FilaMan spools carry Bambu tray_uuid=%s in %s; "
                "using the oldest spool id", tray_uuid, carried_in
            )
        spool = min(matches, key=lambda candidate: candidate.id)
        logger.info(
            "Matched existing FilaMan spool %s by %s for Bambu tray_uuid=%s",
            spool.id, carried_in, tray_uuid
        )
        return spool

    async def _find_existing_bambu_spool(
        self, db, external_id: str, tray_uuid: str
    ) -> Spool | None:
        """Find a spool already representing this tray, wherever it carries it."""
        result = await db.execute(
            select(Spool).where(Spool.external_id == external_id)
        )
        spool = result.scalar_one_or_none()
        if spool is not None:
            return spool

        # Some importers stored Bambu's logical tray UUID in FilaMan's built-in
        # RFID field before ``external_id`` became the canonical integration
        # identity. Normalize candidate values so case and common separators do
        # not cause the same physical spool to be imported a second time.
        result = await db.execute(
            select(Spool).where(Spool.rfid_uid.is_not(None))
        )
        spool = self._pick_oldest_match(
            [
                candidate
                for candidate in result.scalars().all()
                if self._normalize_hex_identifier(candidate.rfid_uid, 32)
                == tray_uuid
            ],
            tray_uuid,
            "rfid_uid",
        )
        if spool is not None:
            return spool

        # Spools that reached FilaMan through its Spoolman import keep the tray
        # uuid in their custom fields, where neither lookup above can see it.
        # Without this the import treats a spool FilaMan already has as unknown
        # and creates a second record for the same physical spool, which then
        # wins every later lookup through its own external_id.
        result = await db.execute(
            select(Spool).where(Spool.custom_fields.is_not(None))
        )
        return self._pick_oldest_match(
            [
                candidate
                for candidate in result.scalars().all()
                if any(
                    self._normalize_hex_identifier(value, 32) == tray_uuid
                    for value in self._spoolman_import_tags(
                        candidate.custom_fields
                    )
                )
            ],
            tray_uuid,
            "custom fields of the Spoolman import",
        )

    def _schedule_auto_import(self, slots: list[dict[str, Any]]) -> None:
        """Queue eligible RFID trays for a rate-limited asynchronous lookup.

        Runs whether or not spools may be created: recognising the spool in a
        tray is what fills the tray-uuid index, and that index is the only
        thing that puts a spool into a slot.
        """
        if not self._loop or not self._running:
            return

        now = time.monotonic()
        _evict_expired(self._auto_import_last_attempt, AUTO_IMPORT_RETRY_SECONDS, now)
        candidates: list[dict[str, Any]] = []
        for slot in slots:
            tray_uuid = self._normalize_hex_identifier(slot.get("tray_uuid"), 32)
            if not tray_uuid:
                continue
            last_attempt = self._auto_import_last_attempt.get(tray_uuid, 0)
            if now - last_attempt < AUTO_IMPORT_RETRY_SECONDS:
                continue
            self._auto_import_last_attempt[tray_uuid] = now
            candidates.append(dict(slot))

        if not candidates:
            return

        self._loop.call_soon_threadsafe(
            lambda: asyncio.create_task(self._auto_import_rfid_spools(candidates))
        )

    def _schedule_slot_location_release(self, slot_index: str) -> None:
        """Queue the location cleanup for a tray that just went empty."""
        parsed = self._parse_slot_index(slot_index)
        if not parsed or not self._loop:
            return
        ams_id, tray_id = parsed
        self._loop.call_soon_threadsafe(
            lambda: asyncio.create_task(
                self._release_slot_location(ams_id, tray_id)
            )
        )

    def _schedule_slot_location_update(
        self, slot_index: str, spool_id: int
    ) -> None:
        """Queue a known replacement spool for its slot location."""
        parsed = self._parse_slot_index(slot_index)
        if not parsed or not self._loop:
            return
        ams_id, tray_id = parsed
        self._loop.call_soon_threadsafe(
            lambda: asyncio.create_task(
                self._update_spool_location(
                    spool_id,
                    ams_id,
                    tray_id,
                )
            )
        )

    async def _auto_import_rfid_spools(
        self, slots: list[dict[str, Any]]
    ) -> None:
        """Recognise the spool in each RFID tray, and import it where allowed.

        Recognition always runs, because the tray-uuid index it fills is what
        assigns a spool to a slot. ``auto_import_spools`` decides only whether
        a tray FilaMan does not know may become a new spool, and whether a
        known one may be written to.
        """
        if not self._auto_import_lock:
            return

        changed = False
        async with self._auto_import_lock:
            for slot in slots:
                tray_uuid = self._normalize_hex_identifier(slot.get("tray_uuid"), 32)
                tag_uid = self._normalize_hex_identifier(slot.get("tag_uid"), 16)
                if not tray_uuid:
                    continue

                external_id = f"{BAMBU_EXTERNAL_ID_PREFIX}{tray_uuid}"
                spool: Spool | None = None
                created = False
                dirty = False
                weight_changed = False
                estimated_weight = (
                    self._estimated_remaining_weight(slot)
                    if self._sync_spool_weight
                    else None
                )

                async with async_session_maker() as db:
                    spool = await self._find_existing_bambu_spool(
                        db, external_id, tray_uuid
                    )

                    if spool is None:
                        if not self._auto_import_spools:
                            # Nothing known and nothing may be created, so this
                            # tray simply stays unassigned.
                            continue
                        filament = await self._find_matching_filament(db, slot)
                        if filament is None:
                            logger.warning(
                                "Bambu RFID spool not imported because no unique matching "
                                "FilaMan filament exists: "
                                f"tray_uuid={tray_uuid}, "
                                f"tray_info_idx={slot.get('tray_info_idx', '')}, "
                                f"tray_type={slot.get('tray_type', '')}, "
                                f"tray_color={slot.get('tray_color', '')}"
                            )
                            continue

                        status_result = await db.execute(
                            select(SpoolStatus).where(SpoolStatus.key == "opened")
                        )
                        status_obj = status_result.scalar_one_or_none()
                        if status_obj is None:
                            status_result = await db.execute(
                                select(SpoolStatus).where(SpoolStatus.key == "new")
                            )
                            status_obj = status_result.scalar_one_or_none()
                        if status_obj is None:
                            logger.error(
                                "Bambu RFID spool not imported: neither 'opened' nor "
                                "'new' spool status exists"
                            )
                            continue

                        custom_fields = {}
                        if tag_uid:
                            custom_fields[BAMBU_RFID_TAG_1_FIELD] = tag_uid
                        spool = Spool(
                            filament_id=filament.id,
                            status_id=status_obj.id,
                            external_id=external_id,
                            stocked_in_at=datetime.now(timezone.utc),
                            remaining_weight_g=estimated_weight,
                            custom_fields=custom_fields,
                        )
                        db.add(spool)
                        try:
                            await db.commit()
                            await db.refresh(spool)
                            created = True
                        except IntegrityError:
                            await db.rollback()
                            result = await db.execute(
                                select(Spool).where(Spool.external_id == external_id)
                            )
                            spool = result.scalar_one_or_none()
                            if spool is None:
                                raise
                    elif self._auto_import_spools:
                        custom_fields = dict(spool.custom_fields or {})
                        if tag_uid and not custom_fields.get(BAMBU_RFID_TAG_1_FIELD):
                            custom_fields[BAMBU_RFID_TAG_1_FIELD] = tag_uid
                            spool.custom_fields = custom_fields
                            flag_modified(spool, "custom_fields")
                            dirty = True
                        if not spool.external_id:
                            spool.external_id = external_id
                            dirty = True
                        if estimated_weight is not None and (
                            spool.remaining_weight_g is None
                            or abs(spool.remaining_weight_g - estimated_weight) >= 0.01
                        ):
                            previous_weight = spool.remaining_weight_g
                            spool.remaining_weight_g = estimated_weight
                            dirty = True
                            weight_changed = True
                            logger.info(
                                "Updated Bambu RFID spool %s estimated remaining "
                                "weight from %s g to %.1f g (tray_weight=%s, remain=%s%%)",
                                spool.id,
                                previous_weight,
                                estimated_weight,
                                slot.get("tray_weight"),
                                slot.get("remain"),
                            )
                        if dirty:
                            await db.commit()

                if spool is None:
                    continue

                tray_mapping_changed = (
                    self._spool_ids_by_tray_uuid.get(tray_uuid) != spool.id
                )
                self._spool_ids_by_tray_uuid[tray_uuid] = spool.id
                slot_index = str(slot.get("slot_index") or "")
                slot_mapping_changed = bool(slot_index) and (
                    self._slot_spool_ids.get(slot_index) != spool.id
                )
                if slot_index:
                    self._slot_spool_ids[slot_index] = spool.id
                parsed_slot = self._parse_slot_index(slot_index)
                if parsed_slot and slot_mapping_changed:
                    await self._update_spool_location(
                        spool.id, parsed_slot[0], parsed_slot[1]
                    )
                changed = (
                    changed
                    or created
                    or dirty
                    or tray_mapping_changed
                    or slot_mapping_changed
                )
                if created or dirty or tray_mapping_changed or slot_mapping_changed:
                    action = (
                        "Created"
                        if created
                        else "Updated"
                        if weight_changed
                        else "Matched"
                    )
                    logger.info(
                        f"{action} Bambu RFID spool {spool.id} "
                        f"for tray_uuid={tray_uuid} at slot {slot_index}"
                    )

            if changed:
                await event_bus.publish({"event": "spools_changed"})
                self._emit_slots_with_spool_ids()


    def _generate_slot_location_name(self, ams_id: int, tray_id: int) -> str:
        """Generiert Location-Namen für AMS-Slot.

        Format:
        - AMS Slots: "{Drucker Name} - AMS {A-D}{ams_id+1}"
        - External Slots: "{Drucker Name} - ext. Slot {tray_id+1}"

        Beispiele:
        - "Bambu P1S - AMS A2" (ams_id=0, tray_id=1)
        - "Bambu X1C - ext. Slot 1" (ams_id=255, tray_id=0)
        """
        printer_name = self._printer_name or f"Printer {self.printer_id}"

        if ams_id >= 200:  # Virtual/external slots use tray ids 254 and 255
            external_slot = tray_id - 253 if tray_id >= 254 else tray_id + 1
            return f"{printer_name} - ext. Slot {external_slot}"
        elif ams_id >= 128:  # AMS HT units are numbered from protocol id 128
            return f"{printer_name} - AMS HT {ams_id - 127} - Slot {tray_id + 1}"
        else:
            # AMS slots: A1, B1, C1, D1, A2, B2, ... (tray_id 0-3)
            slot_label = chr(65 + tray_id)  # 65 = 'A' in ASCII
            return f"{printer_name} - AMS {slot_label}{ams_id + 1}"

    def _generate_slot_location_identifier(self, ams_id: int, tray_id: int) -> str:
        """Return the stable database identity for one physical printer slot."""
        return f"bambulab_{self.printer_id}_{ams_id}_{tray_id}"

    def _generate_legacy_slot_location_name(self, ams_id: int, tray_id: int) -> str:
        """Return the fallback name used before printer names loaded correctly."""
        current_printer_name = self._printer_name or f"Printer {self.printer_id}"
        current_name = self._generate_slot_location_name(ams_id, tray_id)
        prefix = f"{current_printer_name} - "
        suffix = current_name.removeprefix(prefix)
        return f"Printer {self.printer_id} - {suffix}"

    def _adopt_legacy_slot_location(
        self, location: Location, identifier: str
    ) -> Location:
        """Attach stable identity and current ownership metadata to a legacy row."""
        location.identifier = identifier
        self._refresh_slot_location_metadata(location)
        logger.info(
            "Adopted legacy Bambu slot location '%s' as %s",
            location.name,
            identifier,
        )
        return location

    def _refresh_slot_location_metadata(self, location: Location) -> None:
        """Ensure a managed slot records its current plugin ownership."""
        custom_fields = (
            dict(location.custom_fields)
            if isinstance(location.custom_fields, dict)
            else {}
        )
        custom_fields.update(
            {
                "managed_by": "bambulab_plugin",
                "printer_id": self.printer_id,
            }
        )
        location.custom_fields = custom_fields

    async def _find_slot_location(
        self, db, ams_id: int, tray_id: int
    ) -> Location | None:
        """Find the stable slot or conservatively adopt an owned legacy row."""
        identifier = self._generate_slot_location_identifier(ams_id, tray_id)
        result = await db.execute(
            select(Location).where(Location.identifier == identifier)
        )
        location = result.scalar_one_or_none()
        if location is not None:
            self._refresh_slot_location_metadata(location)
            return location

        # Current plugin versions always set an identifier. Only identifier-less
        # rows can be legacy candidates; a manual or foreign location with its
        # own identity must never be taken over merely because its name matches.
        result = await db.execute(
            select(Location).where(
                or_(Location.identifier.is_(None), Location.identifier == "")
            )
        )
        candidates = list(result.scalars().all())
        legacy_name = self._generate_legacy_slot_location_name(ams_id, tray_id)
        slot_suffix = legacy_name.split(" - ", 1)[-1]
        managed_candidates = []
        for candidate in candidates:
            custom_fields = candidate.custom_fields or {}
            if not isinstance(custom_fields, dict):
                continue
            if custom_fields.get("managed_by") != "bambulab_plugin":
                continue
            if str(custom_fields.get("printer_id")) != str(self.printer_id):
                continue
            if not str(candidate.name or "").casefold().endswith(
                f" - {slot_suffix}".casefold()
            ):
                continue
            managed_candidates.append(candidate)

        if len(managed_candidates) == 1:
            return self._adopt_legacy_slot_location(
                managed_candidates[0], identifier
            )
        if len(managed_candidates) > 1:
            logger.warning(
                "Multiple identifier-less Bambu locations claim printer %s; "
                "not adopting any for slot %s-%s",
                self.printer_id,
                ams_id,
                tray_id,
            )
            return None

        name_candidates = [
            candidate
            for candidate in candidates
            if str(candidate.name or "").casefold() == legacy_name.casefold()
        ]
        if len(name_candidates) == 1:
            return self._adopt_legacy_slot_location(name_candidates[0], identifier)
        if len(name_candidates) > 1:
            logger.warning(
                "Multiple locations match legacy Bambu slot name '%s'; "
                "not adopting any",
                legacy_name,
            )
        return None

    async def _unique_slot_location_name(
        self,
        db,
        preferred_name: str,
        identifier: str,
    ) -> str:
        """Keep names readable while separating equally named printers."""
        result = await db.execute(select(Location))
        locations = list(result.scalars().all())

        def name_is_used_elsewhere(candidate_name: str) -> bool:
            return any(
                str(location.name or "").casefold() == candidate_name.casefold()
                and location.identifier != identifier
                for location in locations
            )

        if not name_is_used_elsewhere(preferred_name):
            return preferred_name

        collision_name = f"{preferred_name} [Printer {self.printer_id}]"
        suffix = 2
        while name_is_used_elsewhere(collision_name):
            collision_name = (
                f"{preferred_name} [Printer {self.printer_id} #{suffix}]"
            )
            suffix += 1
        return collision_name

    async def _clear_slot_location(
        self, db, location: Location, location_name: str, keep_spool_id: int | None
    ) -> int:
        """Nimmt den Lagerort von jeder Spule, die nicht mehr darin liegt."""
        result = await db.execute(
            select(Spool).where(Spool.location_id == location.id)
        )
        stale = [
            spool
            for spool in result.scalars().all()
            if spool.id != keep_spool_id
        ]
        if not stale:
            return 0
        service = SpoolService(db)
        for spool in stale:
            await service.move_location(
                spool,
                None,
                datetime.now(timezone.utc),
                source="driver",
                note=f"No longer in {location_name}",
            )
            logger.info(
                "Spool %s left location '%s'", spool.id, location_name
            )
        return len(stale)

    async def _release_slot_location(self, ams_id: int, tray_id: int) -> None:
        """Räumt den Lagerort eines Trays, aus dem die Spule gezogen wurde.

        Ein leeres Tray sagt nichts mehr darüber aus, wo die Spule liegt. Ohne
        dieses Aufräumen behauptet FilaMan weiterhin, sie sei im AMS, und der
        Lagerort sammelt über die Zeit jede Spule, die je darin lag.
        """
        slot_location_name = self._generate_slot_location_name(ams_id, tray_id)
        try:
            async with async_session_maker() as db:
                location = await self._find_slot_location(db, ams_id, tray_id)
                if location is None:
                    return
                await self._clear_slot_location(
                    db,
                    location,
                    location.name or slot_location_name,
                    keep_spool_id=None,
                )
                # Also persists a conservative legacy-location adoption even if
                # the already-empty location contained no spool to release.
                await db.commit()
        except Exception as e:
            logger.error(
                f"Failed to release location for slot {ams_id}-{tray_id}: {e}",
                exc_info=True,
            )

    async def _update_spool_location(
        self, filaman_spool_id: int, ams_id: int, tray_id: int
    ) -> None:
        """Setzt Spulen-Standort auf AMS-Slot-Location.

        Erstellt die Location automatisch falls sie noch nicht existiert.
        Nutzt SpoolService.move_location() für konsistente Event-Generierung.
        """
        try:
            preferred_name = self._generate_slot_location_name(ams_id, tray_id)
            identifier = self._generate_slot_location_identifier(ams_id, tray_id)

            async with async_session_maker() as db:
                # The identifier represents the physical slot. Its display name
                # may change when the printer is renamed and is not an identity.
                location = await self._find_slot_location(db, ams_id, tray_id)
                slot_location_name = await self._unique_slot_location_name(
                    db,
                    preferred_name,
                    identifier,
                )

                # 2. Location erstellen falls nicht vorhanden
                if not location:
                    location = Location(
                        name=slot_location_name,
                        identifier=identifier,
                        custom_fields={
                            "managed_by": "bambulab_plugin",
                            "printer_id": self.printer_id,
                        },
                    )
                    db.add(location)
                    await db.flush()  # Für location.id
                    logger.info(f"Created location: {slot_location_name}")
                elif location.name != slot_location_name:
                    logger.info(
                        "Renamed managed location '%s' to '%s'",
                        location.name,
                        slot_location_name,
                    )
                    location.name = slot_location_name

                # 3. Spule zur Location bewegen (wenn nicht bereits dort)
                spool = await db.get(Spool, filaman_spool_id)
                if not spool:
                    logger.warning(
                        f"Spool {filaman_spool_id} not found, cannot update location"
                    )
                    return

                # Ein Tray hält eine Spule. Wer sonst noch auf diesem Lagerort
                # steht, lag früher einmal darin und liegt längst woanders.
                await self._clear_slot_location(
                    db, location, slot_location_name, keep_spool_id=filaman_spool_id
                )

                if spool.location_id == location.id:
                    logger.debug(
                        f"Spool {filaman_spool_id} already at location '{slot_location_name}'"
                    )
                    await db.commit()
                    return

                # SpoolService für konsistente Event-Generierung nutzen
                await SpoolService(db).move_location(
                    spool,
                    location.id,
                    datetime.now(timezone.utc),
                    source="driver",
                    note=f"Assigned to {slot_location_name}",
                )

                # Einmaliger commit für beide Operationen (Location + Move)
                await db.commit()

                logger.info(
                    f"Moved spool {filaman_spool_id} to location '{slot_location_name}' "
                    f"(location_id={location.id})"
                )

        except Exception as e:
            logger.error(
                f"Failed to update location for spool {filaman_spool_id} "
                f"(slot {ams_id}-{tray_id}): {e}",
                exc_info=True,
            )
