"""Parse Bambu MQTT payloads and normalize AMS slot metadata.

The helpers in this module avoid database and network access. The mixin turns
partial Bambu status messages into a stable slot snapshot for the main driver.
"""

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any

from app.models.filament import Filament

logger = logging.getLogger(__name__)

_HEX_IDENTIFIER_RE = re.compile(r"^[0-9A-F]+$")
_BAMBU_PRODUCT_CODE_RE = re.compile(r"(?<!\d)(\d{5})(?!\d)")
_BAMBU_SUBTYPE_ALIASES = (
    ("silk-plus", "silk-plus"),
    ("tough-plus", "tough-plus"),
    ("pla-aero", "aero"),
    ("petg-hf", "hf"),
    ("high-flow", "hf"),
    ("matte", "matte"),
    ("basic", "basic"),
    ("silk", "silk"),
    ("aero", "aero"),
    ("tough", "tough"),
    ("support", "support"),
    ("metal", "metal"),
    ("wood", "wood"),
    ("lite", "lite"),
    ("hf", "hf"),
    ("cf", "cf"),
    ("gf", "gf"),
)


class SlotSupportMixin:
    """Pure slot helpers plus MQTT AMS payload processing."""

    @staticmethod
    def _normalize_hex_identifier(value: Any, expected_length: int) -> str | None:
        """Normalize a Bambu identifier and reject empty/all-zero placeholders."""
        if value is None:
            return None
        normalized = re.sub(r"[^0-9A-Fa-f]", "", str(value)).upper()
        if (
            len(normalized) != expected_length
            or not _HEX_IDENTIFIER_RE.fullmatch(normalized)
            or set(normalized) == {"0"}
        ):
            return None
        return normalized

    @staticmethod
    def _normalize_tray_color(value: Any) -> str | None:
        """Normalize an RGB/RGBA tray color to six uppercase RGB digits."""
        normalized = re.sub(r"[^0-9A-Fa-f]", "", str(value or "")).upper()
        if len(normalized) not in (6, 8):
            return None
        return normalized[:6]

    @staticmethod
    def _normalize_product_text(value: Any) -> str:
        """Normalize product text for deterministic subtype comparisons."""
        text = str(value or "").strip().lower().replace("+", "-plus")
        return re.sub(r"[^a-z0-9]+", "-", text).strip("-")

    @classmethod
    def _bambu_subtype(cls, value: Any) -> str | None:
        """Map Bambu product wording to a stable material subtype token."""
        normalized = cls._normalize_product_text(value)
        if not normalized:
            return None
        for token, subtype in _BAMBU_SUBTYPE_ALIASES:
            if token in normalized:
                return subtype
        return None

    @staticmethod
    def _filamentdb_id(filament: Filament) -> int | None:
        """Return a numeric FilamentDB identity when the record provides one."""
        custom_fields = filament.custom_fields or {}
        try:
            return int(custom_fields.get("filamentdb_id"))
        except (TypeError, ValueError):
            return None

    @classmethod
    def _canonical_candidate(cls, candidates: list[Filament]) -> Filament:
        """Choose deterministically among records known to describe one product."""
        return min(
            candidates,
            key=lambda item: (
                cls._filamentdb_id(item) is None,
                cls._filamentdb_id(item) or item.id,
                item.id,
            ),
        )

    @classmethod
    def _select_bambu_candidate(
        cls, candidates: list[Filament], slot: dict[str, Any]
    ) -> Filament | None:
        """Resolve duplicate FilamentDB records without guessing across products."""
        if len(candidates) <= 1:
            return candidates[0] if candidates else None

        slot_subtype = cls._bambu_subtype(slot.get("tray_sub_brands"))
        if slot_subtype:
            subtype_matches = [
                candidate
                for candidate in candidates
                if cls._bambu_subtype(candidate.material_subgroup) == slot_subtype
            ]
            if subtype_matches:
                candidates = subtype_matches
                if len(candidates) == 1:
                    return candidates[0]

        # FilamentDB currently contains both legacy, product-code-bearing Bambu
        # records and newer duplicates without the code.  Prefer the coded record
        # only when every coded candidate points to the same Bambu product.
        candidates_by_code: dict[str, list[Filament]] = {}
        for candidate in candidates:
            searchable = " ".join(
                filter(
                    None,
                    (candidate.designation, candidate.manufacturer_color_name),
                )
            )
            for code in set(_BAMBU_PRODUCT_CODE_RE.findall(searchable)):
                candidates_by_code.setdefault(code, []).append(candidate)
        if len(candidates_by_code) == 1:
            return cls._canonical_candidate(next(iter(candidates_by_code.values())))

        # Exact duplicate names without a product code are also safe to collapse.
        identities = {
            (
                cls._normalize_product_text(candidate.designation),
                cls._normalize_product_text(candidate.manufacturer_color_name),
                cls._bambu_subtype(candidate.material_subgroup),
            )
            for candidate in candidates
        }
        if len(identities) == 1:
            return cls._canonical_candidate(candidates)
        return None

    @staticmethod
    def _parse_slot_index(slot_index: str) -> tuple[int, int] | None:
        """Parse FilaMan's ``ams_id-tray_id`` slot notation."""
        try:
            ams_id, tray_id = slot_index.split("-", 1)
            return int(ams_id), int(tray_id)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_positive_float(value: Any) -> float | None:
        """Parse a strictly positive numeric value or return ``None``."""
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    def _parse_percentage(value: Any) -> float | None:
        """Parse an inclusive percentage between zero and one hundred."""
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if 0 <= parsed <= 100 else None

    @classmethod
    def _estimated_remaining_weight(cls, slot: dict[str, Any]) -> float | None:
        """Calculate remaining grams from nominal tray weight and percentage."""
        tray_weight = cls._parse_positive_float(slot.get("tray_weight"))
        remain = cls._parse_percentage(slot.get("remain"))
        if tray_weight is None or remain is None:
            return None
        return tray_weight * remain / 100

    @staticmethod
    def _load_bambu_filaments() -> dict[str, str]:
        """Load the bundled tray-index-to-filament-name mapping."""
        try:
            path = Path(__file__).with_name("bambu_filaments.json")
            data = json.loads(path.read_text(encoding="utf-8"))
            return {
                str(key).upper(): str(value)
                for key, value in data.items()
                if not str(key).startswith("_")
            }
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"Could not load Bambu filament mapping: {e}")
            return {}

    def _build_ams_info(self, slots: list[dict[str, Any]]) -> dict[str, Any]:
        """Build the compact AMS summary persisted and exposed by FilaMan."""
        external_slots = [
            slot
            for slot in slots
            if str(slot.get("slot_index") or "").startswith("255-")
        ]
        has_external = bool(external_slots)
        total_slots = sum(
            unit.get("tray_count", 0) for unit in self._current_ams_units
        )
        total_slots += len(external_slots)
        return {
            "ams_count": len(self._current_ams_units),
            "ams_type": "AMS Lite" if self._is_ams_lite else "AMS",
            "slot_count": total_slots,
            "external_spool": has_external,
            "ams_units": self._current_ams_units,
        }


    def _process_version_info(self, payload: dict) -> None:
        """AMS Seriennummern aus get_version/push_info Antwort extrahieren."""
        modules = payload.get("info", {}).get("module", [])
        for module in modules:
            name = module.get("name", "")
            if name.startswith("ams/"):
                ams_id = name.split("/")[1]
                sn = module.get("sn", "")
                if sn:
                    self._ams_serials[ams_id] = sn
                    logger.debug(f"AMS {ams_id} serial: {sn}")

    # -- Slot-Verarbeitung (paho-Thread) --------------------------------------

    def _process_slots(self, payload: dict) -> None:
        """AMS/Tray-Daten aus push_status extrahieren und slots_update emittieren.
        Wird bei jeder push_status Nachricht aufgerufen. Emittiert nur wenn sich
        die Slot-Daten geändert haben, um unnötige DB-Writes zu vermeiden.

        Merge-Strategie: Nur Slot-Kategorien aktualisieren, die in der aktuellen
        Nachricht vorhanden sind. Fehlende Kategorien behalten ihren vorherigen Zustand.
        BambuLab sendet nicht immer alle Daten in jeder push_status Nachricht.

        Auto-assignment nutzt ausschließlich Feld-Vergleich (wie C++ Referenz):
        Erkennt Änderungen in tray_info_idx, tray_type, tray_color, cali_idx, setting_id."""

        print_data = payload.get("print", {})
        ams_section = print_data.get("ams")
        vt_tray = print_data.get("vt_tray")
        vir_slot = print_data.get("vir_slot")

        # vir_slot is used by newer printers for one or more external trays.
        if ams_section is None and vt_tray is None and vir_slot is None:
            return

        ams_data = (ams_section or {}).get("ams", [])

        # Leichtgewichtige Nachricht (nur tray_now/version) — keine Slot-Daten vorhanden
        if not ams_data and vt_tray is None and vir_slot is None:
            return

        # -- Merge-Strategie: vorherige Slots als Basis, nur vorhandene Daten aktualisieren --
        # BambuLab sendet nicht immer ams UND vt_tray in jeder push_status Nachricht.
        # Ohne Merge würde das Fehlen einer Kategorie deren Slots löschen (Flicker).
        prev_ams_slots = [
            s
            for s in self._current_slots
            if not s.get("slot_index", "").startswith("255-")
        ]
        prev_ext_slots = [
            s for s in self._current_slots if s.get("slot_index", "").startswith("255-")
        ]

        # AMS-Einheiten Metadaten — nur aktualisieren wenn AMS-Daten vorhanden
        if ams_data:
            ams_units: list[dict[str, Any]] = []
            for ams_unit in ams_data:
                ams_id = int(ams_unit.get("id", 0))
                ams_units.append(
                    {
                        "ams_id": ams_id,
                        "humidity": ams_unit.get(
                            "humidity_raw", ams_unit.get("humidity")
                        ),
                        "temp": ams_unit.get("temp"),
                        "tray_count": len(ams_unit.get("tray", [])),
                        "serial": self._ams_serials.get(str(ams_id), None),
                    }
                )
            self._current_ams_units = ams_units
        else:
            ams_units = list(self._current_ams_units)

        # AMS-Trays: nur aktualisieren wenn ams_data vorhanden, sonst vorherige beibehalten
        if ams_data:
            ams_slots: list[dict[str, Any]] = []
            for ams_unit in ams_data:
                ams_id = int(ams_unit.get("id", 0))
                trays = ams_unit.get("tray", [])

                for tray in trays:
                    tray_id = int(tray.get("id", 0))
                    slot_index = f"{ams_id}-{tray_id}"
                    tray_type = tray.get("tray_type", "")

                    if ams_id >= 128:
                        slot_name = (
                            f"AMS HT {ams_id - 127} - Slot {tray_id + 1}"
                        )
                    elif self._is_ams_lite:
                        slot_name = f"AMS Lite - Slot {tray_id + 1}"
                    else:
                        slot_name = f"AMS {ams_id + 1} - Slot {tray_id + 1}"

                    present = bool(tray_type)
                    ams_slots.append(
                        {
                            "slot_index": slot_index,
                            "slot_name": slot_name,
                            "tray_info_idx": tray.get("tray_info_idx", ""),
                            "tray_type": tray_type,
                            "tray_color": tray.get("tray_color", ""),
                            "nozzle_temp_min": tray.get("nozzle_temp_min"),
                            "nozzle_temp_max": tray.get("nozzle_temp_max"),
                            "setting_id": tray.get("setting_id", ""),
                            "cali_idx": tray.get("cali_idx"),
                            "tag_uid": tray.get("tag_uid", ""),
                            "tray_uuid": tray.get("tray_uuid", ""),
                            "tray_sub_brands": tray.get("tray_sub_brands", ""),
                            "tray_weight": tray.get("tray_weight"),
                            "remain": tray.get("remain"),
                            "present": present,
                        }
                    )
        else:
            ams_slots = prev_ams_slots

        # Newer printer generations use vir_slot (list); older ones use vt_tray.
        # An empty/falsy vir_slot (e.g. []) falls through to vt_tray, and from
        # there to prev_ext_slots below, instead of hard-clearing external slots.
        external_trays: list[dict[str, Any]] | None = None
        if vir_slot:
            if isinstance(vir_slot, list):
                external_trays = [item for item in vir_slot if isinstance(item, dict)]
            elif isinstance(vir_slot, dict):
                external_trays = [vir_slot]
        elif isinstance(vt_tray, dict):
            external_trays = [vt_tray]

        if external_trays is not None:
            ext_slots = []
            multiple_external = len(external_trays) > 1
            for external_index, tray in enumerate(external_trays):
                try:
                    tray_id = int(tray.get("id", 254 + external_index))
                except (TypeError, ValueError):
                    tray_id = 254 + external_index
                external_number = (
                    tray_id - 253 if tray_id >= 254 else external_index + 1
                )
                ext_slots.append(
                    {
                        "slot_index": f"255-{tray_id}",
                        "slot_name": (
                            f"External Tray {external_number}"
                            if multiple_external
                            else "External Tray"
                        ),
                        "tray_info_idx": tray.get("tray_info_idx", ""),
                        "tray_type": tray.get("tray_type", ""),
                        "tray_color": tray.get("tray_color", ""),
                        "nozzle_temp_min": tray.get("nozzle_temp_min"),
                        "nozzle_temp_max": tray.get("nozzle_temp_max"),
                        "setting_id": tray.get("setting_id", ""),
                        "cali_idx": tray.get("cali_idx"),
                        "tag_uid": tray.get("tag_uid", ""),
                        "tray_uuid": tray.get("tray_uuid", ""),
                        "tray_sub_brands": tray.get("tray_sub_brands", ""),
                        "tray_weight": tray.get("tray_weight"),
                        "remain": tray.get("remain"),
                        "present": bool(tray.get("tray_type")),
                    }
                )
        else:
            ext_slots = prev_ext_slots

        # Zusammenführen: AMS-Slots + External Slot
        slots = ams_slots + ext_slots
        for slot in slots:
            slot_index = str(slot.get("slot_index") or "")
            previous = next(
                (
                    known
                    for known in self._current_slots
                    if str(known.get("slot_index") or "") == slot_index
                ),
                None,
            )
            previous_uuid = self._normalize_hex_identifier(
                previous.get("tray_uuid") if previous else None, 32
            )
            tray_uuid = self._normalize_hex_identifier(slot.get("tray_uuid"), 32)
            spool_was_replaced = bool(
                previous is not None
                and previous.get("present")
                and slot.get("present")
                and previous_uuid
                and tray_uuid
                and previous_uuid != tray_uuid
            )

            if spool_was_replaced:
                # The previous slot mapping must never leak into image lookup or
                # FilaMan's PrinterSlotAssignment for the new tray contents.
                self._slot_spool_ids.pop(slot_index, None)
                self._slot_display_metadata.pop(slot_index, None)
                slot["spool_id"] = None

            known_spool_id: int | None = None
            if tray_uuid and tray_uuid in self._spool_ids_by_tray_uuid:
                known_spool_id = self._spool_ids_by_tray_uuid[tray_uuid]
                slot["spool_id"] = known_spool_id
                self._slot_spool_ids[slot_index] = known_spool_id
            elif not slot.get("present") and slot_index in self._slot_spool_ids:
                slot["spool_id"] = None
                self._slot_spool_ids.pop(slot_index, None)

            if (
                previous is not None
                and previous.get("present")
                and not slot.get("present")
            ):
                self._schedule_slot_location_release(slot_index)
            elif spool_was_replaced:
                if known_spool_id is not None:
                    self._schedule_slot_location_update(
                        slot_index,
                        known_spool_id,
                    )
                else:
                    self._schedule_slot_location_release(slot_index)

        # -- Auto-assignment: Tray-Daten-Vergleich (wie C++ Implementierung) --
        # Erkennt wenn sich Tray-Felder ändern (Spule eingelegt/gewechselt).
        # Vergleicht tray_info_idx, tray_type, tray_color, cali_idx und setting_id
        # gegen die zuletzt gespeicherten Slot-Daten.
        # Beibehaltene (unveränderte) Slots matchen ihre Vorgänger → kein false positive.
        if self._pending and self._current_slots:
            _compare_fields = ("tray_info_idx", "tray_type", "tray_color", "cali_idx")
            for new_slot in slots:
                sid = new_slot.get("slot_index", "")
                new_tray_type = new_slot.get("tray_type", "")
                if not new_tray_type:
                    continue  # Leerer Slot, kein Assignment möglich
                # Passendes altes Slot finden
                old_slot = next(
                    (s for s in self._current_slots if s.get("slot_index") == sid), None
                )
                if old_slot is None:
                    continue  # Kein Vergleich möglich (erster Sync)
                # Wenn alter Slot leer war (tray_type war leer), setting_id zurücksetzen (wie C++)
                if not old_slot.get("tray_type", ""):
                    old_slot["setting_id"] = ""
                # setting_id null → leerer String (wie C++: if (trayObj["setting_id"].isNull()) trayObj["setting_id"] = "")
                new_setting_id = new_slot.get("setting_id") or ""
                old_setting_id = old_slot.get("setting_id") or ""
                # Prüfe ob sich relevante Felder geändert haben
                has_changed = any(
                    new_slot.get(f, "") != old_slot.get(f, "") for f in _compare_fields
                )
                # setting_id: nur vergleichen wenn neuer Wert nicht leer ist (wie C++)
                if (
                    not has_changed
                    and new_setting_id
                    and new_setting_id != old_setting_id
                ):
                    has_changed = True
                if not has_changed:
                    continue
                # Slot-Filter: wenn Pending einen bestimmten Slot will
                if (
                    self._pending.slot_index is not None
                    and self._pending.slot_index != sid
                ):
                    continue
                # Parse ams_id und tray_id aus slot_index (z.B. "0-1" oder "255-254")
                try:
                    parts = sid.split("-")
                    ams_id_parsed, tray_id_parsed = int(parts[0]), int(parts[1])
                except (ValueError, IndexError):
                    continue
                logger.info(
                    f"Tray data changed at slot {sid}: "
                    f"assigning pending spool {self._pending.spool_id}"
                )
                filaman_spool_id = self._pending.spool_id
                # This is what puts the spool into the slot as far as FilaMan is
                # concerned: the plugin manager turns slot["spool_id"] into the
                # PrinterSlotAssignment. The filament setting below only tells
                # the printer which material it is holding, and the printer has
                # no idea that FilaMan spools exist.
                if filaman_spool_id:
                    new_slot["spool_id"] = filaman_spool_id
                    self._slot_spool_ids[sid] = filaman_spool_id
                    pending_tray_uuid = self._normalize_hex_identifier(
                        new_slot.get("tray_uuid"), 32
                    )
                    if pending_tray_uuid:
                        self._spool_ids_by_tray_uuid[pending_tray_uuid] = (
                            filaman_spool_id
                        )
                self._send_filament_setting(
                    ams_id_parsed, tray_id_parsed, self._pending.filament_data
                )
                # The spool sits in that tray whether or not the printer was
                # told about it, so the location follows the assignment and not
                # the MQTT send.
                if self._loop and filaman_spool_id:
                    self._loop.call_soon_threadsafe(
                        lambda: asyncio.create_task(
                            self._update_spool_location(
                                filaman_spool_id, ams_id_parsed, tray_id_parsed
                            )
                        )
                    )
                if self._pending.timer and self._loop:
                    self._loop.call_soon_threadsafe(self._pending.timer.cancel)
                self._pending = None
                break  # Nur erste Änderung zuweisen

        ams_info = self._build_ams_info(slots)

        # Auto-import is independent from MQTT read-only mode: it only writes
        # to FilaMan's local database.
        self._schedule_auto_import(slots)
        self._schedule_shop_image_refresh(slots)

        # Nur emittieren wenn sich Slot-Daten geändert haben
        if slots == self._current_slots:
            return

        # Event an System melden (muss im asyncio-Thread passieren)
        self._current_slots = slots
        logger.info(
            f"Slot data changed for printer {self.printer_id}, emitting slots_update"
        )
        if self._loop:
            self._loop.call_soon_threadsafe(
                self.emit,
                {"event_type": "slots_update", "slots": slots, "ams_info": ams_info},
            )
