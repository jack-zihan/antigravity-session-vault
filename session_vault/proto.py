"""Minimal byte-preserving Protobuf wire codec for agyhub_summaries_proto.pb."""

from __future__ import annotations

from typing import Iterator


def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    val = shift = 0
    n = len(buf)
    while True:
        if i >= n:
            raise ValueError(f"Truncated protobuf varint at offset {i}")
        if shift > 63:
            raise ValueError(f"Overlong protobuf varint at offset {i}")
        b = buf[i]
        i += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, i
        shift += 7


def _encode_varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def iter_proto_fields(buf: bytes) -> Iterator[tuple[int, int, int, int, bytes]]:
    """Yield (start, end, field_number, wire_type, payload) for top-level fields."""
    i, n = 0, len(buf)
    while i < n:
        start = i
        tag, i = _read_varint(buf, i)
        fnum, wtype = tag >> 3, tag & 7
        if wtype == 0:
            _, i = _read_varint(buf, i)
            payload = buf[start:i]
        elif wtype == 1:
            if i + 8 > n:
                raise ValueError(f"Truncated 64-bit field {fnum} at offset {start}")
            i += 8
            payload = buf[start:i]
        elif wtype == 2:
            ln, i = _read_varint(buf, i)
            if ln < 0 or i + ln > n:
                raise ValueError(f"Truncated length-delimited field {fnum} (len={ln}) at offset {start}")
            payload = buf[i : i + ln]
            i += ln
        elif wtype == 5:
            if i + 4 > n:
                raise ValueError(f"Truncated 32-bit field {fnum} at offset {start}")
            i += 4
            payload = buf[start:i]
        else:
            raise ValueError(f"Unsupported protobuf wire type {wtype} at offset {start}")
        yield start, i, fnum, wtype, payload


def extract_map_key(entry_payload: bytes) -> str | None:
    for _, _, fnum, wtype, payload in iter_proto_fields(entry_payload):
        if fnum == 1 and wtype == 2:
            return payload.decode("utf-8")
    return None


def encode_field1_entry(cid: str, raw_summary: bytes) -> bytes:
    kb = cid.encode("utf-8")
    inner = (
        b"\x0a"
        + _encode_varint(len(kb))
        + kb
        + b"\x12"
        + _encode_varint(len(raw_summary))
        + raw_summary
    )
    return b"\x0a" + _encode_varint(len(inner)) + inner


def filter_summaries_proto(buf: bytes, purge_ids: set[str]) -> tuple[bytes, int]:
    """Remove Field-1 map entries whose keys are in purge_ids, preserving all other bytes verbatim."""
    out = bytearray()
    removed = 0
    for start, end, fnum, wtype, payload in iter_proto_fields(buf):
        if fnum == 1 and wtype == 2 and extract_map_key(payload) in purge_ids:
            removed += 1
            continue
        out.extend(buf[start:end])
    return bytes(out), removed


def upsert_summary_proto(buf: bytes, cid: str, raw_summary: bytes) -> bytes:
    """Replace or append a Field-1 summary entry without touching Field 2 or unknown fields."""
    cleaned, _ = filter_summaries_proto(buf, {cid})
    return cleaned + encode_field1_entry(cid, raw_summary)
