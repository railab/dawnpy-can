# tools/dawnpy/src/dawnpy/can/descriptor.py
#
# SPDX-License-Identifier: Apache-2.0
#

"""CAN descriptor parsing utilities for dawnpy."""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from dawnpy.descriptor.client import (
    ClientDescriptor,
    ClientIo,
    load_client_descriptor,
)
from dawnpy.descriptor.support.mapping import resolve_objects_with_bindings


@dataclass(frozen=True)
class CanAccess:
    """CAN access method for a single IO."""

    method: str
    can_id: int
    index: int | None
    flags: int


@dataclass(frozen=True)
class CanBlockUsage:
    """CAN ID usage information for a single protocol object block."""

    label: str
    can_ids: list[int]


@dataclass
class CanDescriptor:
    """Parsed CAN descriptor information."""

    client: ClientDescriptor
    access_map: dict[str, list[CanAccess]]
    block_usage: list[CanBlockUsage]
    node_id: int
    proto_id: str

    def get_io(self, io_id: str) -> ClientIo | None:
        """Return IO metadata by ID."""
        return self.client.get_io(io_id)

    def get_access(self, io_id: str) -> list[CanAccess]:
        """Return CAN access list for an IO."""
        return self.access_map.get(io_id, [])

    def get_tagged_ios(self, tag: str) -> list[ClientIo]:
        """Return IOs matching a tag."""
        return self.client.get_tagged_ios(tag)

    def uses_extended_ids(self) -> bool:
        """Return True if any access uses extended CAN IDs."""
        for accesses in self.access_map.values():
            for access in accesses:
                if access.can_id > 0x7FF:
                    return True
        return False


def iter_conflict_keys(
    desc: CanDescriptor,
) -> Iterable[tuple[int, str]]:
    """Yield CAN ID keys for overlap checks."""
    """Yield (key, item_label) tuples for CAN overlap conflict checks."""
    for block in desc.block_usage:
        for can_id in block.can_ids:
            yield (can_id, block.label)


def load_can_descriptor(
    yaml_path: str,
    kconfig_path: str | None = None,
    kconfig_overrides: dict[str, Any] | None = None,
) -> CanDescriptor:
    """Load descriptor.yaml and build CAN mapping."""
    client = load_client_descriptor(
        yaml_path,
        kconfig_path=kconfig_path,
        kconfig_overrides=kconfig_overrides,
    )
    proto_can = client.get_protocol("can")
    if not proto_can:
        raise ValueError("No CAN protocol entry found in descriptor")

    node_id = _parse_int_default0(proto_can.config.get("node_id", 0))
    access_map: dict[str, list[CanAccess]] = {}
    block_usage: list[CanBlockUsage] = []

    for obj_idx, obj in enumerate(
        resolve_objects_with_bindings(proto_can.config, key="objects")
    ):
        method = str(obj.get("type", "")).lower()
        can_id_start = _parse_int_default0(obj.get("can_id_start", 0))
        flags = _parse_int_default0(obj.get("flags", 0))
        bindings = obj.get("bindings_resolved", [])
        base_can_id = node_id + can_id_start
        binding_count = len(bindings)

        # CAN ID reservation policy for overlap checks:
        # - indexed methods use a single CAN ID shared by all bindings
        # - direct/segmented methods reserve one CAN ID per resolved binding.
        if method in ("read_indexed", "write_indexed"):
            has_group = binding_count > 0
            block_ids = [base_can_id] if has_group else []
        else:
            block_ids = [base_can_id + i for i in range(binding_count)]

        for idx, io_id in enumerate(bindings):
            if method in ("read_indexed", "write_indexed"):
                can_id = base_can_id
                index = idx + 1
            else:
                can_id = base_can_id + idx
                index = None

            access = CanAccess(
                method=method,
                can_id=can_id,
                index=index,
                flags=flags,
            )
            access_map.setdefault(io_id, []).append(access)
        block_usage.append(
            CanBlockUsage(
                label=f"obj{obj_idx}:{method}:0x{base_can_id:X}",
                can_ids=block_ids,
            )
        )

    return CanDescriptor(
        client=client,
        access_map=access_map,
        block_usage=block_usage,
        node_id=node_id,
        proto_id=proto_can.proto_id,
    )


def _parse_int_default0(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 0)
        except ValueError:
            return 0
    return 0
