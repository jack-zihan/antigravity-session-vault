"""Unit tests for the minimal byte-preserving Protobuf wire codec."""

from __future__ import annotations

import unittest

from session_vault.proto import (
    encode_field1_entry,
    extract_map_key,
    filter_summaries_proto,
    iter_proto_fields,
    upsert_summary_proto,
)


class TestProtoCodec(unittest.TestCase):
    def test_filter_preserves_non_target_and_other_wire_types_verbatim(self) -> None:
        e1 = encode_field1_entry("root-1", b"\x0a\x04Root")
        e2 = encode_field1_entry("sub-1", b"\x0a\x03Sub")
        e3 = encode_field1_entry("cli-1", b"\x0a\x03CLI")
        field2 = b"\x12\x06cli_ts"
        field3 = b"\x18\x2a"
        field4 = b"\x21" + b"\x01\x02\x03\x04\x05\x06\x07\x08"
        field5 = b"\x2d" + b"\xaa\xbb\xcc\xdd"
        buf = e1 + e2 + field2 + e3 + field3 + field4 + field5

        cleaned, removed = filter_summaries_proto(buf, {"sub-1", "nonexistent"})
        self.assertEqual(removed, 1)
        self.assertEqual(cleaned, e1 + field2 + e3 + field3 + field4 + field5)

        keys = [
            extract_map_key(payload)
            for _, _, fnum, wtype, payload in iter_proto_fields(cleaned)
            if fnum == 1 and wtype == 2
        ]
        self.assertEqual(keys, ["root-1", "cli-1"])

    def test_upsert_replaces_existing_and_appends_new(self) -> None:
        buf = encode_field1_entry("root-1", b"old") + b"\x12\x04meta"
        updated = upsert_summary_proto(buf, "root-1", b"new")
        self.assertEqual(updated, b"\x12\x04meta" + encode_field1_entry("root-1", b"new"))

        added = upsert_summary_proto(updated, "root-2", b"second")
        keys = [
            extract_map_key(pl)
            for _, _, fn, wt, pl in iter_proto_fields(added)
            if fn == 1 and wt == 2
        ]
        self.assertEqual(keys, ["root-1", "root-2"])

    def test_malformed_wire_inputs_raise_value_error(self) -> None:
        bad_payloads = [
            b"\x80",                      # truncated varint
            b"\x08" + b"\x80" * 10 + b"\x01",  # overlong varint (>64 bits)
            b"\x09\x01\x02\x03",          # truncated 64-bit fixed
            b"\x0d\x01\x02",              # truncated 32-bit fixed
            b"\x0a\x10short",             # truncated length-delimited
            b"\x0b",                      # unsupported wire type 3
        ]
        for raw in bad_payloads:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    list(iter_proto_fields(raw))


if __name__ == "__main__":
    unittest.main()
