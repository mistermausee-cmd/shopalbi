"""Wire the pieces together: capture -> Photon decode -> market extract -> ship.

    UDP payloads --> PhotonParser --> (kind, code, params)
                                          |
                                   MarketExtractor.books_from_params
                                          |
                                       Shipper.enqueue --> backend
"""

from __future__ import annotations

import logging
import threading
import time

from .capture import PacketCapture
from .config import AgentConfig
from .locations import LocationMap
from .market import MarketExtractor
from .photon import PhotonParser
from .shipper import Shipper

log = logging.getLogger("depth_capture.agent")


class DepthCaptureAgent:
    def __init__(self, cfg: AgentConfig):
        self.cfg = cfg
        self.extractor = MarketExtractor(LocationMap())
        self.shipper = Shipper(cfg)
        self.parser = PhotonParser(self._on_message)
        self.capture = PacketCapture(
            on_payload=self.parser.handle_payload,
            port=cfg.udp_port,
            interface=cfg.interface,
            pcap_file=cfg.pcap_file,
        )
        self._stop = threading.Event()

    # message -> books -> queue
    def _on_message(self, kind: str, code: int, params: dict) -> None:
        # Market data arrives in operation responses; events/requests are
        # cheaply skipped by the structural extractor, but gating on responses
        # avoids the JSON probe on the bulk of gameplay traffic.
        if kind != "response":
            return
        books = self.extractor.books_from_params(params)
        if books:
            self.shipper.enqueue(books)

    def _log_stats(self) -> None:
        p = self.parser.stats
        m = self.extractor.stats
        s = self.shipper.stats
        log.info(
            "stats | packets=%d msgs=%d frags=%d perr=%d | resp=%d orders=%d books=%d unmapped=%d "
            "| shipped_books=%d shipped_orders=%d flushes=%d fails=%d dropped=%d",
            p["packets"], p["messages"], p["fragments"], p["errors"],
            m["responses"], m["orders"], m["books"], m["unmapped"],
            s["shipped_books"], s["shipped_orders"], s["flushes"], s["failures"], s["dropped"],
        )

    def run(self) -> None:
        if not self.cfg.ingest_token:
            log.warning("SHOPALBI_INGEST_TOKEN is empty; the backend will reject "
                        "ingest unless it also has no token. Set it on both sides.")
        self.shipper.start()

        if self.cfg.pcap_file:
            # offline replay: run to completion, drain, done
            self.capture.run_offline()
            self.shipper.flush()
            self._log_stats()
            self.shipper.shutdown()
            return

        self.capture.run_live()
        log.info("capture running; press Ctrl-C to stop")
        try:
            last_report = time.monotonic()
            while not self._stop.is_set():
                time.sleep(1.0)
                if time.monotonic() - last_report >= 30.0:
                    self._log_stats()
                    last_report = time.monotonic()
        except KeyboardInterrupt:
            log.info("interrupt received, shutting down")
        finally:
            self.stop()

    def stop(self) -> None:
        self._stop.set()
        self.capture.stop()
        self.shipper.shutdown()
        self._log_stats()
