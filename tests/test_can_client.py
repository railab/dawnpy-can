# tools/dawnpy/tests/test_can_client.py
#
# SPDX-License-Identifier: Apache-2.0
#

"""Tests for CAN client helpers and descriptor mapping."""

import socket
import time
from unittest.mock import Mock, patch

import pytest
from dawnpy.descriptor.client import load_client_descriptor
from dawnpy.descriptor.definitions.registry import IOTypeInfo, ProtoTypeInfo
from dawnpy.descriptor.validation.conflicts import check_key_conflicts

from dawnpy_can.client import (
    CanClient,
    HeartbeatState,
    pack_value,
    unpack_value,
)
from dawnpy_can.console import CanConsole
from dawnpy_can.descriptor import CanAccess, load_can_descriptor


@pytest.fixture(autouse=True)
def descriptor_types(monkeypatch):
    """Provide only descriptor types used by this test module."""
    from dawnpy.descriptor.definitions import registry

    registry.reset_type_registry()
    monkeypatch.setattr(registry, "_REGISTRY_LOADED", True)
    registry._IO_TYPES_DATA["dummy"] = IOTypeInfo(
        cpp_class="CIODummy",
        header="dawn/io/dummy.hxx",
        helper_func="{cpp_class}::objectId",
        params=["dtype", "timestamp", "instance"],
    )
    registry._PROTO_TYPES_DATA["can"] = ProtoTypeInfo(
        cpp_class="CProtoCan",
        header="dawn/proto/can/can.hxx",
    )
    yield
    registry.reset_type_registry()


def _write_descriptor(tmp_path, content: str) -> str:
    path = tmp_path / "descriptor.yaml"
    path.write_text(content)
    return str(path)


def test_client_descriptor_tags(tmp_path):
    descriptor = """
metadata:
  version: '1.0'
ios:
  - id: io1
    type: dummy
    instance: 1
    dtype: uint16
    tags: [heartbeat, fast]
  - id: io2
    type: dummy
    instance: 2
    dtype: uint16
    tags: heartbeat
protocols:
  - id: can_main
    type: can
    instance: 1
    config:
      node_id: 0
      objects: []
"""
    path = _write_descriptor(tmp_path, descriptor)
    desc = load_client_descriptor(path)

    tagged = desc.get_tagged_ios("heartbeat")
    ids = sorted(io.io_id for io in tagged)
    assert ids == ["io1", "io2"]


def test_can_descriptor_mapping_indexed(tmp_path):
    descriptor = """
metadata:
  version: '1.0'
ios:
  - &io1
    id: io1
    type: dummy
    instance: 1
    dtype: uint8
  - &io2
    id: io2
    type: dummy
    instance: 2
    dtype: uint8
protocols:
  - id: can_main
    type: can
    instance: 1
    config:
      node_id: 0
      objects:
        - type: read_indexed
          flags: 0
          can_id_start: 256
          count: 2
          bindings:
            - *io1
            - *io2
        - type: write
          flags: 0
          can_id_start: 512
          count: 2
          bindings:
            - *io1
            - *io2
"""
    path = _write_descriptor(tmp_path, descriptor)
    desc = load_can_descriptor(path)

    access_io1 = desc.get_access("io1")
    read_idx = next(a for a in access_io1 if a.method == "read_indexed")
    write = next(a for a in access_io1 if a.method == "write")

    assert read_idx.can_id == 0 + 256
    assert read_idx.index == 1
    assert write.can_id == 0 + 512
    assert write.index is None
    assert not desc.uses_extended_ids()


