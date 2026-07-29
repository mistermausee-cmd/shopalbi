"""Albion market LocationId -> city name mapping.

The order objects the server sends carry a numeric ``LocationId`` for the
market they belong to. These ids are stable within a patch but are not
officially documented, and Sandbox has renumbered them before, so this table
is a *default* that the operator confirms once and can override without
touching code.

Names here match ``config.ROYAL_CITIES`` / ``config.BLACK_MARKET`` on the
backend exactly, so captured books land in the same city buckets the AODP
pipeline already uses.

Calibration: run the agent with ``--log-level debug`` and watch the
"captured book" lines. Each shows the raw location id. If a city shows up as
``loc_<id>`` instead of its name, add the id here or to the override file
pointed to by ``SHOPALBI_CAPTURE_LOCATIONS`` (a JSON object of
``{"id": "City Name"}``) and restart.
"""

from __future__ import annotations

import json
import logging
import os

log = logging.getLogger("depth_capture.locations")

# Widely-referenced defaults (same ids the public AODP tooling uses).
DEFAULT_LOCATIONS: dict[str, str] = {
    "7": "Thetford",
    "1002": "Lymhurst",
    "2004": "Bridgewatch",
    "3003": "Black Market",
    "3005": "Caerleon",
    "3008": "Martlock",
    "4002": "Fort Sterling",
    "5003": "Brecilien",
}


def _load_overrides() -> dict[str, str]:
    path = os.environ.get("SHOPALBI_CAPTURE_LOCATIONS", "").strip()
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("location override file must be a JSON object")
        overrides = {str(k): str(v) for k, v in data.items()}
        log.info("loaded %d location overrides from %s", len(overrides), path)
        return overrides
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        log.warning("could not load location overrides from %s: %s", path, exc)
        return {}


class LocationMap:
    """Resolves raw location ids to city names, with override support."""

    def __init__(self) -> None:
        self._map = dict(DEFAULT_LOCATIONS)
        self._map.update(_load_overrides())
        self._unknown_seen: set[str] = set()

    def resolve(self, location_id) -> tuple[str, bool]:
        """Return (city_name, known). Unknown ids get a stable ``loc_<id>``
        placeholder so their books are still stored and can be relabelled
        later by fixing the mapping."""
        if location_id is None:
            return "loc_unknown", False
        key = str(location_id).strip()
        # Some markets append a sub-index like "3005-Auction2"; take the head.
        head = key.split("-", 1)[0].split("@", 1)[0]
        name = self._map.get(key) or self._map.get(head)
        if name:
            return name, True
        if head not in self._unknown_seen:
            self._unknown_seen.add(head)
            log.warning("unmapped LocationId %r -> stored as loc_%s "
                        "(add it to SHOPALBI_CAPTURE_LOCATIONS to relabel)", key, head)
        return f"loc_{head}", False
