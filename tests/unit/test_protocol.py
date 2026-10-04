"""JSON-RPC envelope and NDJSON framing."""

from __future__ import annotations

import json

import pytest

from agent.models.protocol import (
    ErrorCode,
    FrameDecoder,
    RpcError,
    encode_frame,
    failure,
    request,
    success,
)


def test_request_and_response_shapes():
    call = request("1", "fs.read", {"path": "/workspace/a"})
    assert call == {"jsonrpc": "2.0", "id": "1", "method": "fs.read", "params": {"path": "/workspace/a"}}
    assert success("1", {"ok": True})["result"] == {"ok": True}
    error = failure("1", RpcError(ErrorCode.PATH_DENIED, "nope", {"path": "/etc"}))
    assert error["error"]["code"] == 1008
    assert error["error"]["data"] == {"path": "/etc"}


def test_rpc_error_roundtrip():
    original = RpcError(ErrorCode.TIMEOUT, "too slow", {"method": "exec.run"})
    restored = RpcError.from_dict(original.to_dict())
    assert restored.code == original.code
    assert restored.message == original.message
    assert restored.data == original.data
    assert restored.label == "timeout"


def test_encode_frame_is_newline_delimited_json():
    raw = encode_frame({"a": 1})
    assert raw.endswith(b"\n")
    assert json.loads(raw.decode()) == {"a": 1}


def test_decoder_handles_split_and_batched_frames():
    decoder = FrameDecoder()
    assert decoder.feed(b'{"id":1,"result"') == []
    assert decoder.pending_bytes > 0
    out = decoder.feed(b':2}\n{"id":2,"result":3}\n')
    assert [frame["id"] for frame in out] == [1, 2]


def test_decoder_handles_multibyte_utf8_split_across_chunks():
    payload = encode_frame({"text": "中文 → ok"})
    decoder = FrameDecoder()
    first = decoder.feed(payload[:12])
    second = decoder.feed(payload[12:])
    frames = first + second
    assert frames and frames[0]["text"] == "中文 → ok"


def test_decoder_skips_blank_lines():
    decoder = FrameDecoder()
    assert decoder.feed(b"\n\n   \n") == []


def test_decoder_rejects_malformed_json():
    decoder = FrameDecoder()
    with pytest.raises(RpcError) as excinfo:
        decoder.feed(b"{not json}\n")
    assert excinfo.value.code == ErrorCode.PARSE_ERROR


def test_decoder_rejects_non_object_frames():
    decoder = FrameDecoder()
    with pytest.raises(RpcError) as excinfo:
        decoder.feed(b"[1,2,3]\n")
    assert excinfo.value.code == ErrorCode.INVALID_REQUEST


def test_decoder_enforces_the_frame_limit():
    decoder = FrameDecoder(max_frame_bytes=64)
    with pytest.raises(RpcError) as excinfo:
        decoder.feed(b"x" * 128)
    assert excinfo.value.code == ErrorCode.LIMIT_EXCEEDED


def test_decoder_rejects_oversized_complete_frame():
    decoder = FrameDecoder(max_frame_bytes=32)
    with pytest.raises(RpcError):
        decoder.feed(b"y" * 40 + b"\n")


def test_encode_frame_rejects_oversized_payload():
    with pytest.raises(RpcError) as excinfo:
        encode_frame({"data": "z" * (9 * 1024 * 1024)})
    assert excinfo.value.code == ErrorCode.LIMIT_EXCEEDED
