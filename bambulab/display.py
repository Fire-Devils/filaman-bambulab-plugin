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
