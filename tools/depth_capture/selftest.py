"""Offline self-test: synthesize a Photon market response and run it through
the real parser + extractor. No network, no scapy, no game required.

    python -m depth_capture.selftest
"""

from __future__ import annotations

import json
import struct
import sys

from .market import MarketExtractor
from .photon import (
    CMD_SEND_RELIABLE, MSG_OP_RESPONSE, T_NULL, T_STRING_ARRAY, PhotonParser,
)


def _string(s: str) -> bytes:
    raw = s.encode("utf-8")
    return struct.pack(">H", len(raw)) + raw


def _string_array(strings: list[str]) -> bytes:
    out = struct.pack(">H", len(strings))
    for s in strings:
        out += _string(s)
    return out


def _operation_response(op_code: int, params: dict[int, bytes]) -> bytes:
    # op_code, return_code(i16), debug(type+value=null), param table
    body = struct.pack(">B", op_code)
    body += struct.pack(">h", 0)
    body += struct.pack(">B", T_NULL)          # debug message = null (no payload)
    body += struct.pack(">H", len(params))
    for key, (type_code, value) in params.items():
        body += struct.pack(">BB", key, type_code) + value
    return body


def _reliable_command(message: bytes) -> bytes:
    # message = 0xF3 signature + message-type byte + message body
    payload = bytes([0xF3, MSG_OP_RESPONSE]) + message
    length = 12 + len(payload)
    header = struct.pack(">BBBBii", CMD_SEND_RELIABLE, 0, 0, 4, length, 0)
    return header + payload


def _packet(commands: list[bytes]) -> bytes:
    header = struct.pack(">HBBIi", 0, 0, len(commands), 0, 0)
    return header + b"".join(commands)


def build_synthetic_packet() -> bytes:
    orders = [
        {"Id": 1, "ItemTypeId": "T5_BAG", "ItemGroupTypeId": "T5_BAG",
         "LocationId": 1002, "QualityLevel": 2, "EnchantmentLevel": 1,
         "UnitPriceSilver": 1500 * 10000, "Amount": 5, "AuctionType": "offer",
         "Expires": "2026-08-01T00:00:00"},
        {"Id": 2, "ItemTypeId": "T5_BAG@1", "ItemGroupTypeId": "T5_BAG",
         "LocationId": 1002, "QualityLevel": 2, "EnchantmentLevel": 0,
         "UnitPriceSilver": 1600 * 10000, "Amount": 3, "AuctionType": "offer",
         "Expires": "2026-08-01T00:00:00"},
        {"Id": 3, "ItemTypeId": "T5_BAG@1", "ItemGroupTypeId": "T5_BAG",
         "LocationId": 3003, "QualityLevel": 2, "EnchantmentLevel": 0,
         "UnitPriceSilver": 2500 * 10000, "Amount": 4, "AuctionType": "request",
         "Expires": "2026-08-01T00:00:00"},
    ]
    strings = [json.dumps(o) for o in orders]
    params = {0: (T_STRING_ARRAY, _string_array(strings))}
    message = _operation_response(op_code=21, params=params)
    return _packet([_reliable_command(message)])


def run() -> int:
    captured: list[dict] = []
    extractor = MarketExtractor()

    def on_message(kind, code, params):
        if kind == "response":
            captured.extend(extractor.books_from_params(params))

    parser = PhotonParser(on_message)
    parser.handle_payload(build_synthetic_packet())

    ok = True

    def check(cond, msg):
        nonlocal ok
        status = "PASS" if cond else "FAIL"
        if not cond:
            ok = False
        print(f"  [{status}] {msg}")

    print("parser stats:", parser.stats)
    print(f"captured {len(captured)} books")

    by_key = {(b["item_id"], b["city"], b["auction_type"]): b for b in captured}

    check(parser.stats["messages"] == 1, "decoded exactly one Photon message")
    check(len(captured) == 2, "grouped into 2 books (Lymhurst offers, BM requests)")

    lym = by_key.get(("T5_BAG@1", "Lymhurst", "offer"))
    check(lym is not None, "Lymhurst offer book present, item normalized to T5_BAG@1")
    if lym:
        prices = sorted(o["unit_price"] for o in lym["orders"])
        check(prices == [1500, 1600], f"prices normalized /10000 -> {prices}")
        check(lym["quality"] == 2, "quality preserved")
        check(sum(o["amount"] for o in lym["orders"]) == 8, "amounts preserved (5+3)")

    bm = by_key.get(("T5_BAG@1", "Black Market", "request"))
    check(bm is not None, "Black Market request book present")
    if bm:
        check(bm["orders"][0]["unit_price"] == 2500, "BM buy price 2500")

    print("extractor stats:", extractor.stats)
    print("RESULT:", "ALL PASS" if ok else "FAILURES")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
