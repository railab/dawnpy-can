#!/usr/bin/env python3
# tools/dawnpy/src/dawnpy/can/client.py
#
# SPDX-License-Identifier: Apache-2.0
#

"""Interactive CAN client utilities for Dawn CAN protocol."""

from __future__ import annotations

import queue
import struct
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from dawnpy.device.decode import normalize_dtype

from dawnpy_can.can import (
    CANSocket,
    build_isotp_frames,
    build_segmented_frames,
)

if TYPE_CHECKING:
    from dawnpy_can.descriptor import CanAccess, CanDescriptor

READ_METHODS = ("read", "read_seg", "read_indexed", "push")
WRITE_METHODS = ("write", "write_seg", "write_indexed")


@dataclass
class HeartbeatState:
    """Tracks heartbeat timing for a CAN device."""

    can_id: int
    interval_s: float
    timeout_mult: int
    last_seen: float | None = None

    def mark(self, ts: float) -> None:
        """Record a heartbeat timestamp."""
        self.last_seen = ts

    def is_live(self, now: float | None = None) -> bool:
        """Return True if the heartbeat is within timeout."""
        if self.last_seen is None:
            return False
        if now is None:
            now = time.time()
        timeout = self.interval_s * max(self.timeout_mult, 1)
        return (now - self.last_seen) <= timeout


class CanClient:
    """CAN protocol client for descriptor-based access."""

    def __init__(
        self,
        descriptor: CanDescriptor,
        ifname: str = "can0",
        extended: bool | None = None,
        heartbeat: HeartbeatState | None = None,
    ) -> None:
        """Initialize CAN client."""
        self.descriptor = descriptor
        self.ifname = ifname
        self.extended = (
            descriptor.uses_extended_ids() if extended is None else extended
        )
        self.heartbeat = heartbeat
        self.socket = CANSocket(ifname)
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._buffer_max = 256
        self._buffer: deque[dict[str, Any]] = deque(maxlen=self._buffer_max)
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._recv_loop, name="dawnpy-can-recv", daemon=True
        )

    def start(self) -> None:
        """Start background receive loop."""
        self._thread.start()

    def close(self) -> None:
        """Stop background receive loop and close socket."""
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self.socket.close()

    def _recv_loop(self) -> None:
        while not self._stop.is_set():
            try:
                frame = self.socket.recv(timeout=0.2)
            except TimeoutError:
                continue
            except OSError:
                break

            if self.heartbeat and frame.get("can_id") == self.heartbeat.can_id:
                self.heartbeat.mark(time.time())

            self._queue.put(frame)

    def _buffer_frame(self, frame: dict[str, Any]) -> None:
        if self._buffer.maxlen != self._buffer_max:
            self._buffer = deque(self._buffer, maxlen=self._buffer_max)
        self._buffer.append(frame)

    def recv_match(
        self,
        can_id: int,
        timeout: float,
        predicate: Callable[[dict[str, Any]], bool] | None = None,
    ) -> dict[str, Any] | None:
        """Receive first frame matching CAN ID and predicate."""
        end = time.time() + timeout

        if self._buffer:
            buffered = list(self._buffer)
            self._buffer.clear()
            for frame in buffered:
                if frame.get("can_id") == can_id and (
                    predicate is None or predicate(frame)
                ):
                    return frame
                self._buffer_frame(frame)

        while time.time() < end:
            remaining = max(0.0, end - time.time())
            try:
                frame = self._queue.get(timeout=remaining)
            except queue.Empty:
                return None

            if frame.get("can_id") == can_id and (
                predicate is None or predicate(frame)
            ):
                return frame

            self._buffer_frame(frame)

        return None

    def send_rtr(self, can_id: int, dlc: int = 0) -> None:
        """Send a remote transmission request."""
        self.socket.send(
            can_id, data=None, extended=self.extended, rtr=True, dlc=dlc
        )

    def send_data(self, can_id: int, data: bytes) -> None:
        """Send a data frame."""
        self.socket.send(can_id, data=data, extended=self.extended, rtr=False)

    def read_simple(
        self, access: CanAccess, timeout: float = 1.0
    ) -> bytes | None:
        """Read a single-frame value using RTR."""
        self.send_rtr(access.can_id)
        frame = self.recv_match(
            access.can_id,
            timeout=timeout,
            predicate=lambda f: not f.get("rtr", False),
        )
        if not frame:
            return None
        return bytes(frame.get("data", b""))

    def read_push(
        self, access: CanAccess, timeout: float = 1.0
    ) -> bytes | None:
        """Read a pushed value without RTR."""
        frame = self.recv_match(
            access.can_id,
            timeout=timeout,
            predicate=lambda f: not f.get("rtr", False),
        )
        if not frame:
            return None
        return bytes(frame.get("data", b""))

    def read_segmented(
        self,
        access: CanAccess,
        timeout: float = 1.0,
        with_index: bool = False,
        seekable: bool = False,
    ) -> bytes | None:
        """Read a segmented value using data request (len=0)."""
        _ = seekable
        self.send_data(access.can_id, b"")
        return self._recv_segmented(
            access,
            timeout=timeout,
            with_index=with_index,
        )

    def read_indexed(
        self, access: CanAccess, timeout: float = 1.0
    ) -> bytes | None:
        """Read a segmented value with index."""
        if access.index is None:
            return None
        data = bytes([0x80, access.index])
        self.send_data(access.can_id, data)
        return self._recv_segmented(
            access,
            timeout=timeout,
            with_index=True,
        )

    def _recv_segmented(
        self,
        access: CanAccess,
        timeout: float,
        with_index: bool,
    ) -> bytes | None:
        end = time.time() + timeout
        parts: list[bytes] = []
        expected_seg = 0

        while time.time() < end:
            remaining = max(0.0, end - time.time())

            def pred(frame: dict[str, Any]) -> bool:
                data = frame.get("data", b"")
                if len(data) < (2 if with_index else 1):
                    return False
                if with_index:
                    return access.index is not None and data[1] == access.index
                return True

            frame = self.recv_match(access.can_id, remaining, predicate=pred)
            if not frame:
                return None

            data = bytes(frame.get("data", b""))
            seg = data[0]
            seg_no = seg & 0x7F
            seg_last = (seg & 0x80) != 0

            if seg_no != expected_seg:
                parts = []
                expected_seg = 0
                continue

            payload = data[2:] if with_index else data[1:]
            parts.append(payload)
            expected_seg += 1

            if seg_last:
                return b"".join(parts)

        return None

    def write_simple(self, access: CanAccess, data: bytes) -> None:
        """Write a single-frame value."""
        self.send_data(access.can_id, data)

    def write_segmented(self, access: CanAccess, data: bytes) -> None:
        """Write a segmented value using ISO-TP."""
        frames = build_isotp_frames(
            access.can_id, data, extended=self.extended
        )
        for frame in frames:
            self.socket.send_frame(frame)

    def write_indexed(self, access: CanAccess, data: bytes) -> None:
        """Write a segmented value with index."""
        if access.index is None:
            return
        frames = build_segmented_frames(
            access.can_id,
            data,
            with_index=True,
            index=access.index,
            extended=self.extended,
            use_isotp=False,
        )
        for frame in frames:
            self.socket.send_frame(frame)


