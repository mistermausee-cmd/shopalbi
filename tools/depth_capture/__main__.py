"""CLI entry point:  python -m depth_capture [options]

Examples
--------
Live capture, shipping to a local backend::

    sudo SHOPALBI_INGEST_TOKEN=secret python -m depth_capture \
        --backend http://127.0.0.1:8000

Replay a capture file (no privileges needed)::

    python -m depth_capture --pcap session.pcapng --backend http://host:8000 \
        --token secret --log-level debug

List capture interfaces::

    python -m depth_capture --list-ifaces
"""

from __future__ import annotations

import argparse
import logging
import sys

from .agent import DepthCaptureAgent
from .config import AgentConfig


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _list_ifaces() -> int:
    try:
        from scapy.all import get_if_list
    except ImportError:
        print("scapy is not installed; run: pip install -r requirements.txt", file=sys.stderr)
        return 1
    print("Available capture interfaces:")
    for name in get_if_list():
        print(f"  {name}")
    return 0


def build_config(args: argparse.Namespace) -> AgentConfig:
    cfg = AgentConfig()
    if args.backend:
        cfg.backend_url = args.backend
    if args.token:
        cfg.ingest_token = args.token
    if args.iface:
        cfg.interface = args.iface
    if args.pcap:
        cfg.pcap_file = args.pcap
    if args.port:
        cfg.udp_port = args.port
    if args.log_level:
        cfg.log_level = args.log_level
    return cfg


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="depth_capture",
        description="Passive Albion market order-book capture for shopalbi.",
    )
    parser.add_argument("--backend", help="shopalbi base URL (default env SHOPALBI_BACKEND_URL or http://127.0.0.1:8000)")
    parser.add_argument("--token", help="ingest token (default env SHOPALBI_INGEST_TOKEN)")
    parser.add_argument("--iface", help="capture interface (default: pcap picks one)")
    parser.add_argument("--pcap", help="replay a pcap/pcapng file instead of live capture")
    parser.add_argument("--port", type=int, help="Photon UDP port (default 5056)")
    parser.add_argument("--log-level", default=None, help="debug|info|warning|error")
    parser.add_argument("--list-ifaces", action="store_true", help="list interfaces and exit")
    args = parser.parse_args(argv)

    if args.list_ifaces:
        _configure_logging("warning")
        return _list_ifaces()

    cfg = build_config(args)
    _configure_logging(cfg.log_level)
    log = logging.getLogger("depth_capture")
    log.info("shipping to %s", cfg.ingest_endpoint())

    try:
        DepthCaptureAgent(cfg).run()
    except PermissionError:
        log.error("permission denied opening the capture device. Run as root/Administrator "
                  "(Linux/macOS: sudo; Windows: install Npcap and run as Administrator).")
        return 1
    except ImportError:
        log.error("scapy is required for live capture. Install it: pip install -r requirements.txt")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
