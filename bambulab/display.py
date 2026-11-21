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

Dual-nozzle printers (H2D, H2C) need a little more than copying: which tray
feeds which hotend, the temperature of the hotend in use, and a chamber
temperature that is sent encoded while the chamber heats.

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
# The highest slot a tray can have (the nine bays of an AMS 3H).  Anything
# above is a firmware placeholder, not a tray.
_MAX_SLOT = 8
# Bit 1 of an extruder's info: filament sits in that hotend.
_EXTRUDER_HAS_FILAMENT = 0b10
# Bit 8 of extruder.state: the hotend in use, 0 right and 1 left.
_ACTIVE_EXTRUDER_SHIFT = 8
# A temperature above this is sent encoded as target * 65536 + current; it is
# how an H2D reports a heater that is on (chamber_temper, extruder temp).
_ENCODED_TEMP_FLOOR = 500
_TEMP_TARGET_SHIFT = 65536


def _decode_temp(value: Any) -> tuple[float, float | None] | None:
    """(current, target) from a plain or encoded Bambu temperature, or None."""
    try:
        raw = float(value)
    except (TypeError, ValueError):
        return None
    if raw <= _ENCODED_TEMP_FLOOR:
        return raw, None
    encoded = int(raw)
    return float(encoded % _TEMP_TARGET_SHIFT), float(encoded // _TEMP_TARGET_SHIFT)


def _extruder_slot(entry: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """One hotend's entry as FilaMan's Display API reads it, or None if malformed."""
    try:
        ext_id = int(entry["id"])
        snow = int(entry.get("snow", _EXTRUDER_SLOT_EMPTY))
        flags = int(entry.get("info", 0) or 0)
    except (KeyError, TypeError, ValueError):
        return None
    loaded = 0 <= snow != _EXTRUDER_SLOT_EMPTY and (snow & 0xFF) <= _MAX_SLOT
    return str(ext_id), {
        "ams_id": (snow >> 8) & 0xFF if loaded else None,
        "slot_id": snow & 0xFF if loaded else None,
        "has_filament": bool(flags & _EXTRUDER_HAS_FILAMENT),
    }


def _merge_extruders(state: dict[str, Any], print_data: dict[str, Any]) -> None:
    """Keep extruder_slots and active_extruder, the shape display_service reads.

    tray_now is a single value for the whole printer: on a dual-nozzle machine
    with both hotends loaded it names only one of them, and on an H2D it is a
    bare slot without its AMS.  A printer counts as dual-nozzle once a report
    lists two hotends, as BambuStudio decides it; after that a report naming
    only one hotend updates that one.  A single-nozzle printer, which reports
    one entry, is left alone: its tray_now already says everything.
    """
    device = print_data.get("device")
    extruder = device.get("extruder") if isinstance(device, dict) else None
    if not isinstance(extruder, dict):
        return
    info = extruder.get("info")
    dual = "extruder_slots" in state
    if isinstance(info, list) and (dual or len(info) >= 2):
        slots = dict(state.get("extruder_slots") or {})
        temps = dict(state.get("extruder_temps") or {})
        for entry in info:
            if not isinstance(entry, dict):
                continue
            parsed = _extruder_slot(entry)
            if parsed is None:
                continue
            slots[parsed[0]] = parsed[1]
            temp = _decode_temp(entry.get("temp"))
            if temp is not None:
                temps[parsed[0]] = temp
        state["extruder_slots"] = slots
        state["extruder_temps"] = temps
    if "extruder_slots" not in state:
        return
    try:
        state["active_extruder"] = (int(extruder["state"]) >> _ACTIVE_EXTRUDER_SHIFT) & 1
    except (KeyError, TypeError, ValueError):
        pass


def _apply_temperatures(state: dict[str, Any]) -> None:
    """Plain degrees where the printer sent them encoded.

    chamber_temper carries target * 65536 + current while an H2D heats its
    chamber.  On a dual-nozzle printer nozzle_temper follows the left hotend
    whatever is in use, so the hotend in use is read from its own entry.
    """
    chamber = _decode_temp(state.get("chamber_temper"))
    if chamber is not None:
        state["chamber_temper"] = chamber[0]
    active = state.get("active_extruder")
    temp = (state.get("extruder_temps") or {}).get(str(active))
    if temp is None:
        return
    state["nozzle_temper"] = temp[0]
    state["nozzle_target_temper"] = temp[1] if temp[1] is not None else 0.0


def _external_trays(print_data: dict[str, Any]) -> list[dict[str, Any]] | None:
    """The external spool holders as the list display_service reads.

    Newer printers send vir_slot (one entry per holder), older ones a single
    vt_tray dict.  The same order of preference as the slot parser in slots.py.
    """
    vir_slot = print_data.get("vir_slot")
    if isinstance(vir_slot, list) and vir_slot:
        return [t for t in vir_slot if isinstance(t, dict)]
    if isinstance(vir_slot, dict):
        return [vir_slot]
    vt_tray = print_data.get("vt_tray")
    if isinstance(vt_tray, dict):
        return [vt_tray]
    if isinstance(vt_tray, list):
        return [t for t in vt_tray if isinstance(t, dict)]
    return None


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


def _merge_ams(previous: dict[str, Any] | None, ams_section: dict[str, Any]) -> dict[str, Any]:
    """The ams section as a new dict, never edited in place.

    get_display_state() hands out a shallow copy from another thread; a section
    that is only ever replaced cannot be seen half-updated.
    """
    ams: dict[str, Any] = dict(previous or {})
    if ams_section.get("tray_now") is not None:
        ams["tray_now"] = ams_section["tray_now"]
    units = ams_section.get("ams")
    if isinstance(units, list):
        known = {str(u.get("id")): u for u in ams.get("ams", []) if isinstance(u, dict)}
        ams["ams"] = [
            _merge_unit(known.get(str(u.get("id"))), u) for u in units if isinstance(u, dict)
        ]
    return ams


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
            state["ams"] = _merge_ams(state.get("ams"), ams_section)

        _merge_extruders(state, print_data)
        _apply_temperatures(state)

        external = _external_trays(print_data)
        if external is not None:
            state["vt_tray"] = external

    async def get_display_state(self) -> dict[str, Any] | None:
        """FilaMan's Display API hook: the cached snapshot, never a live query.

        Called on every poll of the feed, so it does no work beyond copying the
        top level - every nested section is replaced as a whole, never edited
        in place, so the copy cannot catch one half-written.
        """
        if not self._display_state:
            return {"connected": self._connected}
        return {**self._display_state, "connected": self._connected}
