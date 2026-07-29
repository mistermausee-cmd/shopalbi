"""Item catalog: download the ao-bin-dumps item list and reduce it to the
universe we care about — T4-T8 combat equipment that the Black Market buys —
with localized (RU) display names.

The source file is a JSON array of ~12k entries, each shaped like:
    {"UniqueName": "T4_BAG@2", "LocalizedNames": {"EN-US": "...", "RU-RU": "..."}}
Enchantment variants (@1..@4) are listed as their own entries, so we simply
filter them in rather than synthesising them.
"""

from __future__ import annotations

import gzip
import json
import re
import time
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path

from . import config

# Item ids look like: T4_BAG, T4_BAG@2, T6_2H_AXE, T5_HEAD_PLATE_SET1@3,
# T8_OFF_SHIELD_HELL. Tier is the leading digit; enchant is the @N suffix;
# the slot token is the second underscore-separated segment.
_TIER_RE = re.compile(r"^T(\d)_")
_ENCHANT_RE = re.compile(r"@(\d)$")


@dataclass(frozen=True)
class Item:
    item_id: str      # full unique name incl. enchant, e.g. T4_BAG@2
    base_id: str      # without enchant, e.g. T4_BAG
    tier: int
    enchant: int
    slot: str         # MAIN/2H/OFF/HEAD/ARMOR/SHOES/CAPE/CAPEITEM/BAG
    category: str     # weapon/offhand/armor/cape/bag
    name_ru: str
    name_en: str

    def display_name(self) -> str:
        base = self.name_ru or self.name_en or self.item_id
        return f"{base} .{self.enchant}" if self.enchant else base

    def to_dict(self) -> dict:
        d = asdict(self)
        d["display_name"] = self.display_name()
        d["tier_label"] = f"T{self.tier}"
        d["category_label"] = config.CATEGORY_NAMES.get(self.category, self.category)
        return d


def _slot_token(unique_name: str) -> str | None:
    """Extract the equipment slot token from an item id, or None if not gear."""
    parts = unique_name.split("@", 1)[0].split("_")
    if len(parts) < 2:
        return None
    token = parts[1]
    if token in config.EQUIP_SLOT_TOKENS:
        return token
    return None


def _is_black_market_equipment(unique_name: str) -> bool:
    m = _TIER_RE.match(unique_name)
    if not m:
        return False
    tier = int(m.group(1))
    if tier not in config.TIERS:
        return False
    if "_TOOL" in unique_name:            # gathering tools — BM does not buy these
        return False
    if "NONTRADABLE" in unique_name:
        return False
    return _slot_token(unique_name) is not None


def _download_catalog() -> bytes:
    req = urllib.request.Request(
        config.CATALOG_URL,
        headers={"User-Agent": "shopalbi/1.0", "Accept-Encoding": "gzip"},
    )
    with urllib.request.urlopen(req, timeout=config.HTTP_TIMEOUT * 3) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return raw


def _cache_fresh(path: Path) -> bool:
    if not path.exists():
        return False
    age_days = (time.time() - path.stat().st_mtime) / 86400.0
    return age_days <= config.CATALOG_MAX_AGE_DAYS


def load_raw_catalog(force: bool = False) -> list[dict]:
    """Return the full raw catalog, using an on-disk cache when it is fresh."""
    cache = config.CATALOG_CACHE
    if not force and _cache_fresh(cache):
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass  # fall through and re-download
    raw = _download_catalog()
    data = json.loads(raw)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(raw)
    return data


def build_items(force: bool = False) -> list[Item]:
    """Filter the catalog down to Black-Market-eligible T4-T8 equipment."""
    raw = load_raw_catalog(force=force)
    items: list[Item] = []
    seen: set[str] = set()
    for entry in raw:
        uid = entry.get("UniqueName") or ""
        if not uid or uid in seen:
            continue
        if not _is_black_market_equipment(uid):
            continue
        slot = _slot_token(uid)
        if slot is None:
            continue
        seen.add(uid)
        base_id = uid.split("@", 1)[0]
        tier = int(_TIER_RE.match(uid).group(1))
        m_ench = _ENCHANT_RE.search(uid)
        enchant = int(m_ench.group(1)) if m_ench else 0
        names = entry.get("LocalizedNames") or {}
        name_ru = (names or {}).get(config.PRIMARY_LANG) or ""
        name_en = (names or {}).get(config.FALLBACK_LANG) or ""
        # Skip placeholder / unnamed entries (test items etc.)
        if not name_ru and not name_en:
            continue
        items.append(
            Item(
                item_id=uid,
                base_id=base_id,
                tier=tier,
                enchant=enchant,
                slot=slot,
                category=config.EQUIP_SLOT_TOKENS[slot],
                name_ru=name_ru,
                name_en=name_en,
            )
        )
    return items


if __name__ == "__main__":
    its = build_items()
    print(f"Black-Market-eligible items (T{min(config.TIERS)}-T{max(config.TIERS)}): {len(its)}")
    by_cat: dict[str, int] = {}
    for it in its:
        by_cat[it.category] = by_cat.get(it.category, 0) + 1
    for cat, n in sorted(by_cat.items()):
        print(f"  {cat:8s}: {n}")
    for it in its[:5]:
        print("  sample:", it.item_id, "->", it.display_name())
