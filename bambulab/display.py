"""Live printer state for FilaMan's Display API.

FilaMan's AMS View asks a driver for ``get_display_state()`` on every poll and
merges what it gets with the spools it has on record.  The printer already
sends everything that call needs in its ``push_status`` messages, so this mixin
keeps a running copy of the interesting fields and hands it out unchanged.

Two things make a plain "remember the last message" wrong:

* Bambu sends partial reports.  A message carrying only ``tray_now`` would wipe
  the humidity of every unit, so each section is merged rather than replaced,
  and units are merged per id.
* ``_process_slots`` returns early when a message has no tray data at all.
  Exactly those light messages carry the nozzle and bed temperatures, so the
  snapshot is updated before that shortcut is taken.

The shape is the Bambu status dict itself, which
``app.services.display_service.normalize_driver_state`` accepts as-is - no
translation layer to keep in step with FilaMan.
"""

from typing import Any

# Copied verbatim from print.<key> when a message carries them.  Anything the
# display service cannot read is left out: this travels to every worker on
# every poll, so it stays as small as the feed needs.
_PRINT_FIELDS = (
    "gcode_state",
    "mc_percent",
    "mc_remaining_time",
    "subtask_name",
    "layer_num",
    "total_layer_num",
    "spd_lvl",
    "nozzle_temper",
    "nozzle_target_temper",
    "bed_temper",
    "bed_target_temper",
    "chamber_temper",
)

# Per AMS unit.  humidity and humidity_raw are kept apart on purpose: Bambu
# sends a 1-5 index in humidity on some units and a percentage on others, and
# FilaMan sorts that out itself.  Folding them into one field here would throw
# away the distinction.
_UNIT_FIELDS = (
    "id",
    "temp",
    "humidity",
    "humidity_raw",
    "dry_status",
    "dry_time",
    "dry_target_temp",
    "info",
)


# Dual-nozzle printers (H2D, H2C) report which tray each hotend is fed from
# in print.device.extruder.  Field meanings follow BambuStudio's
# DevExtruderSystem::ParseExtruderInfo.
#
# snow names the tray as (ams_id << 8) | slot; 0xFFFF means nothing loaded.
_EXTRUDER_SLOT_EMPTY = 0xFFFF
# Bit 1 of an extruder's info: filament sits in that hotend.
_EXTRUDER_HAS_FILAMENT = 0b10
# Bit 8 of extruder.state: the hotend in use, 0 right and 1 left.
_ACTIVE_EXTRUDER_SHIFT = 8


def _extruder_slot(entry: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """One hotend's entry as FilaMan's Display API reads it, or None if malformed."""
    try:
        ext_id = int(entry["id"])
        snow = int(entry.get("snow", _EXTRUDER_SLOT_EMPTY))
        flags = int(entry.get("info", 0) or 0)
    except (KeyError, TypeError, ValueError):
        return None
    loaded = 0 <= snow != _EXTRUDER_SLOT_EMPTY
    return str(ext_id), {
        "ams_id": (snow >> 8) & 0xFF if loaded else None,
        "slot_id": snow & 0xFF if loaded else None,
        "has_filament": bool(flags & _EXTRUDER_HAS_FILAMENT),
    }


def _merge_extruders(state: dict[str, Any], print_data: dict[str, Any]) -> None:
    """Keep extruder_slots and active_extruder, the shape display_service reads.

    tray_now is a single value for the whole printer: on a dual-nozzle machine
    with both hotends loaded it names only one of them, and on an H2D it is a
    bare slot without its AMS.  A single-nozzle printer is left alone, its
    tray_now already says everything.  Partial reports keep what they omit.
    """
    device = print_data.get("device")
    extruder = device.get("extruder") if isinstance(device, dict) else None
    if not isinstance(extruder, dict):
        return
    info = extruder.get("info")
    if isinstance(info, list) and len(info) >= 2:
        slots = dict(state.get("extruder_slots") or {})
        for entry in info:
            parsed = _extruder_slot(entry) if isinstance(entry, dict) else None
            if parsed is not None:
                slots[parsed[0]] = parsed[1]
        state["extruder_slots"] = slots
    if "extruder_slots" not in state:
        return
    try:
        state["active_extruder"] = (int(extruder["state"]) >> _ACTIVE_EXTRUDER_SHIFT) & 1
    except (KeyError, TypeError, ValueError):
        pass


def _merge_unit(previous: dict[str, Any] | None, incoming: dict[str, Any]) -> dict[str, Any]:
    """One AMS unit, keeping values the new report did not mention."""
    unit: dict[str, Any] = dict(previous or {})
    for key in _UNIT_FIELDS:
        if incoming.get(key) is not None:
            unit[key] = incoming[key]
    trays = incoming.get("tray")
    if isinstance(trays, list):
        unit["tray"] = trays
    return unit


class DisplayStateMixin:
    """Keep a live snapshot of the printer for FilaMan's Display API."""

    def _update_display_state(self, payload: dict) -> None:
        """Fold one push_status message into the snapshot (paho thread)."""
        print_data = payload.get("print")
        if not isinstance(print_data, dict):
            return

        state = self._display_state
        for key in _PRINT_FIELDS:
            if print_data.get(key) is not None:
                state[key] = print_data[key]

        hms = print_data.get("hms")
        if isinstance(hms, list):
            state["hms"] = hms

        ams_section = print_data.get("ams")
        if isinstance(ams_section, dict):
            ams = state.setdefault("ams", {})
            if ams_section.get("tray_now") is not None:
                ams["tray_now"] = ams_section["tray_now"]
            units = ams_section.get("ams")
            if isinstance(units, list):
                previous = {
                    str(u.get("id")): u for u in ams.get("ams", []) if isinstance(u, dict)
                }
                ams["ams"] = [
                    _merge_unit(previous.get(str(u.get("id"))), u)
                    for u in units
                    if isinstance(u, dict)
                ]

        _merge_extruders(state, print_data)

        for key in ("vt_tray", "vir_slot"):
            value = print_data.get(key)
            if value is not None:
                state[key] = value

    async def get_display_state(self) -> dict[str, Any] | None:
        """FilaMan's Display API hook: the cached snapshot, never a live query.

        Called on every poll of the feed, so it does no work beyond copying the
        top level - the nested sections are only ever replaced, never edited by
        the caller.
        """
        if not self._display_state:
            return {"connected": self._connected}
        return {**self._display_state, "connected": self._connected}