def test_can_conflict_keys_are_can_id_based(tmp_path):
    descriptor = """
metadata:
  version: '1.0'
ios:
  - &io1
    id: io1
    type: dummy
    instance: 1
    dtype: uint8
  - &io2
    id: io2
    type: dummy
    instance: 2
    dtype: uint8
protocols:
  - id: can_main
    type: can
    instance: 1
    config:
      node_id: 0
      objects:
        - type: read_indexed
          flags: 0
          can_id_start: 256
          count: 2
          bindings:
            - *io1
            - *io2
        - type: write
          flags: 0
          can_id_start: 512
          count: 2
          bindings:
            - *io1
            - *io2
"""
    path = _write_descriptor(tmp_path, descriptor)
    desc = load_can_descriptor(path)
    keys = list(CanConsole._conflict_keys(desc))
    assert keys.count((256, "obj0:read_indexed:0x100")) == 1
    assert (512, "obj1:write:0x200") in keys
    assert (513, "obj1:write:0x200") in keys


def test_can_block_overlap_detected_in_single_descriptor(tmp_path):
    descriptor = """
metadata:
  version: '1.0'
ios:
  - &io1
    id: io1
    type: dummy
    instance: 1
    dtype: uint8
  - &io2
    id: io2
    type: dummy
    instance: 2
    dtype: uint8
protocols:
  - id: can_main
    type: can
    instance: 1
    config:
      node_id: 0
      objects:
        - type: read
          flags: 0
          can_id_start: 256
          count: 2
          bindings:
            - *io1
            - *io2
        - type: write
          flags: 0
          can_id_start: 257
          count: 1
          bindings:
            - *io1
"""
    path = _write_descriptor(tmp_path, descriptor)
    desc = load_can_descriptor(path)
    conflicts = check_key_conflicts([(path, CanConsole._conflict_keys(desc))])
    assert conflicts
    assert conflicts[0].can_id == 257


def test_can_block_overlap_uses_bound_io_count(tmp_path):
    descriptor = """
metadata:
  version: '1.0'
ios:
  - &io1
    id: io1
    type: dummy
    instance: 1
    dtype: uint8
  - &io2
    id: io2
    type: dummy
    instance: 2
    dtype: uint8
protocols:
  - id: can_main
    type: can
    instance: 1
    config:
      node_id: 0
      objects:
        - type: read
          flags: 0
          can_id_start: 100
          count: 1
          bindings:
            - *io1
            - *io2
        - type: write
          flags: 0
          can_id_start: 101
          count: 1
          bindings:
            - *io1
"""
    path = _write_descriptor(tmp_path, descriptor)
    desc = load_can_descriptor(path)
    conflicts = check_key_conflicts([(path, CanConsole._conflict_keys(desc))])
    assert conflicts
    assert conflicts[0].can_id == 101


def test_can_block_overlap_ignores_user_declared_count(tmp_path):
    descriptor = """
metadata:
  version: '1.0'
ios:
  - &io1
    id: io1
    type: dummy
    instance: 1
    dtype: uint8
  - &io2
    id: io2
    type: dummy
    instance: 2
    dtype: uint8
protocols:
  - id: can_main
    type: can
    instance: 1
    config:
      node_id: 0
      objects:
        - type: read
          flags: 0
          can_id_start: 100
          count: 3
          bindings:
            - *io1
        - type: write
          flags: 0
          can_id_start: 102
          count: 1
          bindings:
            - *io2
"""
    path = _write_descriptor(tmp_path, descriptor)
    desc = load_can_descriptor(path)
    conflicts = check_key_conflicts([(path, CanConsole._conflict_keys(desc))])
    assert not conflicts


def test_can_descriptor_extended_ids(tmp_path):
    descriptor = """
metadata:
  version: '1.0'
ios:
  - &io1
    id: io1
    type: dummy
    instance: 1
    dtype: uint8
    tags: [heartbeat]
protocols:
  - id: can_main
    type: can
    instance: 1
    config:
      node_id: 0
      objects:
        - type: read
          flags: 0
          can_id_start: 4096
          count: 1
          bindings: [*io1]
"""
    path = _write_descriptor(tmp_path, descriptor)
    desc = load_can_descriptor(path)
    assert desc.uses_extended_ids()
    assert desc.get_tagged_ios("heartbeat")


