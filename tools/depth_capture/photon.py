"""Photon wire-protocol decoder (Protocol16).

Albion Online uses Exit Games' Photon over UDP. This module turns a raw UDP
payload into the high-level Photon messages it carries — operation requests,
operation responses and events — each with its numeric code and a decoded
parameter table.

Two layers:

  1. Packet / command framing. A Photon packet has a 12-byte header followed by
     N commands. We care about the data-bearing command types: SendReliable,
     SendUnreliable and SendReliableFragment. Large messages are split across
     several fragment commands and must be reassembled before decoding.

  2. Protocol16 value deserialization. Parameter values are type-tagged
     (byte / short / int / string / array / dictionary / ...). ``deserialize``
     reads one value of a given type code; the message parsers build the
     parameter tables on top of it.

Nothing here is Albion-specific — it is a faithful Photon decoder. The Albion
knowledge (which messages carry market data) lives in ``market.py``.

References for the framing and type codes: the widely-used open Photon parsers
(PhotonPackageParser and the albiondata-client Go implementation). Re-derived
and reimplemented here.
"""

from __future__ import annotations

import struct
from typing import Callable

# --- Protocol16 type codes -------------------------------------------------

T_UNKNOWN = 0
T_NULL = 42            # '*'
T_DICTIONARY = 68      # 'D'
T_STRING_ARRAY = 97    # 'a'
T_BYTE = 98            # 'b'
T_CUSTOM = 99          # 'c'
T_DOUBLE = 100         # 'd'
T_EVENTDATA = 101      # 'e'
T_FLOAT = 102          # 'f'
T_HASHTABLE = 104      # 'h'
T_INTEGER = 105        # 'i'
T_SHORT = 107          # 'k'
T_LONG = 108           # 'l'
T_INTEGER_ARRAY = 110  # 'n'
T_BOOLEAN = 111        # 'o'
T_OP_RESPONSE = 112    # 'p'
T_OP_REQUEST = 113     # 'q'
T_STRING = 115         # 's'
T_BYTE_ARRAY = 120     # 'x'
T_ARRAY = 121          # 'y'
T_OBJECT_ARRAY = 122   # 'z'

# --- Photon command types --------------------------------------------------

CMD_ACK = 1
CMD_CONNECT = 2
CMD_VERIFY_CONNECT = 3
CMD_DISCONNECT = 4
CMD_PING = 5
CMD_SEND_RELIABLE = 6
CMD_SEND_UNRELIABLE = 7
CMD_SEND_FRAGMENT = 8

# --- Photon message types (the byte after the 0xF3 signature) --------------

MSG_OP_REQUEST = 2
MSG_OP_RESPONSE = 3
MSG_EVENT = 4
MSG_OP_RESPONSE_ALT = 7

# Safety limits so malformed / non-Photon traffic can't exhaust memory.
_MAX_FRAGMENT_TOTAL = 8 * 1024 * 1024   # 8 MiB assembled message ceiling
_MAX_PENDING_FRAGMENTS = 256            # concurrent fragment groups kept
_MAX_COLLECTION = 1_000_000             # array / table element ceiling


class ProtocolError(Exception):
    """Raised when the byte stream does not match the expected structure."""


class Reader:
    """Big-endian byte cursor with bounds checking."""

    __slots__ = ("buf", "pos", "end")

    def __init__(self, buf: bytes, pos: int = 0, end: int | None = None):
        self.buf = buf
        self.pos = pos
        self.end = len(buf) if end is None else end

    def remaining(self) -> int:
        return self.end - self.pos

    def _take(self, n: int) -> int:
        p = self.pos
        if p + n > self.end:
            raise ProtocolError("unexpected end of buffer")
        self.pos = p + n
        return p

    def read(self, n: int) -> bytes:
        p = self._take(n)
        return self.buf[p:p + n]

    def u8(self) -> int:
        p = self._take(1)
        return self.buf[p]

    def u16(self) -> int:
        p = self._take(2)
        return struct.unpack_from(">H", self.buf, p)[0]

    def i16(self) -> int:
        p = self._take(2)
        return struct.unpack_from(">h", self.buf, p)[0]

    def u32(self) -> int:
        p = self._take(4)
        return struct.unpack_from(">I", self.buf, p)[0]

    def i32(self) -> int:
        p = self._take(4)
        return struct.unpack_from(">i", self.buf, p)[0]

    def i64(self) -> int:
        p = self._take(8)
        return struct.unpack_from(">q", self.buf, p)[0]

    def f32(self) -> float:
        p = self._take(4)
        return struct.unpack_from(">f", self.buf, p)[0]

    def f64(self) -> float:
        p = self._take(8)
        return struct.unpack_from(">d", self.buf, p)[0]


