"""Packet capture front-end.

Sniffs UDP on the Photon port and hands each datagram's payload to a callback.
Uses scapy (libpcap / Npcap under the hood). scapy is imported lazily so this
module — and the rest of the package — can be imported and tested without the
capture stack present (e.g. on the backend host or in CI).

Live capture needs privileges: root on Linux/macOS, and Npcap installed on
Windows (run the shell as Administrator).
"""

from __future__ import annotations

import logging
from typing import Callable

log = logging.getLogger("depth_capture.capture")

PayloadHandler = Callable[[bytes], None]


def _extract_udp_payload(pkt) -> bytes | None:
    from scapy.layers.inet import UDP  # lazy
    if UDP not in pkt:
        return None
    udp = pkt[UDP]
    payload = bytes(udp.payload)
    return payload or None


class PacketCapture:
    def __init__(self, on_payload: PayloadHandler, port: int = 5056,
                 interface: str = "", pcap_file: str = ""):
        self.on_payload = on_payload
        self.port = port
        self.interface = interface or None
        self.pcap_file = pcap_file or None
        self._sniffer = None

    def _bpf(self) -> str:
        return f"udp port {self.port}"

    def _handle(self, pkt) -> None:
        try:
            payload = _extract_udp_payload(pkt)
            if payload:
                self.on_payload(payload)
        except Exception:  # a single bad packet must never kill the sniffer
            log.debug("packet handler error", exc_info=True)

    def run_offline(self) -> None:
        """Replay a pcap file to completion (blocking)."""
        from scapy.all import sniff
        log.info("replaying pcap file %s (filter: %s)", self.pcap_file, self._bpf())
        sniff(offline=self.pcap_file, filter=self._bpf(), prn=self._handle, store=False)
        log.info("pcap replay finished")

    def run_live(self) -> None:
        """Start a live async sniffer (non-blocking); call stop() to end."""
        from scapy.all import AsyncSniffer
        log.info("starting live capture on %s (filter: %s)",
                 self.interface or "default interface", self._bpf())
        self._sniffer = AsyncSniffer(
            iface=self.interface,
            filter=self._bpf(),
            prn=self._handle,
            store=False,
        )
        self._sniffer.start()

    def stop(self) -> None:
        if self._sniffer is not None:
            try:
                self._sniffer.stop()
            except Exception:
                log.debug("error stopping sniffer", exc_info=True)
            self._sniffer = None
