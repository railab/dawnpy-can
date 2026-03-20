# tools/dawnpy/tests/test_can.py
#
# SPDX-License-Identifier: Apache-2.0
#

"""Tests for CAN frame creation and parsing."""

from unittest.mock import Mock, patch

import can
import pytest

from dawnpy_can.can import (
    EFF_MASK,
    SFF_MASK,
    create_can_frame,
    parse_can_frame,
)
from dawnpy_can.descriptor import _parse_int_default0


def test_parse_int_default0():
    assert _parse_int_default0(True) == 0
    assert _parse_int_default0(5) == 5
    assert _parse_int_default0("0x10") == 16
    assert _parse_int_default0("CONFIG_X") == 0
    assert _parse_int_default0([]) == 0


class TestCreateCanFrame:
    """Tests for create_can_frame function."""

    def test_create_standard_frame(self):
        """Test creating a standard CAN frame."""
        frame = create_can_frame(can_id=0x123, data=b"\x01\x02\x03")

        assert len(frame) == 16
        parsed = parse_can_frame(frame)
        assert parsed["can_id"] == 0x123
        assert parsed["data"] == b"\x01\x02\x03"
        assert not parsed["extended"]
        assert not parsed["rtr"]

    def test_pack_extended_frame(self):
        """Test packing an extended CAN frame."""
        frame = create_can_frame(
            can_id=0x12345678, data=b"\x11\x22", extended=True
        )

        assert len(frame) == 16
        parsed = parse_can_frame(frame)
        assert parsed["can_id"] == 0x12345678
        assert parsed["data"] == b"\x11\x22"
        assert parsed["extended"]
        assert not parsed["rtr"]

    def test_pack_rtr_frame(self):
        """Test packing an RTR frame."""
        frame = create_can_frame(can_id=0x100, rtr=True, dlc=4)

        assert len(frame) == 16
        parsed = parse_can_frame(frame)
        assert parsed["can_id"] == 0x100
        # RTR frames return padding bytes up to DLC
        assert parsed["data"] == b"\x00\x00\x00\x00"
        assert parsed["rtr"]
        assert parsed["dlc"] == 4

    def test_pack_with_explicit_dlc(self):
        """Test packing with explicit DLC."""
        frame = create_can_frame(can_id=0x200, data=b"\xaa", dlc=8)

        parsed = parse_can_frame(frame)
        assert parsed["dlc"] == 8
        # Data is padded to DLC length
        assert len(parsed["data"]) == 8
        assert parsed["data"] == b"\xaa" + b"\x00" * 7

    def test_pack_data_as_list(self):
        """Test packing with data as list of ints."""
        frame = create_can_frame(can_id=0x300, data=[0x11, 0x22, 0x33])

        parsed = parse_can_frame(frame)
        assert parsed["data"] == b"\x11\x22\x33"

    def test_pack_no_data(self):
        """Test packing with no data."""
        frame = create_can_frame(can_id=0x400)

        parsed = parse_can_frame(frame)
        assert parsed["data"] == b""
        assert parsed["dlc"] == 0

    def test_pack_none_data(self):
        """Test packing with None data."""
        frame = create_can_frame(can_id=0x500, data=None)

        parsed = parse_can_frame(frame)
        assert parsed["data"] == b""

    def test_pack_max_data(self):
        """Test packing with maximum 8 bytes of data."""
        frame = create_can_frame(
            can_id=0x600, data=b"\x00\x11\x22\x33\x44\x55\x66\x77"
        )

        parsed = parse_can_frame(frame)
        assert len(parsed["data"]) == 8
        assert parsed["data"] == b"\x00\x11\x22\x33\x44\x55\x66\x77"

    def test_pack_standard_id_out_of_range(self):
        """Test error when standard CAN ID is out of range."""
        with pytest.raises(ValueError, match="CAN ID.*out of range"):
            create_can_frame(can_id=0x800, extended=False)

    def test_pack_extended_id_out_of_range(self):
        """Test error when extended CAN ID is out of range."""
        with pytest.raises(ValueError, match="CAN ID.*out of range"):
            create_can_frame(can_id=0x20000000, extended=True)

    def test_pack_negative_id(self):
        """Test error with negative CAN ID."""
        with pytest.raises(ValueError, match="CAN ID.*out of range"):
            create_can_frame(can_id=-1)

    def test_pack_invalid_data_type(self):
        """Test error with invalid data type."""
        with pytest.raises(ValueError, match="data must be bytes"):
            create_can_frame(can_id=0x100, data="invalid")  # type: ignore

    def test_pack_dlc_out_of_range_low(self):
        """Test error when DLC is negative."""
        with pytest.raises(ValueError, match="DLC must be 0-8"):
            create_can_frame(can_id=0x100, dlc=-1)

    def test_pack_dlc_out_of_range_high(self):
        """Test error when DLC is > 8."""
        with pytest.raises(ValueError, match="DLC must be 0-8"):
            create_can_frame(can_id=0x100, dlc=9)

    def test_pack_rtr_with_data(self):
        """Test error when RTR frame has data payload."""
        with pytest.raises(ValueError, match="RTR frames cannot have data"):
            create_can_frame(can_id=0x100, data=b"\x11", rtr=True)

    def test_pack_data_too_long(self):
        """Test error when data exceeds 8 bytes."""
        # Data > 8 bytes auto-calculates DLC to 9, triggering DLC error
        with pytest.raises(ValueError, match="DLC must be 0-8"):
            create_can_frame(can_id=0x100, data=b"\x00" * 9)

    def test_pack_data_too_long_with_explicit_dlc(self):
        """Test payload length before padding."""
        with pytest.raises(ValueError, match="Data length 9 exceeds maximum"):
            create_can_frame(can_id=0x100, data=b"\x00" * 9, dlc=8)

    def test_pack_data_exceeds_dlc(self):
        """Test error when data length exceeds specified DLC."""
        with pytest.raises(
            ValueError, match="Data length.*exceeds specified DLC"
        ):
            create_can_frame(can_id=0x100, data=b"\x11\x22", dlc=1)