def pack_value(dtype: str, value: Any) -> bytes | None:
    """Pack a Python value into bytes for CAN."""
    dtype = normalize_dtype(dtype)

    if dtype == "bool":
        val = 1 if str(value).lower() in ("1", "true", "yes", "on") else 0
        return struct.pack("B", val)

    if dtype in ("float", "double"):
        fmt = "<f" if dtype == "float" else "<d"
        return struct.pack(fmt, float(value))

    if dtype == "char":
        if isinstance(value, bytes):
            return value
        return str(value).encode("utf-8")

    fmt_map = {
        "int8": "b",
        "uint8": "B",
        "int16": "<h",
        "uint16": "<H",
        "int32": "<i",
        "uint32": "<I",
        "int64": "<q",
        "uint64": "<Q",
    }
    fmt_int = fmt_map.get(dtype)
    if fmt_int is None:
        return None
    return struct.pack(fmt_int, int(value))


def unpack_value(dtype: str, data: bytes) -> Any | None:
    """Unpack bytes into a Python value for CAN."""
    dtype = normalize_dtype(dtype)

    fmt_map = {
        "bool": "B",
        "int8": "b",
        "uint8": "B",
        "int16": "<h",
        "uint16": "<H",
        "int32": "<i",
        "uint32": "<I",
        "int64": "<q",
        "uint64": "<Q",
        "float": "<f",
        "double": "<d",
    }
    fmt = fmt_map.get(dtype)
    if not fmt:
        return None
    try:
        return struct.unpack(fmt, data[: struct.calcsize(fmt)])[0]
    except struct.error:
        return None
