#!/usr/bin/env python3
# tools/dawnpy/src/dawnpy/can/can.py
#
# SPDX-License-Identifier: Apache-2.0
#

"""SocketCAN helpers for Dawn CAN protocol testing and tooling.

The frame-building helpers (``create_can_frame`` / ``parse_can_frame`` /
``build_*_frames``) encode Dawn-specific segmentation conventions and stay
in this module. The actual socket transport delegates to
:mod:`python-can <can>`'s ``Bus``.
"""

import socket
import struct
from typing import Any

import can

# 11-bit / 29-bit CAN id masks. ``socket.CAN_*_MASK`` exists on Linux only,
# so define them locally for cross-platform import safety.
SFF_MASK = 0x000007FF
EFF_MASK = 0x1FFFFFFF


def create_can_frame(  # noqa: C901
    can_id: int,
    data: bytes | list[int] | None = None,
    extended: bool = False,
    rtr: bool = False,
    dlc: int | None = None,
) -> bytes:
    """Create a SocketCAN frame.

    :param can_id: CAN identifier (11-bit for standard, 29-bit for extended)
    :param data: Payload data (bytes or list of integers). None for RTR frames.
    :param extended: Use extended frame format (29-bit ID)
    :param rtr: Remote Transmission Request flag
    :param dlc: Data Length Code (0-8). If None, derived from data length.
    :return: Packed CAN frame ready to send (16 bytes).
    """
    max_id = EFF_MASK if extended else SFF_MASK
    if can_id < 0 or can_id > max_id:
        raise ValueError(
            f"CAN ID 0x{can_id:X} out of range for "
            f"{'extended' if extended else 'standard'} frame "
            f"(max: 0x{max_id:X})"
        )

    if data is None:
        data_bytes = b""
    elif isinstance(data, list):
        data_bytes = bytes(data)
    elif isinstance(data, bytes):
        data_bytes = data
    else:
        raise ValueError("data must be bytes, list of ints, or None")

    if dlc is None:
        dlc = len(data_bytes)

    if dlc < 0 or dlc > 8:
        raise ValueError(f"DLC must be 0-8, got {dlc}")

    if rtr:
        if len(data_bytes) > 0:
            raise ValueError("RTR frames cannot have data payload")
    else:
        if len(data_bytes) > 8:
            raise ValueError(
                f"Data length {len(data_bytes)} exceeds maximum of 8"
            )
        if len(data_bytes) > dlc:
            raise ValueError(
                f"Data length {len(data_bytes)} exceeds specified DLC {dlc}"
            )

    msg = can.Message(
        arbitration_id=can_id,
        data=data_bytes,
        is_extended_id=extended,
        is_remote_frame=rtr,
        dlc=dlc,
        check=False,
    )
    frame_id = msg.arbitration_id
    if msg.is_extended_id:
        frame_id |= socket.CAN_EFF_FLAG
    if msg.is_remote_frame:
        frame_id |= socket.CAN_RTR_FLAG

    padded_data = bytes(msg.data).ljust(8, b"\x00")
    return struct.pack("=IB3x8s", frame_id, msg.dlc, padded_data)


def parse_can_frame(frame: bytes) -> dict[str, Any]:
    """Parse a received SocketCAN frame.

    :param frame: Raw 16-byte CAN frame
    :return: Dictionary with frame information.
    """
    if len(frame) != 16:
        raise ValueError(f"CAN frame must be 16 bytes, got {len(frame)}")

    raw_id, dlc, data = struct.unpack("=IB3x8s", frame)

    extended = bool(raw_id & socket.CAN_EFF_FLAG)
    rtr = bool(raw_id & socket.CAN_RTR_FLAG)
    error = bool(raw_id & socket.CAN_ERR_FLAG)
    can_id = raw_id & EFF_MASK
    actual_data = data[:dlc]

    return {
        "can_id": can_id,
        "dlc": dlc,
        "data": actual_data,
        "extended": extended,
        "rtr": rtr,
        "error": error,
        "raw_id": raw_id,
    }


def segment_payload(payload: bytes, payload_max: int) -> list[bytes]:
    """Split payload into chunks of payload_max bytes."""
    if payload_max <= 0:
        raise ValueError("payload_max must be positive")
    return [
        payload[i : i + payload_max]
        for i in range(0, len(payload), payload_max)
    ]


def build_segmented_frames(
    can_id: int,
    payload: bytes,
    *,
    with_index: bool = False,
    index: int = 0,
    extended: bool = False,
    use_isotp: bool = False,
) -> list[bytes]:
    """Build segmented CAN frames.

    :param can_id: CAN identifier
    :param payload: Data to send
    :param with_index: Include index byte (for indexed access)
    :param index: Index value (0-255)
    :param extended: Use extended CAN ID
    :param use_isotp: Use ISO-TP format (FF/CF) instead of old format
    :return: List of CAN frames

    Old format (use_isotp=False):
        - data[0] = seg | 0x80 (if last)
        - For indexed: data[1] = index, data[2:] = payload

    ISO-TP format (use_isotp=True):
        - First Frame: data[0] = 0x10, data[1] = total_len,
          data[2:] = payload
        - Consecutive Frame: data[0] = 0x20 | seq, data[1:] = payload
        - Note: ISO-TP doesn't support with_index
    """
    if with_index and (index < 0 or index > 0xFF):
        raise ValueError("index must be 0..255")

    if use_isotp:
        if with_index:
            raise ValueError("ISO-TP format doesn't support with_index")
        return build_isotp_frames(can_id, payload, extended=extended)

    payload_max = 6 if with_index else 7
    chunks = segment_payload(payload, payload_max) if payload else [b""]
    frames = []

    for seg_no, chunk in enumerate(chunks):
        last = seg_no == (len(chunks) - 1)
        seg = seg_no | (0x80 if last else 0x00)
        if with_index:
            data = bytes([seg, index]) + chunk
        else:
            data = bytes([seg]) + chunk
        frames.append(create_can_frame(can_id, data, extended=extended))

    return frames