class TestParseCanFrame:
    """Tests for parse_can_frame function."""

    def test_parse_invalid_frame_length(self):
        """Test error when parsing frame with invalid length."""
        with pytest.raises(ValueError, match="CAN frame must be 16 bytes"):
            parse_can_frame(b"\x00" * 15)  # Too short

    def test_parse_standard_frame(self):
        """Test parsing a standard CAN frame."""
        # Create a frame manually
        frame = create_can_frame(can_id=0x456, data=b"\xaa\xbb\xcc")

        parsed = parse_can_frame(frame)
        assert parsed["can_id"] == 0x456
        assert parsed["data"] == b"\xaa\xbb\xcc"
        assert parsed["dlc"] == 3
        assert not parsed["extended"]
        assert not parsed["rtr"]
        assert not parsed["error"]

    def test_parse_extended_frame(self):
        """Test parsing an extended CAN frame."""
        frame = create_can_frame(
            can_id=0x1FFFFFFF, extended=True, data=b"\xff"
        )

        parsed = parse_can_frame(frame)
        assert parsed["can_id"] == 0x1FFFFFFF
        assert parsed["extended"]

    def test_parse_rtr_frame(self):
        """Test parsing an RTR frame."""
        frame = create_can_frame(can_id=0x700, rtr=True, dlc=4)

        parsed = parse_can_frame(frame)
        assert parsed["rtr"]
        # RTR frames return padding bytes up to DLC
        assert parsed["data"] == b"\x00\x00\x00\x00"
        assert parsed["dlc"] == 4

    def test_parse_zero_dlc(self):
        """Test parsing frame with zero DLC."""
        frame = create_can_frame(can_id=0x7FF, data=b"", dlc=0)

        parsed = parse_can_frame(frame)
        assert parsed["dlc"] == 0
        # Empty slice returns empty bytes
        assert parsed["data"] == b""


class TestCANConstants:
    """Tests for CAN id mask values."""

    def test_can_masks(self):
        """Test CAN mask values."""
        assert SFF_MASK == 0x7FF
        assert EFF_MASK == 0x1FFFFFFF