def test_can_descriptor_missing_proto(tmp_path):
    descriptor = """
metadata:
  version: '1.0'
ios: []
protocols:
  - id: serial0
    type: serial
    instance: 1
    bindings: []
"""
    path = _write_descriptor(tmp_path, descriptor)
    with pytest.raises(ValueError, match="No CAN protocol entry"):
        load_can_descriptor(path)


def test_can_descriptor_string_bindings(tmp_path):
    descriptor = """
metadata:
  version: '1.0'
ios:
  - id: io1
    type: dummy
    instance: 1
    dtype: uint8
protocols:
  - id: can_main
    type: can
    instance: 1
    config:
      node_id: 0
      objects:
        - type: read
          flags: 0
          can_id_start: 1
          count: 1
          bindings: ["io1"]
"""
    path = _write_descriptor(tmp_path, descriptor)
    desc = load_can_descriptor(path)
    assert desc.get_io("io1") is not None
    assert desc.get_access("io1")[0].can_id == 1


def test_pack_unpack_values():
    packed = pack_value("uint16", 513)
    assert packed == b"\x01\x02"
    assert unpack_value("uint16", packed) == 513

    packed = pack_value("bool", "true")
    assert packed == b"\x01"
    assert unpack_value("bool", packed) == 1

    packed = pack_value("bool", "no")
    assert packed == b"\x00"
    assert unpack_value("bool", packed) == 0

    packed = pack_value("float", 3.5)
    assert packed is not None
    value = unpack_value("float", packed)
    assert value == pytest.approx(3.5, rel=1e-6)

    packed = pack_value("char", "A")
    assert packed == b"A"
    packed = pack_value("char", b"B")
    assert packed == b"B"

    packed = pack_value("uint16_t", 1)
    assert packed == b"\x01\x00"

    assert pack_value("invalid", 1) is None
    assert unpack_value("invalid", b"\x00") is None
    assert unpack_value("uint32", b"\x00") is None


def test_heartbeat_state():
    hb = HeartbeatState(can_id=0x100, interval_s=1.0, timeout_mult=2)
    assert not hb.is_live()
    now = time.time()
    hb.mark(now)
    assert hb.is_live(now + 1.5)
    assert not hb.is_live(now + 2.5)
    assert hb.is_live()


class _FakeSock:
    def __init__(self):
        self.sent = []

    def send(self, frame):
        self.sent.append(frame)


class _FakeCANSocket:
    def __init__(self, ifname="can0"):
        self.ifname = ifname
        self.sent = []
        self.sock = _FakeSock()
        self._frames = []

    def set_recv_frames(self, frames):
        self._frames = list(frames)

    def close(self):
        return None

    def send(self, can_id, data=None, extended=False, rtr=False, dlc=None):
        self.sent.append(
            {
                "can_id": can_id,
                "data": data,
                "extended": extended,
                "rtr": rtr,
                "dlc": dlc,
            }
        )

    def send_frame(self, frame):
        self.sock.send(frame)

    def recv(self, timeout=None):
        if not self._frames:
            raise OSError("no frames")
        return self._frames.pop(0)


def _make_descriptor(tmp_path):
    descriptor = """
metadata:
  version: '1.0'
ios:
  - &io1
    id: io1
    type: dummy
    instance: 1
    dtype: uint8
protocols:
  - id: can_main
    type: can
    instance: 1
    config:
      node_id: 0
      objects:
        - type: read
          flags: 0
          can_id_start: 16
          count: 1
          bindings: [*io1]
        - type: read_seg
          flags: 0
          can_id_start: 32
          count: 1
          bindings: [*io1]
        - type: read_indexed
          flags: 0
          can_id_start: 48
          count: 1
          bindings: [*io1]
        - type: write
          flags: 0
          can_id_start: 64
          count: 1
          bindings: [*io1]
        - type: write_seg
          flags: 0
          can_id_start: 80
          count: 1
          bindings: [*io1]
        - type: write_indexed
          flags: 0
          can_id_start: 96
          count: 1
          bindings: [*io1]
"""
    path = _write_descriptor(tmp_path, descriptor)
    return load_can_descriptor(path)


