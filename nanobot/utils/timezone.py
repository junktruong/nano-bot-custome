"""Timezone helpers for RTC/local scheduling defaults."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def _valid_zone(name: str) -> str | None:
    zone = (name or "").strip()
    if not zone:
        return None
    if zone.startswith(":"):
        zone = zone[1:]
    if zone.startswith("/"):
        marker = "/zoneinfo/"
        if marker not in zone:
            return None
        zone = zone.split(marker, 1)[1].strip()
    if not zone:
        return None
    try:
        ZoneInfo(zone)
        return zone
    except Exception:
        return None


def _zone_from_local_tzinfo() -> str | None:
    try:
        tzinfo = datetime.now().astimezone().tzinfo
    except Exception:
        return None
    if tzinfo is None:
        return None

    key = getattr(tzinfo, "key", None)
    if isinstance(key, str):
        checked = _valid_zone(key)
        if checked:
            return checked

    zone = getattr(tzinfo, "zone", None)
    if isinstance(zone, str):
        checked = _valid_zone(zone)
        if checked:
            return checked
    return None


def _zone_from_etc_timezone() -> str | None:
    path = Path("/etc/timezone")
    if not path.is_file():
        return None
    try:
        return _valid_zone(path.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def _zone_from_localtime_link() -> str | None:
    path = Path("/etc/localtime")
    if not path.exists() or not path.is_symlink():
        return None
    try:
        target = str(path.resolve())
    except Exception:
        return None
    return _valid_zone(target)


def get_rtc_timezone_name(default: str = "UTC") -> str:
    """Return timezone name for scheduling defaults.

    Priority:
    1) NANOBOT_RTC_TIMEZONE (if valid)
    2) TZ env var (if valid)
    3) OS local timezone (RTC/system)
    4) default (UTC)
    """
    for candidate in (
        os.environ.get("NANOBOT_RTC_TIMEZONE", ""),
        os.environ.get("TZ", ""),
    ):
        checked = _valid_zone(candidate)
        if checked:
            return checked

    for getter in (_zone_from_local_tzinfo, _zone_from_etc_timezone, _zone_from_localtime_link):
        checked = getter()
        if checked:
            return checked

    fallback = _valid_zone(default)
    return fallback or "UTC"


def get_rtc_zoneinfo(default: str = "UTC") -> ZoneInfo:
    """Return ZoneInfo for scheduling defaults."""
    zone = get_rtc_timezone_name(default=default)
    try:
        return ZoneInfo(zone)
    except Exception:
        return ZoneInfo("UTC")