class TestCANAdvancedFeatures:
    """Tests for advanced CAN features (segmentation, ISO-TP, etc)."""

    def test_segment_payload(self):
        """Test payload segmentation."""
        from dawnpy_can.can import segment_payload

        payload = b"\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a"
        segments = segment_payload(payload, 4)

        assert len(segments) == 3
        assert segments[0] == b"\x01\x02\x03\x04"
        assert segments[1] == b"\x05\x06\x07\x08"
        assert segments[2] == b"\x09\x0a"

    def test_segment_payload_exact_fit(self):
        """Test segmentation when payload fits exactly."""
        from dawnpy_can.can import segment_payload

        payload = b"\x01\x02\x03\x04\x05\x06\x07\x08"
        segments = segment_payload(payload, 4)

        assert len(segments) == 2
        assert segments[0] == b"\x01\x02\x03\x04"
        assert segments[1] == b"\x05\x06\x07\x08"

    def test_segment_payload_invalid_max(self):
        """Test error when payload_max is invalid."""
        from dawnpy_can.can import segment_payload

        with pytest.raises(ValueError, match="payload_max must be positive"):
            segment_payload(b"\x01\x02", 0)

    def test_build_segmented_frames(self):
        """Test building segmented CAN frames."""
        from dawnpy_can.can import build_segmented_frames

        payload = b"\x01\x02\x03\x04\x05\x06\x07\x08\x09"
        frames = build_segmented_frames(
            can_id=0x100, payload=payload, extended=False
        )

        # Should have 2 frames (7 bytes + 2 bytes with old format)
        assert len(frames) == 2
        # Each frame should be 16 bytes
        assert all(len(f) == 16 for f in frames)

    def test_build_segmented_frames_with_index(self):
        """Test building segmented frames with index."""
        from dawnpy_can.can import build_segmented_frames

        payload = b"\x01\x02\x03\x04\x05\x06\x07\x08"
        frames = build_segmented_frames(
            can_id=0x200, payload=payload, with_index=True, index=0x42
        )

        # Should have 2 frames (6 bytes + 2 bytes with index format)
        assert len(frames) == 2
        # Check that index is included in frames
        for frame in frames:
            parsed = parse_can_frame(frame)
            assert parsed["data"][1] == 0x42  # Index byte

    def test_build_segmented_frames_invalid_index(self):
        """Test error when index is out of range."""
        from dawnpy_can.can import build_segmented_frames

        with pytest.raises(ValueError, match="index must be 0..255"):
            build_segmented_frames(
                can_id=0x100, payload=b"\x01", with_index=True, index=256
            )

    def test_build_segmented_frames_isotp(self):
        """Test building segmented frames with ISO-TP format."""
        from dawnpy_can.can import build_segmented_frames

        payload = b"\x01\x02\x03\x04\x05\x06\x07\x08"
        frames = build_segmented_frames(
            can_id=0x300, payload=payload, use_isotp=True
        )

        # Should build ISO-TP formatted frames
        assert len(frames) >= 1
        # First frame should have ISO-TP header
        first = parse_can_frame(frames[0])
        assert first["data"][0] == 0x10  # First Frame PCI

    def test_build_segmented_frames_isotp_with_index_error(self):
        """Test error when using ISO-TP with index."""
        from dawnpy_can.can import build_segmented_frames

        with pytest.raises(
            ValueError, match="ISO-TP format doesn't support with_index"
        ):
            build_segmented_frames(
                can_id=0x100,
                payload=b"\x01",
                use_isotp=True,
                with_index=True,
            )

    def test_build_isotp_frames(self):
        """Test building ISO-TP frames."""
        from dawnpy_can.can import build_isotp_frames

        payload = b"\x01\x02\x03\x04\x05"
        frames = build_isotp_frames(
            can_id=0x200, payload=payload, extended=False
        )

        # Single frame for payload <= 7 bytes
        assert len(frames) >= 1
        assert all(len(f) == 16 for f in frames)

    def test_build_isotp_frames_multiframe(self):
        """Test building multi-frame ISO-TP."""
        from dawnpy_can.can import build_isotp_frames

        # Payload > 7 bytes requires multiple frames
        payload = b"\x00" * 20
        frames = build_isotp_frames(
            can_id=0x300, payload=payload, extended=False
        )

        # Should have multiple frames for 20 bytes
        assert len(frames) > 1

    def test_build_isotp_frames_empty_payload_error(self):
        """Test error when ISO-TP payload is empty."""
        from dawnpy_can.can import build_isotp_frames

        with pytest.raises(
            ValueError, match="ISO-TP requires non-empty payload"
        ):
            build_isotp_frames(can_id=0x100, payload=b"", extended=False)

    def test_build_indexed_request(self):
        """Test building indexed CAN request."""
        from dawnpy_can.can import build_indexed_request

        frame = build_indexed_request(can_id=0x400, index=0x12, extended=False)

        assert len(frame) == 16
        parsed = parse_can_frame(frame)
        assert parsed["can_id"] == 0x400
        # Request format: data[0] = seg (0x80), data[1] = index
        assert parsed["data"][0] == 0x80
        assert parsed["data"][1] == 0x12

    def test_build_indexed_request_custom_seg(self):
        """Test indexed request with custom seg byte."""
        from dawnpy_can.can import build_indexed_request

        frame = build_indexed_request(can_id=0x500, index=0x01, seg=0x00)

        assert len(frame) == 16
        parsed = parse_can_frame(frame)
        assert parsed["data"][0] == 0x00
        assert parsed["data"][1] == 0x01

    def test_build_indexed_request_invalid_index(self):
        """Test error when index is out of range."""
        from dawnpy_can.can import build_indexed_request

        with pytest.raises(ValueError, match="index must be 0..255"):
            build_indexed_request(can_id=0x100, index=256)