@patch("dawnpy_can.client.CANSocket", autospec=True)
def test_can_client_send_and_read(mock_can_socket, tmp_path):
    fake = _FakeCANSocket()
    mock_can_socket.return_value = fake

    desc = _make_descriptor(tmp_path)
    client = CanClient(desc, ifname="can0", extended=False)

    access = next(a for a in desc.get_access("io1") if a.method == "read")
    client._queue.put(
        {
            "can_id": access.can_id,
            "data": b"\x01",
            "rtr": False,
        }
    )
    result = client.read_simple(access, timeout=0.1)
    assert result == b"\x01"
    assert fake.sent[-1]["rtr"] is True

    result = client.read_simple(access, timeout=0.01)
    assert result is None

    client._queue.put(
        {
            "can_id": access.can_id,
            "data": b"\x02",
            "rtr": False,
        }
    )
    result = client.read_push(access, timeout=0.1)
    assert result == b"\x02"
    result = client.read_push(access, timeout=0.01)
    assert result is None

    access_w = next(a for a in desc.get_access("io1") if a.method == "write")
    client.write_simple(access_w, b"\x02")
    assert fake.sent[-1]["can_id"] == access_w.can_id
    assert fake.sent[-1]["data"] == b"\x02"


@patch("dawnpy_can.client.CANSocket", autospec=True)
def test_can_client_segmented_and_indexed(mock_can_socket, tmp_path):
    fake = _FakeCANSocket()
    mock_can_socket.return_value = fake

    desc = _make_descriptor(tmp_path)
    client = CanClient(desc, ifname="can0", extended=False)

    access_seg = next(
        a for a in desc.get_access("io1") if a.method == "read_seg"
    )
    client._queue.put(
        {
            "can_id": access_seg.can_id,
            "data": b"\x00\xaa\xbb",
            "rtr": False,
        }
    )
    client._queue.put(
        {
            "can_id": access_seg.can_id,
            "data": b"\x81\xcc",
            "rtr": False,
        }
    )
    result = client.read_segmented(access_seg, timeout=0.1)
    assert result == b"\xaa\xbb\xcc"
    assert fake.sent[-1]["rtr"] is False
    assert fake.sent[-1]["data"] == b""

    client._queue.put(
        {
            "can_id": access_seg.can_id,
            "data": b"\x02\xaa",
            "rtr": False,
        }
    )
    result = client.read_segmented(access_seg, timeout=0.01)
    assert result is None

    access_idx = next(
        a for a in desc.get_access("io1") if a.method == "read_indexed"
    )
    client._queue.put(
        {
            "can_id": access_idx.can_id,
            "data": b"\x00",
            "rtr": False,
        }
    )
    result = client._recv_segmented(access_idx, timeout=0.01, with_index=True)
    assert result is None
    result = client._recv_segmented(access_idx, timeout=0.0, with_index=True)
    assert result is None

    access_idx = next(
        a for a in desc.get_access("io1") if a.method == "read_indexed"
    )
    client._queue.put(
        {
            "can_id": access_idx.can_id,
            "data": bytes([0x80, access_idx.index, 0x11]),
            "rtr": False,
        }
    )
    result = client.read_indexed(access_idx, timeout=0.1)
    assert result == b"\x11"

    no_index = CanAccess(method="read_indexed", can_id=1, index=None, flags=0)
    assert client.read_indexed(no_index, timeout=0.1) is None

    access_idx_w = next(
        a for a in desc.get_access("io1") if a.method == "write_indexed"
    )
    client.write_indexed(access_idx_w, b"\x01\x02")
    assert fake.sock.sent

    no_index_w = CanAccess(
        method="write_indexed", can_id=1, index=None, flags=0
    )
    fake.sock.sent.clear()
    client.write_indexed(no_index_w, b"\x01")
    assert not fake.sock.sent

    access_seg_w = next(
        a for a in desc.get_access("io1") if a.method == "write_seg"
    )
    client.write_segmented(access_seg_w, b"\x01\x02\x03")
    assert fake.sock.sent