# --- Protocol16 value deserialization --------------------------------------

def _read_string(r: Reader) -> str:
    length = r.u16()
    if length == 0:
        return ""
    return r.read(length).decode("utf-8", errors="replace")


def deserialize(r: Reader, type_code: int):
    """Read a single value of the given Protocol16 type code."""
    if type_code in (T_UNKNOWN, T_NULL):
        return None
    if type_code == T_BOOLEAN:
        return r.u8() != 0
    if type_code == T_BYTE:
        return r.u8()
    if type_code == T_SHORT:
        return r.i16()
    if type_code == T_INTEGER:
        return r.i32()
    if type_code == T_LONG:
        return r.i64()
    if type_code == T_FLOAT:
        return r.f32()
    if type_code == T_DOUBLE:
        return r.f64()
    if type_code == T_STRING:
        return _read_string(r)
    if type_code == T_BYTE_ARRAY:
        length = r.u32()
        if length > _MAX_COLLECTION:
            raise ProtocolError("byte array too large")
        return r.read(length)
    if type_code == T_INTEGER_ARRAY:
        length = r.u32()
        if length > _MAX_COLLECTION:
            raise ProtocolError("int array too large")
        return [r.i32() for _ in range(length)]
    if type_code == T_STRING_ARRAY:
        length = r.u16()
        return [_read_string(r) for _ in range(length)]
    if type_code == T_ARRAY:
        return _read_array(r)
    if type_code == T_OBJECT_ARRAY:
        return _read_object_array(r)
    if type_code == T_DICTIONARY:
        return _read_dictionary(r)
    if type_code == T_HASHTABLE:
        return _read_hashtable(r)
    if type_code == T_CUSTOM:
        custom_type = r.u8()
        length = r.u16()
        return {"__custom__": custom_type, "data": r.read(length)}
    if type_code == T_EVENTDATA:
        code, params = parse_event(r)
        return {"__event__": code, "params": params}
    if type_code == T_OP_REQUEST:
        code, params = parse_operation_request(r)
        return {"__op_request__": code, "params": params}
    if type_code == T_OP_RESPONSE:
        code, params = parse_operation_response(r)
        return {"__op_response__": code, "params": params}
    raise ProtocolError(f"unknown type code {type_code}")


def _read_array(r: Reader) -> list:
    length = r.u16()
    elem_type = r.u8()
    if length > _MAX_COLLECTION:
        raise ProtocolError("array too large")
    return [deserialize(r, elem_type) for _ in range(length)]


def _read_object_array(r: Reader) -> list:
    length = r.u16()
    if length > _MAX_COLLECTION:
        raise ProtocolError("object array too large")
    out = []
    for _ in range(length):
        t = r.u8()
        out.append(deserialize(r, t))
    return out


def _read_typed(r: Reader, declared_type: int):
    # In dictionaries a declared element type of 0 means each entry carries its
    # own type byte.
    if declared_type in (T_UNKNOWN, T_NULL):
        t = r.u8()
        return deserialize(r, t)
    return deserialize(r, declared_type)


def _read_dictionary(r: Reader) -> dict:
    key_type = r.u8()
    val_type = r.u8()
    length = r.u16()
    if length > _MAX_COLLECTION:
        raise ProtocolError("dictionary too large")
    out: dict = {}
    for _ in range(length):
        k = _read_typed(r, key_type)
        v = _read_typed(r, val_type)
        try:
            out[k] = v
        except TypeError:
            out[str(k)] = v
    return out


def _read_hashtable(r: Reader) -> dict:
    length = r.u16()
    if length > _MAX_COLLECTION:
        raise ProtocolError("hashtable too large")
    out: dict = {}
    for _ in range(length):
        kt = r.u8()
        k = deserialize(r, kt)
        vt = r.u8()
        v = deserialize(r, vt)
        try:
            out[k] = v
        except TypeError:
            out[str(k)] = v
    return out


# --- message parsers -------------------------------------------------------

def parse_parameter_table(r: Reader) -> dict:
    count = r.u16()
    params: dict = {}
    for _ in range(count):
        key = r.u8()
        type_code = r.u8()
        params[key] = deserialize(r, type_code)
    return params


def parse_operation_request(r: Reader) -> tuple[int, dict]:
    code = r.u8()
    return code, parse_parameter_table(r)


def parse_operation_response(r: Reader) -> tuple[int, dict]:
    code = r.u8()
    r.i16()                       # return code (unused here)
    debug_type = r.u8()           # debug message value, type-prefixed
    deserialize(r, debug_type)
    return code, parse_parameter_table(r)