class TestCANSocket:
    """Tests for CANSocket class (python-can-backed)."""

    @patch("dawnpy_can.can.can.interface.Bus")
    def test_init(self, mock_bus_class):
        """Test CANSocket initialization."""
        from dawnpy_can.can import CANSocket

        mock_bus = Mock()
        mock_bus_class.return_value = mock_bus

        can_sock = CANSocket(ifname="can0")

        mock_bus_class.assert_called_once_with(
            interface="socketcan",
            channel="can0",
            receive_own_messages=False,
        )
        assert can_sock.ifname == "can0"
        assert can_sock.bus is mock_bus

    @patch("dawnpy_can.can.can.interface.Bus")
    def test_close(self, mock_bus_class):
        """Test CANSocket close."""
        from dawnpy_can.can import CANSocket

        mock_bus = Mock()
        mock_bus_class.return_value = mock_bus

        can_sock = CANSocket(ifname="can0")
        can_sock.close()

        mock_bus.shutdown.assert_called_once()

    @patch("dawnpy_can.can.can.interface.Bus")
    def test_send(self, mock_bus_class):
        """Test CANSocket send."""
        from dawnpy_can.can import CANSocket

        mock_bus = Mock()
        mock_bus_class.return_value = mock_bus

        can_sock = CANSocket(ifname="can0")
        can_sock.send(can_id=0x123, data=b"\x01\x02", extended=False)

        mock_bus.send.assert_called_once()
        sent_msg = mock_bus.send.call_args[0][0]
        assert isinstance(sent_msg, can.Message)
        assert sent_msg.arbitration_id == 0x123
        assert bytes(sent_msg.data) == b"\x01\x02"
        assert sent_msg.is_extended_id is False

    @patch("dawnpy_can.can.can.interface.Bus")
    def test_send_frame(self, mock_bus_class):
        """Test CANSocket send_frame."""
        from dawnpy_can.can import CANSocket

        mock_bus = Mock()
        mock_bus_class.return_value = mock_bus

        can_sock = CANSocket(ifname="can0")
        frame = create_can_frame(can_id=0x123, data=b"\x01")
        can_sock.send_frame(frame)

        mock_bus.send.assert_called_once()
        sent_msg = mock_bus.send.call_args[0][0]
        assert isinstance(sent_msg, can.Message)
        assert sent_msg.arbitration_id == 0x123
        assert bytes(sent_msg.data) == b"\x01"

    @patch("dawnpy_can.can.can.interface.Bus")
    def test_send_frame_invalid_length(self, mock_bus_class):
        """Test CANSocket send_frame with invalid length."""
        from dawnpy_can.can import CANSocket

        mock_bus = Mock()
        mock_bus_class.return_value = mock_bus

        can_sock = CANSocket(ifname="can0")
        with pytest.raises(ValueError, match="CAN frame must be 16 bytes"):
            can_sock.send_frame(b"\x00" * 15)

    @patch("dawnpy_can.can.can.interface.Bus")
    def test_recv(self, mock_bus_class):
        """Test CANSocket receive."""
        from dawnpy_can.can import CANSocket

        mock_bus = Mock()
        mock_bus_class.return_value = mock_bus
        mock_bus.recv.return_value = can.Message(
            arbitration_id=0x456,
            data=b"\xaa\xbb",
            is_extended_id=False,
            dlc=2,
        )

        can_sock = CANSocket(ifname="can0")
        result = can_sock.recv()

        mock_bus.recv.assert_called_once_with(timeout=None)
        assert result["can_id"] == 0x456
        assert result["data"] == b"\xaa\xbb"

    @patch("dawnpy_can.can.can.interface.Bus")
    def test_recv_with_timeout(self, mock_bus_class):
        """Test CANSocket receive with timeout."""
        from dawnpy_can.can import CANSocket

        mock_bus = Mock()
        mock_bus_class.return_value = mock_bus
        mock_bus.recv.return_value = can.Message(
            arbitration_id=0x789,
            data=b"\xff",
            is_extended_id=False,
            dlc=1,
        )

        can_sock = CANSocket(ifname="can0")
        result = can_sock.recv(timeout=1.0)

        mock_bus.recv.assert_called_once_with(timeout=1.0)
        assert result["can_id"] == 0x789
        assert result["data"] == b"\xff"