@patch("dawnpy_can.client.CANSocket", autospec=True)
def test_can_client_segmented_seekable_offset_request(
    mock_can_socket, tmp_path
):
    fake = _FakeCANSocket()
    mock_can_socket.return_value = fake

    desc = _make_descriptor(tmp_path)
    client = CanClient(desc, ifname="can0", extended=False)

    access_seg = next(
        a for a in desc.get_access("io1") if a.method == "read_seg"
    )

    client._queue.put(
        {
            "can_id": access_seg.can_id,
            "data": b"\x00\xaa\xbb",
            "rtr": False,
        }
    )
    client._queue.put(
        {
            "can_id": access_seg.can_id,
            "data": b"\x81\xcc",
            "rtr": False,
        }
    )

    result = client.read_segmented(access_seg, timeout=0.1, seekable=True)
    assert result == b"\xaa\xbb\xcc"
    assert fake.sent[-1]["rtr"] is False
    assert fake.sent[-1]["data"] == b""


@patch("dawnpy_can.client.CANSocket", autospec=True)
def test_can_client_recv_loop_updates_heartbeat(mock_can_socket, tmp_path):
    fake = _FakeCANSocket()
    mock_can_socket.return_value = fake

    desc = _make_descriptor(tmp_path)
    hb = HeartbeatState(can_id=0x123, interval_s=1.0, timeout_mult=2)
    client = CanClient(desc, ifname="can0", extended=False, heartbeat=hb)

    fake.set_recv_frames(
        [
            {
                "can_id": 0x123,
                "data": b"\x00",
                "rtr": False,
            }
        ]
    )
    client._recv_loop()
    assert hb.last_seen is not None


@patch("dawnpy_can.client.CANSocket", autospec=True)
def test_can_client_start_close_and_timeout(mock_can_socket, tmp_path):
    fake = _FakeCANSocket()
    mock_can_socket.return_value = fake

    desc = _make_descriptor(tmp_path)
    client = CanClient(desc, ifname="can0", extended=None)

    fake.recv = Mock(side_effect=[socket.timeout(), OSError("stop")])
    client.start()
    time.sleep(0.05)
    client.close()

    client._thread = Mock()
    client._thread.is_alive.return_value = True
    client.close()


@patch("dawnpy_can.client.CANSocket", autospec=True)
def test_can_client_recv_match_buffer(mock_can_socket, tmp_path):
    fake = _FakeCANSocket()
    mock_can_socket.return_value = fake

    desc = _make_descriptor(tmp_path)
    client = CanClient(desc, ifname="can0", extended=False)

    client._buffer_frame({"can_id": 0x999, "data": b"\x00"})
    client._queue.put({"can_id": 0x123, "data": b"\x01"})
    result = client.recv_match(0x123, timeout=0.1)
    assert result["data"] == b"\x01"

    client._buffer_frame({"can_id": 0x123, "data": b"\x02"})
    result = client.recv_match(0x123, timeout=0.1)
    assert result["data"] == b"\x02"

    client._queue.put({"can_id": 0x999, "data": b"\x03"})
    result = client.recv_match(0x123, timeout=0.01)
    assert result is None

    client._buffer.clear()
    client._buffer_max = 1
    client._buffer_frame({"can_id": 0x111, "data": b"\x00"})
    client._buffer_frame({"can_id": 0x112, "data": b"\x00"})
    assert len(client._buffer) == 1

    result = client.recv_match(0x123, timeout=0.0)
    assert result is None


def test_can_access_repr():
    access = CanAccess(method="read", can_id=0x10, index=None, flags=0)
    assert access.can_id == 0x10
