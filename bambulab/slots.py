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
_AMS_STATE_PRESENCE = {"0": False, "3": True, "8": False, "9": True, "10": False, "11": True}
_TRAY_METADATA_DEFAULTS = {
    "tray_info_idx": "",
    "tray_type": "",
    "tray_color": "",
    "nozzle_temp_min": None,
    "nozzle_temp_max": None,
    "setting_id": "",
    "cali_idx": None,
    "tag_uid": "",
    "tray_uuid": "",
    "tray_sub_brands": "",
    "tray_weight": None,
    "remain": None,
}
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

    # -- Slot-Verarbeitung (asyncio-Thread) --------------------------------------

    @staticmethod
    def _parse_ams_mask(value: Any) -> int | None:
        if isinstance(value, str) and _HEX_IDENTIFIER_RE.fullmatch(value.upper()):
            return int(value, 16)
        return None

    @staticmethod
    def _ams_unit_is_connected(ams_id: int, unit_bits: int | None) -> bool:
        if unit_bits is None:
            return True
        if 0 <= ams_id <= 3:
            bit = ams_id
        elif 128 <= ams_id <= 135:
            bit = 4 + ams_id - 128
        else:
            return True
        return bool(unit_bits & (1 << bit))

    @staticmethod
    def _wire_tray_address(ams_id: int, tray_id: int) -> tuple[int, int]:
        if ams_id < 200:
            return ams_id, tray_id
        if tray_id < 254:
            tray_id = 255 if tray_id == 1 else 254
        return 255, tray_id

    @staticmethod
    def _ams_tray_bit_index(ams_id: int, tray_id: int) -> int | None:
        # Bambu reserves bits 0–15 for four-slot units and 16–23 for AMS HT.
        if 0 <= ams_id <= 3 and 0 <= tray_id <= 3:
            return ams_id * 4 + tray_id
        if 128 <= ams_id <= 135 and tray_id == 0:
            return 16 + ams_id - 128
        return None

    def _ams_tray_presence_bit(self, ams_id: int, tray_id: int) -> bool | None:
        bit = self._ams_tray_bit_index(ams_id, tray_id)
        if (
            bit is None
            or self._tray_exist_bits is None
            or ams_id in self._ams_units_awaiting_occupancy
        ):
            return None
        return bool(self._tray_exist_bits & (1 << bit))

    def _ams_reported_presence(self, ams_id: int, tray_id: int) -> bool | None:
        present = self._ams_tray_presence_bit(ams_id, tray_id)
        return present if present is not None else self._ams_state_presence.get(f"{ams_id}-{tray_id}")

    def _p2s_confirmed_empty_trays(
        self, ams_data: list[dict]
    ) -> tuple[list[dict], int | None]:
        """Use only individual empty states in a shutdown-shaped packet."""
        bits = self._tray_exist_bits
        units = []
        for unit in ams_data:
            ams_id = int(unit.get("id", 0))
            trays = [
                tray for tray in unit.get("tray", [])
                if "id" in tray and _AMS_STATE_PRESENCE.get(str(tray.get("state"))) is False
            ]
            if not trays:
                continue
            units.append({**unit, "tray": trays})
            if bits is not None:
                for tray in trays:
                    bit = self._ams_tray_bit_index(ams_id, int(tray["id"]))
                    if bit is not None:
                        bits &= ~(1 << bit)
        return units, bits

    def _merge_ams_slots(
        self, previous: dict[str, dict], ams_data: list[dict], unit_bits: int | None
    ) -> list[dict[str, Any]]:
        units = {
            unit["ams_id"]: unit for unit in self._current_ams_units
            if self._ams_unit_is_connected(unit["ams_id"], unit_bits)
        }
        reported = {}
        for unit in ams_data:
            ams_id = int(unit.get("id", 0))
            if not self._ams_unit_is_connected(ams_id, unit_bits):
                continue
            old_unit = units.get(ams_id, {})
            units[ams_id] = {
                **old_unit,
                "ams_id": ams_id,
                "humidity": unit.get("humidity_raw", unit.get("humidity", old_unit.get("humidity"))),
                "temp": unit.get("temp", old_unit.get("temp")),
                "serial": self._ams_serials.get(str(ams_id)),
                "info": unit.get("info") or old_unit.get("info"),
            }
            for tray in unit.get("tray", []):
                reported[f'{ams_id}-{int(tray.get("id", 0))}'] = tray

        slots = []
        tray_counts: dict[int, int] = {}
        for slot_index in dict.fromkeys((*previous, *reported)):
            ams_id, tray_id = self._parse_slot_index(slot_index)
            if not self._ams_unit_is_connected(ams_id, unit_bits):
                continue
            tray_counts[ams_id] = tray_counts.get(ams_id, 0) + 1
            old_slot = previous.get(slot_index, {})
            tray = reported.get(slot_index, {})
            state = str(tray.get("state"))
            if state in _AMS_STATE_PRESENCE:
                self._ams_state_presence[slot_index] = _AMS_STATE_PRESENCE[state]

            present = self._ams_reported_presence(ams_id, tray_id)
            if present is None:
                if not tray:
                    present = old_slot.get("present", False)
                elif len(tray) == 1 and "id" in tray:
                    present = False  # Legacy empty trays can contain only the ID.
                else:
                    present = bool(tray["tray_type"]) if "tray_type" in tray else old_slot.get("present", False)

            if ams_id >= 128:
                slot_name = f"AMS HT {ams_id - 127} - Slot {tray_id + 1}"
            elif self._is_ams_lite:
                slot_name = f"AMS Lite - Slot {tray_id + 1}"
            else:
                slot_name = f"AMS {ams_id + 1} - Slot {tray_id + 1}"

            # Empty trays may report a remembered profile. It never belongs to
            # a new physical insertion, even when a mask arrives on its own.
            metadata = {}
            if present:
                incoming_uuid = self._normalize_hex_identifier(tray.get("tray_uuid"), 32)
                old_uuid = self._confirmed_tray_uuids.get(slot_index)
                same_spool = not (incoming_uuid and old_uuid and incoming_uuid != old_uuid)
                if old_slot.get("present") and state != "9" and same_spool:
                    metadata = old_slot
                if _AMS_STATE_PRESENCE.get(state) is not False:
                    metadata = {**metadata, **tray}
            slots.append({
                "slot_index": slot_index,
                "slot_name": slot_name,
                **{key: metadata.get(key, default) for key, default in _TRAY_METADATA_DEFAULTS.items()},
                "present": present,
            })

        self._current_ams_units = [
            {**unit, "tray_count": tray_counts.get(ams_id, 0)}
            for ams_id, unit in units.items()
        ]
        return slots

    def _process_slots(self, payload: dict) -> None:
        """Merge a partial MQTT status and reconcile tray assignments."""
        print_data = payload.get("print", {})
        ams_section = print_data.get("ams") or {}
        vt_tray = print_data.get("vt_tray")
        vir_slot = print_data.get("vir_slot")

        ams_data = ams_section.get("ams") or []
        unit_bits = self._parse_ams_mask(ams_section.get("ams_exist_bits"))
        tray_bits = self._parse_ams_mask(ams_section.get("tray_exist_bits"))
        shutdown_delta = (
            tray_bits == 0
            and self._printer_model == "P2S"
            and ams_section.get("power_on_flag") is False
        )
        if shutdown_delta:
            unit_bits = None
            ams_data, tray_bits = self._p2s_confirmed_empty_trays(ams_data)

        bits_changed = tray_bits is not None and tray_bits != self._tray_exist_bits
        if tray_bits is not None:
            self._tray_exist_bits = tray_bits
            if not shutdown_delta and self._ams_units_awaiting_occupancy:
                if unit_bits is None:
                    connected = {unit["ams_id"] for unit in self._current_ams_units}
                    connected.update(int(unit.get("id", 0)) for unit in ams_data)
                    refreshed = self._ams_units_awaiting_occupancy & connected
                else:
                    refreshed = {
                        ams_id for ams_id in self._ams_units_awaiting_occupancy
                        if self._ams_unit_is_connected(ams_id, unit_bits)
                    }
                self._ams_units_awaiting_occupancy.difference_update(refreshed)
                bits_changed |= bool(refreshed)
        if (
            not ams_data and not bits_changed and unit_bits is None
            and vt_tray is None and vir_slot is None
        ):
            return

        previous = {slot["slot_index"]: slot for slot in self._current_slots}
        prev_ams_units = self._current_ams_units
        prev_ams_slots = {sid: slot for sid, slot in previous.items() if not sid.startswith("255-")}
        ams_slots = self._merge_ams_slots(prev_ams_slots, ams_data, unit_bits)
        if unit_bits is not None:
            self._ams_units_awaiting_occupancy.update(
                unit["ams_id"] for unit in prev_ams_units
                if not self._ams_unit_is_connected(unit["ams_id"], unit_bits)
            )
        active_ams_slots = {slot["slot_index"] for slot in ams_slots}
        retired_names = {
            sid: old.get("slot_name", sid)
            for sid, old in prev_ams_slots.items() if sid not in active_ams_slots
        }
        for sid in active_ams_slots:
            self._saved_ams_slots.pop(sid, None)
        if unit_bits is not None:
            for sid, name in list(self._saved_ams_slots.items()):
                ams_id, _ = self._parse_slot_index(sid)
                if not self._ams_unit_is_connected(ams_id, unit_bits):
                    retired_names.setdefault(sid, name)
                    self._saved_ams_slots.pop(sid)
                    self._ams_units_awaiting_occupancy.add(ams_id)
        retired_slots = [
            {"slot_index": sid, "slot_name": name, "present": False, "is_active": False}
            for sid, name in retired_names.items()
        ]

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
                _, tray_id = self._wire_tray_address(255, tray_id)
                external_number = tray_id - 253
                ext_slots.append(
                    {
                        "slot_index": f"255-{tray_id}",
                        "slot_name": (
                            f"External Tray {external_number}"
                            if multiple_external
                            else "External Tray"
                        ),
                        **{key: tray.get(key, default) for key, default in _TRAY_METADATA_DEFAULTS.items()},
                        "present": bool(tray.get("tray_type")),
                    }
                )
        else:
            ext_slots = [dict(slot) for sid, slot in previous.items() if sid.startswith("255-")]

        slots = ams_slots + ext_slots
        locations: dict[str, int | None] = {}
        pending = self._pending
        candidates = []
        for slot in slots + retired_slots:
            sid = slot["slot_index"]
            old = previous.get(sid)
            previous_spool_id = self._slot_spool_ids.get(sid)
            tray_uuid = self._normalize_hex_identifier(slot.get("tray_uuid"), 32)
            old_uuid = self._confirmed_tray_uuids.get(sid)
            if old_uuid is None and old:
                old_uuid = self._normalize_hex_identifier(old.get("tray_uuid"), 32)
            replaced = bool(
                old and old.get("present") and slot["present"]
                and old_uuid and tray_uuid and old_uuid != tray_uuid
            )
            if not slot["present"] or replaced:
                self._explicit_slot_assignments.discard(sid)
                self._slot_spool_ids.pop(sid, None)
                self._slot_display_metadata.pop(sid, None)
                self._confirmed_tray_uuids.pop(sid, None)
                if old_uuid:
                    self._auto_import_last_attempt.pop(old_uuid, None)
                slot["spool_id"] = None

            if not slot["present"]:
                # The first empty AMS report can clear a managed location left
                # behind while FilaMan was stopped. Subsequent polls are cheap.
                if slot.get("is_active") is False:
                    self._ams_state_presence.pop(sid, None)
                    locations[sid] = None
                elif (old and old.get("present")) or (old is None and not sid.startswith("255-")):
                    locations[sid] = None
                continue

            if tray_uuid:
                self._confirmed_tray_uuids[sid] = tray_uuid
            if sid not in self._explicit_slot_assignments and tray_uuid in self._spool_ids_by_tray_uuid:
                self._slot_spool_ids[sid] = self._spool_ids_by_tray_uuid[tray_uuid]
            spool_id = self._slot_spool_ids.get(sid)
            if spool_id is not None:
                slot["spool_id"] = spool_id
            if replaced or (spool_id is not None and spool_id != previous_spool_id):
                locations[sid] = spool_id

            if pending is None or pending.slot_index not in (None, sid):
                continue
            if old is None:
                if pending.send_setting:
                    continue  # A first snapshot is not a scale-scan insertion.
                old = {}
            parsed = self._parse_slot_index(sid)
            has_presence = self._ams_reported_presence(*parsed) is not None
            if replaced or (has_presence and not old.get("present")):
                candidates.append((0, slot, parsed))
            elif not has_presence and slot.get("tray_type"):
                fields = ("tray_info_idx", "tray_type", "tray_color", "cali_idx")
                if not old.get("present") or any(slot.get(k) != old.get(k) for k in fields) or (
                    slot.get("setting_id") and slot["setting_id"] != old.get("setting_id")
                ):
                    candidates.append((1, slot, parsed))

        if candidates:
            _, slot, (ams_id, tray_id) = min(candidates, key=lambda candidate: candidate[0])
            self._pending = None
            if pending.timer:
                pending.timer.cancel()
            if pending.claim is None or pending.claim():
                sid = slot["slot_index"]
                if pending.spool_id:
                    slot["spool_id"] = pending.spool_id
                    self._slot_spool_ids[sid] = pending.spool_id
                    self._explicit_slot_assignments.add(sid)
                    locations[sid] = pending.spool_id
                logger.info("Assigning pending spool %s to slot %s", pending.spool_id, sid)
                if pending.send_setting:
                    self._send_filament_setting(ams_id, tray_id, pending.filament_data)

        changed = (
            self._needs_slot_sync
            or bool(retired_slots)
            or slots != self._current_slots
            or self._current_ams_units != prev_ams_units
        )
        self._needs_slot_sync = False
        self._current_slots = slots
        for sid, spool_id in locations.items():
            if spool_id is None:
                self._schedule_slot_location_release(sid)
            else:
                self._schedule_slot_location_update(sid, spool_id)

        self._schedule_auto_import(slots)
        self._schedule_shop_image_refresh(slots)

        if changed:
            self.emit({
                "event_type": "slots_update",
                "slots": slots + retired_slots,
                "ams_info": self._build_ams_info(slots),
            })
