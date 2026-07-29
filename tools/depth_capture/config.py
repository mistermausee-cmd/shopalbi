"""Capture agent configuration (env-driven, CLI can override)."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _get(name: str, default: str) -> str:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def _get_int(name: str, default: int) -> int:
    try:
        return int(_get(name, str(default)))
    except ValueError:
        return default


def _get_float(name: str, default: float) -> float:
    try:
        return float(_get(name, str(default)))
    except ValueError:
        return default


@dataclass
class AgentConfig:
    # Where captured books are shipped.
    backend_url: str = _get("SHOPALBI_BACKEND_URL", "http://127.0.0.1:8000")
    ingest_token: str = _get("SHOPALBI_INGEST_TOKEN", "")

    # Photon runs on UDP; Albion uses 5056. Change only if you know why.
    udp_port: int = _get_int("SHOPALBI_PHOTON_PORT", 5056)

    # Capture interface. Empty => let the pcap layer pick the default. On
    # multi-NIC machines set this to the adapter carrying game traffic.
    interface: str = _get("SHOPALBI_CAPTURE_IFACE", "")

    # Optional pcap file to replay instead of live capture (testing / offline).
    pcap_file: str = _get("SHOPALBI_PCAP_FILE", "")

    # Shipping cadence: flush whenever we have this many books OR this many
    # seconds have passed since the last flush, whichever comes first.
    flush_max_books: int = _get_int("SHOPALBI_FLUSH_MAX_BOOKS", 200)
    flush_interval_s: float = _get_float("SHOPALBI_FLUSH_INTERVAL_S", 5.0)

    # Offline buffer ceiling: if the backend is unreachable, keep at most this
    # many books queued (newest wins) so memory stays bounded.
    max_queue_books: int = _get_int("SHOPALBI_MAX_QUEUE_BOOKS", 20000)

    http_timeout_s: float = _get_float("SHOPALBI_HTTP_TIMEOUT", 20.0)
    http_retries: int = _get_int("SHOPALBI_HTTP_RETRIES", 4)

    log_level: str = _get("SHOPALBI_LOG_LEVEL", "info")

    def ingest_endpoint(self) -> str:
        return self.backend_url.rstrip("/") + "/api/ingest/orders"