def parse_event(r: Reader) -> tuple[int, dict]:
    code = r.u8()
    return code, parse_parameter_table(r)


# --- packet / command framing ----------------------------------------------

# on_message signature: (msg_type: str, code: int, params: dict) -> None
OnMessage = Callable[[str, int, dict], None]


class PhotonParser:
    """Feed it UDP payloads; it calls back with decoded Photon messages.

    Fragment reassembly is handled internally. The parser is deliberately
    forgiving: a single malformed packet (or unrelated UDP traffic on the port)
    is skipped rather than raising, because a passive sniffer sees plenty of
    both.
    """

    def __init__(self, on_message: OnMessage):
        self.on_message = on_message
        # start_sequence_number -> reassembly state
        self._fragments: dict[int, dict] = {}
        self.stats = {"packets": 0, "messages": 0, "fragments": 0, "errors": 0}

    def handle_payload(self, payload: bytes) -> None:
        if len(payload) < 12:
            return
        self.stats["packets"] += 1
        r = Reader(payload)
        try:
            r.u16()                      # peer id
            flags = r.u8()               # crc flag (0 for Albion)
            command_count = r.u8()
            r.u32()                      # timestamp
            r.i32()                      # challenge
            if flags != 0:
                # CRC-enabled packets have a different trailing layout; Albion
                # runs with CRC off, so we simply skip the rare exception.
                return
            for _ in range(command_count):
                if not self._handle_command(r):
                    break
        except ProtocolError:
            self.stats["errors"] += 1

    def _handle_command(self, r: Reader) -> bool:
        if r.remaining() < 12:
            return False
        command_type = r.u8()
        r.u8()                           # channel id
        r.u8()                           # command flags
        r.u8()                           # reserved
        command_length = r.i32()
        r.i32()                          # reliable sequence number
        body_len = command_length - 12
        if body_len < 0 or body_len > r.remaining():
            return False
        body = r.read(body_len)

        if command_type == CMD_SEND_RELIABLE:
            self._handle_message(body)
        elif command_type == CMD_SEND_UNRELIABLE:
            if len(body) >= 4:
                self._handle_message(body[4:])   # skip 4-byte unreliable seq
        elif command_type == CMD_SEND_FRAGMENT:
            self._handle_fragment(body)
        return True

    def _handle_message(self, body: bytes) -> None:
        # body = [signature 0xF3][message type][message payload...]
        if len(body) < 2:
            return
        msg_type = body[1]
        if msg_type & 0x80:
            return                        # encrypted message, cannot decode
        msg_type &= 0x7F
        r = Reader(body, 2)
        try:
            if msg_type == MSG_OP_REQUEST:
                code, params = parse_operation_request(r)
                kind = "request"
            elif msg_type in (MSG_OP_RESPONSE, MSG_OP_RESPONSE_ALT):
                code, params = parse_operation_response(r)
                kind = "response"
            elif msg_type == MSG_EVENT:
                code, params = parse_event(r)
                kind = "event"
            else:
                return
        except ProtocolError:
            self.stats["errors"] += 1
            return
        self.stats["messages"] += 1
        self.on_message(kind, code, params)

    def _handle_fragment(self, body: bytes) -> None:
        r = Reader(body)
        try:
            start_seq = r.i32()
            fragment_count = r.i32()
            fragment_number = r.i32()
            total_length = r.i32()
            fragment_offset = r.i32()
        except ProtocolError:
            return
        frag = r.read(r.remaining())

        if total_length <= 0 or total_length > _MAX_FRAGMENT_TOTAL:
            return
        if fragment_offset < 0 or fragment_offset + len(frag) > total_length:
            return

        entry = self._fragments.get(start_seq)
        if entry is None:
            if len(self._fragments) >= _MAX_PENDING_FRAGMENTS:
                # evict the oldest pending group to bound memory
                oldest = next(iter(self._fragments))
                self._fragments.pop(oldest, None)
            entry = {
                "buf": bytearray(total_length),
                "total": total_length,
                "count": fragment_count,
                "received_numbers": set(),
                "received_bytes": 0,
            }
            self._fragments[start_seq] = entry
            self.stats["fragments"] += 1

        if fragment_number in entry["received_numbers"]:
            return
        entry["buf"][fragment_offset:fragment_offset + len(frag)] = frag
        entry["received_numbers"].add(fragment_number)
        entry["received_bytes"] += len(frag)

        if entry["received_bytes"] >= entry["total"]:
            assembled = bytes(entry["buf"])
            self._fragments.pop(start_seq, None)
            self._handle_message(assembled)