def build_isotp_frames(
    can_id: int, payload: bytes, *, extended: bool = False
) -> list[bytes]:
    """Build ISO-TP formatted CAN frames.

    ISO-TP format:
        - First Frame (FF): data[0] = 0x10, data[1] = total_length,
          data[2:8] = first 6 bytes
        - Consecutive Frame (CF): data[0] = 0x20 | seq (wraps 0-15),
          data[1:8] = next 7 bytes

    :param can_id: CAN identifier
    :param payload: Data to send
    :param extended: Use extended CAN ID
    :return: List of CAN frames in ISO-TP format
    """
    if len(payload) == 0:
        raise ValueError("ISO-TP requires non-empty payload")

    frames = []

    ff_data = bytes([0x10, len(payload)]) + payload[:6]
    frames.append(create_can_frame(can_id, ff_data, extended=extended))

    offset = 6
    seq = 0
    while offset < len(payload):
        chunk = payload[offset : offset + 7]
        cf_data = bytes([0x20 | seq]) + chunk
        frames.append(create_can_frame(can_id, cf_data, extended=extended))
        offset += 7
        seq = (seq + 1) & 0x0F

    return frames


def build_indexed_request(
    can_id: int, index: int, *, extended: bool = False, seg: int = 0x80
) -> bytes:
    """Build indexed read request (seg in data[0], index in data[1])."""
    if index < 0 or index > 0xFF:
        raise ValueError("index must be 0..255")
    data = bytes([seg, index])
    return create_can_frame(can_id, data, extended=extended)


class CANSocket:
    """SocketCAN wrapper backed by :mod:`python-can`."""

    def __init__(self, ifname: str = "can0") -> None:
        """Initialize SocketCAN socket.

        :param ifname: CAN interface name (default: can0)
        """
        self.ifname = ifname
        self.bus: can.BusABC = can.interface.Bus(
            interface="socketcan",
            channel=ifname,
            receive_own_messages=False,
        )

    def close(self) -> None:
        """Close the socket."""
        self.bus.shutdown()

    def send(
        self,
        can_id: int,
        data: bytes | list[int] | None = None,
        extended: bool = False,
        rtr: bool = False,
        dlc: int | None = None,
    ) -> None:
        """Send a CAN frame.

        :param can_id: CAN identifier
        :param data: Payload data
        :param extended: Use extended frame format
        :param rtr: Remote Transmission Request flag
        :param dlc: Data Length Code
        """
        if data is None:
            payload = b""
        elif isinstance(data, list):
            payload = bytes(data)
        else:
            payload = data

        msg = can.Message(
            arbitration_id=can_id,
            data=payload,
            is_extended_id=extended,
            is_remote_frame=rtr,
            dlc=len(payload) if dlc is None else dlc,
        )
        self.bus.send(msg)

    def send_frame(self, frame: bytes) -> None:
        """Send a pre-built raw CAN frame (16 bytes)."""
        if len(frame) != 16:
            raise ValueError(f"CAN frame must be 16 bytes, got {len(frame)}")
        parsed = parse_can_frame(frame)
        msg = can.Message(
            arbitration_id=parsed["can_id"],
            data=parsed["data"],
            is_extended_id=parsed["extended"],
            is_remote_frame=parsed["rtr"],
            dlc=parsed["dlc"],
        )
        self.bus.send(msg)

    def recv(self, timeout: float | None = None) -> dict[str, Any]:
        """Receive and parse a CAN frame.

        :param timeout: Optional receive timeout in seconds
        :return: Parsed CAN frame dictionary
        :raises socket.timeout: when no frame arrives within ``timeout``
        """
        msg = self.bus.recv(timeout=timeout)
        if msg is None:
            raise TimeoutError("CAN recv timed out")

        raw_id = msg.arbitration_id
        if msg.is_extended_id:
            raw_id |= socket.CAN_EFF_FLAG
        if msg.is_remote_frame:
            raw_id |= socket.CAN_RTR_FLAG
        if msg.is_error_frame:
            raw_id |= socket.CAN_ERR_FLAG

        return {
            "can_id": msg.arbitration_id,
            "dlc": msg.dlc,
            "data": bytes(msg.data),
            "extended": msg.is_extended_id,
            "rtr": msg.is_remote_frame,
            "error": msg.is_error_frame,
            "raw_id": raw_id,
        }
