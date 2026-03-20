#!/usr/bin/env python3
# tools/dawnpy/src/dawnpy/can/console.py
#
# SPDX-License-Identifier: Apache-2.0
#

"""Interactive CAN console for Dawn devices."""

from collections.abc import Callable, Iterable

from dawnpy.cli.console_base import ConsoleBase
from dawnpy.cli.device_registry import DeviceConflictError
from dawnpy.cli.multi_device import MultiDeviceManager
from dawnpy.cli.table import print_table
from dawnpy.descriptor.client import find_descriptor_path
from dawnpy.descriptor.definitions.summary import (
    ObjectIdResolver,
    build_io_table,
)
from dawnpy.descriptor.encoding.proto_caps import validate_descriptor_args
from dawnpy.descriptor.validation.conflicts import check_key_conflicts

from dawnpy_can.client import (
    CanClient,
    HeartbeatState,
    pack_value,
    unpack_value,
)
from dawnpy_can.descriptor import (
    CanAccess,
    CanDescriptor,
    iter_conflict_keys,
    load_can_descriptor,
)


class CanConsole(ConsoleBase):  # pragma: no cover
    """Interactive CAN console."""

    def __init__(
        self,
        descriptor_paths: list[str],
        ifname: str = "can0",
        extended: bool | None = None,
        heartbeat_mult: int = 3,
        heartbeat_default_s: float = 1.0,
        kconfig_path: str | None = None,
        kconfig_overrides: list[dict[str, object]] | None = None,
    ) -> None:
        """Initialize CAN console."""
        validate_descriptor_args("can", descriptor_paths)
        self.descriptor_paths = [
            find_descriptor_path(path) for path in descriptor_paths
        ]
        self.descriptors = []
        for idx, path in enumerate(self.descriptor_paths):
            overrides = None
            if kconfig_overrides:
                overrides = kconfig_overrides[idx]
            self.descriptors.append(
                load_can_descriptor(
                    path,
                    kconfig_path=kconfig_path,
                    kconfig_overrides=overrides,
                )
            )
        conflicts = check_key_conflicts(
            [
                (path, list(self._conflict_keys(desc)))
                for path, desc in zip(
                    self.descriptor_paths, self.descriptors, strict=False
                )
            ]
        )
        if conflicts:
            raise DeviceConflictError(conflicts)
        self.ifname = ifname
        super().__init__(
            prompt="\nEnter command (h for help): ",
            history_file=".dawnpy_can_history",
        )
        self.active_idx = 0
        self.objid_resolver = ObjectIdResolver()
        self._objid_maps = [
            self._build_objid_map(desc) for desc in self.descriptors
        ]
        self.heartbeats: dict[int, HeartbeatState | None] = {}
        self._init_heartbeats(heartbeat_mult, heartbeat_default_s)
        self.clients = [
            CanClient(
                descriptor=desc,
                ifname=self.ifname,
                extended=extended,
                heartbeat=self.heartbeats[idx],
            )
            for idx, desc in enumerate(self.descriptors)
        ]
        self.multi = MultiDeviceManager(self.descriptor_paths)
        for idx, hb in self.heartbeats.items():
            self.multi.set_heartbeat(idx, hb)
        self._check_can_id_conflicts()

    def _init_heartbeats(
        self, heartbeat_mult: int, heartbeat_default_s: float
    ) -> None:
        """Initialize heartbeat tracking from descriptors."""
        for idx, desc in enumerate(self.descriptors):
            tagged = desc.get_tagged_ios("heartbeat")
            if not tagged:
                self.heartbeats[idx] = None
                continue
            if len(tagged) > 1:
                names = ", ".join(io.io_id for io in tagged)
                print(
                    f"ERROR: Multiple heartbeat tags found in "
                    f"{self.descriptor_paths[idx]}: {names}. "
                    "Use a single heartbeat IO."
                )
                self.heartbeats[idx] = None
                continue
            hb_io = tagged[0]
            interval_us = hb_io.config.get("interval_us")
            if interval_us is None:
                interval_s = heartbeat_default_s
            else:
                interval_s = max(float(interval_us) / 1_000_000.0, 0.01)

            access_list = desc.get_access(hb_io.io_id)
            access = next((a for a in access_list if a.method == "push"), None)
            if not access:
                print(
                    f"WARNING: heartbeat IO '{hb_io.io_id}' "
                    f"has no push binding in {self.descriptor_paths[idx]}"
                )
                self.heartbeats[idx] = None
                continue

            self.heartbeats[idx] = HeartbeatState(
                can_id=access.can_id,
                interval_s=interval_s,
                timeout_mult=heartbeat_mult,
            )

    def _check_can_id_conflicts(self) -> None:
        """Check for CAN ID conflicts."""
        return None

    @staticmethod
    def _conflict_keys(
        desc: CanDescriptor,
    ) -> Iterable[tuple[int, str]]:
        """Yield conflict keys for a descriptor."""
        yield from iter_conflict_keys(desc)

    def show_menu(self) -> None:
        """Show console help menu."""
        self.print_menu(
            "CAN Console - Commands",
            [
                "devices: List loaded devices",
                "l: List IOs (all nodes)",
                "i [node] <objid>: Show IO details",
                "h: Show this help message",
                "q: Quit",
                "",
                "r [node] <objid> [method]: Read IO value",
                "  Methods: read, read_seg, read_indexed, push",
                "",
                "w [node] <objid> <value> [method]: Write IO value",
                "  Methods: write, write_seg, write_indexed",
                "",
                "hb [node]: Heartbeat status",
            ],
        )

    def start(self) -> None:
        """Start CAN clients and enter the console loop."""
        print(
            f"\nCAN Console - {self.ifname} "
            f"Descriptors: {len(self.descriptors)}"
        )
        for client in self.clients:
            client.start()

        self._wait_for_heartbeats()

    def stop(self) -> None:
        """Stop CAN clients."""
        for client in self.clients:
            client.close()

    def _wait_for_heartbeats(self) -> None:
        self.multi.wait_for_heartbeats()

    def _active_descriptor(self) -> CanDescriptor:
        return self.descriptors[self.active_idx]

    def _active_client(self) -> CanClient:
        return self.clients[self.active_idx]

    def _resolve_io_id(self, io_id: str) -> str | None:
        descriptor = self._active_descriptor()
        if io_id in descriptor.client.ios:
            return io_id
        matches = [key for key in descriptor.client.ios if key == io_id]
        if matches:
            return matches[0]
        print(f"ERROR: Unknown IO '{io_id}'")
        return None

    def _parse_objid(self, objid_str: str) -> int | None:
        try:
            return int(objid_str, 0)
        except ValueError:
            print("ERROR: Invalid Object ID (use hex like 0x00010001)")
            return None

    def _parse_node_args(self, args: str) -> tuple[int, list[str]] | None:
        parts = args.split()
        if len(self.descriptors) > 1:
            if not parts:
                print("ERROR: Specify node index (e.g., 0, 1, 2)")
                return None
            try:
                node_idx = int(parts[0], 10)
            except ValueError:
                print("ERROR: First argument must be node index")
                return None
            if node_idx < 0 or node_idx >= len(self.descriptors):
                print("ERROR: Invalid node index")
                return None
            return node_idx, parts[1:]
        return self.active_idx, parts

    def _resolve_objid(
        self, node_idx: int, objid_str: str
    ) -> tuple[int, str] | None:
        objid = self._parse_objid(objid_str)
        if objid is None:
            return None
        mapping = self._objid_maps[node_idx]
        io_id = mapping.get(objid)
        if io_id is None:
            print(
                f"ERROR: Object ID 0x{objid:08X} "
                f"not found on node {node_idx}"
            )
            return None
        return objid, io_id

    def _build_objid_map(self, desc: CanDescriptor) -> dict[int, str]:
        mapping: dict[int, str] = {}
        for io in desc.client.ios.values():
            objid = self.objid_resolver.io_objid(io)
            if objid is None:
                continue
            mapping[objid] = io.io_id
        return mapping

    def _select_access(
        self,
        io_id: str,
        method: str | None,
        write: bool,
        descriptor: CanDescriptor | None = None,
    ) -> CanAccess | None:
        target = descriptor or self._active_descriptor()
        accesses = target.get_access(io_id)
        if not accesses:
            print("ERROR: No CAN access methods for this IO")
            return None

        if method:
            method = method.lower()
            for access in accesses:
                if access.method == method:
                    return access
            print(f"ERROR: Method '{method}' not available for {io_id}")
            return None

        preferred = (
            ["write", "write_seg", "write_indexed"]
            if write
            else ["read", "read_seg", "read_indexed", "push"]
        )
        for m in preferred:
            for access in accesses:
                if access.method == m:
                    return access
        return accesses[0]

    def cmd_list(self) -> None:
        """List available IOs."""
        print("\nAvailable IOs:")
        for idx, descriptor in enumerate(self.descriptors):
            if idx > 0:
                print()
            print(f" Node {idx}: {self.descriptor_paths[idx]}")
            headers, rows = build_io_table(
                descriptor.client,
                resolver=self.objid_resolver,
                methods_lookup=self._methods_lookup(descriptor),
            )
            print_table(headers, rows)

    def _methods_lookup(
        self, descriptor: CanDescriptor
    ) -> Callable[[str], str]:
        def _lookup(io_id: str) -> str:
            accesses = descriptor.get_access(io_id)
            return ", ".join(a.method for a in accesses) or "-"

        return _lookup

    def cmd_info(self, args: str) -> None:
        """Show IO details."""
        if not args:
            print("ERROR: Usage: i [node] <objid>")
            return
        parsed = self._parse_node_args(args)
        if not parsed:
            return
        node_idx, parts = parsed
        if not parts:
            print("ERROR: Usage: i [node] <objid>")
            return
        resolved = self._resolve_objid(node_idx, parts[0])
        if not resolved:
            return
        objid, io_id = resolved
        io = self.descriptors[node_idx].get_io(io_id)
        if not io:
            print("ERROR: IO not found")
            return
        accesses = self.descriptors[node_idx].get_access(io_id)
        print(f"\nIO: {io_id}")
        print(f"  Object ID: 0x{objid:08X}")
        print(f"  Type: {io.io_type}")
        print(f"  Instance: {io.instance}")
        print(f"  DType: {io.dtype}")
        print(f"  Tags: {', '.join(io.tags) if io.tags else '-'}")
        print("  CAN Access:")
        for access in accesses:
            idx = f", index={access.index}" if access.index else ""
            print(f"    {access.method}: can_id=0x{access.can_id:X}{idx}")

    def cmd_read(self, args: str) -> None:  # noqa: C901
        """Read an IO value."""
        if not args:
            print("ERROR: Usage: r [node] <objid> [method]")
            return
        parsed = self._parse_node_args(args)
        if not parsed:
            return
        node_idx, parts = parsed
        if not parts:
            print("ERROR: Usage: r [node] <objid> [method]")
            return
        resolved = self._resolve_objid(node_idx, parts[0])
        if not resolved:
            return
        _, io_id = resolved
        method = parts[1] if len(parts) > 1 else None
        io = self.descriptors[node_idx].get_io(io_id)
        if method is None and io is not None:
            is_seekable = (
                str(io.dtype).lower().rstrip("_t") == "block"
                or str(io.io_type).lower() == "descriptor"
            )
            if is_seekable:
                has_read_seg = any(
                    a.method == "read_seg"
                    for a in self.descriptors[node_idx].get_access(io_id)
                )
                if has_read_seg:
                    method = "read_seg"

        access = self._select_access(
            io_id, method, write=False, descriptor=self.descriptors[node_idx]
        )
        if not access:
            return
        data = None
        if access.method == "read":
            data = self.clients[node_idx].read_simple(access)
        elif access.method == "read_seg":
            data = self.clients[node_idx].read_segmented(access)
        elif access.method == "read_indexed":
            data = self.clients[node_idx].read_indexed(access)
        elif access.method == "push":
            data = self.clients[node_idx].read_push(access)

        if data is None:
            print("ERROR: Read failed")
            return

        print(f"Value (hex): {data.hex()}")
        if io:
            parsed = unpack_value(io.dtype, data)
            if parsed is not None:
                print(f"Value ({io.dtype}): {parsed}")

    def cmd_write(self, args: str) -> None:
        """Write an IO value."""
        if not args:
            print("ERROR: Usage: w [node] <objid> <value> [method]")
            return
        parsed = self._parse_node_args(args)
        if not parsed:
            return
        node_idx, parts = parsed
        if len(parts) < 2:
            print("ERROR: Usage: w [node] <objid> <value> [method]")
            return
        resolved = self._resolve_objid(node_idx, parts[0])
        if not resolved:
            return
        _, io_id = resolved
        value = parts[1]
        method = parts[2] if len(parts) > 2 else None
        access = self._select_access(
            io_id, method, write=True, descriptor=self.descriptors[node_idx]
        )
        if not access:
            return

        io = self.descriptors[node_idx].get_io(io_id)
        dtype = io.dtype if io else "uint32"
        packed = pack_value(dtype, value)
        if packed is None:
            print(f"ERROR: Unable to pack value for dtype {dtype}")
            return

        if access.method == "write":
            self.clients[node_idx].write_simple(access, packed)
        elif access.method == "write_seg":
            self.clients[node_idx].write_segmented(access, packed)
        elif access.method == "write_indexed":
            self.clients[node_idx].write_indexed(access, packed)
        else:
            print("ERROR: Unsupported write method")
            return

        print("Write sent.")

    def cmd_heartbeat(self, args: str) -> None:
        """Show heartbeat status."""
        node_idx = self.active_idx
        if len(self.descriptors) > 1:
            if not args:
                print("ERROR: Usage: hb <node>")
                return
            try:
                node_idx = int(args.strip(), 10)
            except ValueError:
                print("ERROR: Node index must be integer")
                return
            if node_idx < 0 or node_idx >= len(self.descriptors):
                print("ERROR: Invalid node index")
                return
        status = self.multi.heartbeat_status(node_idx)
        if not status:
            self.warn("No heartbeat configured for active device.")
            return
        state = "LIVE" if status["live"] else "DEAD"
        last_seen = status["last_seen"]
        last = f"{last_seen:.3f}" if last_seen else "-"
        hb = self.heartbeats.get(node_idx)
        if not hb:
            return
        print(
            f"Heartbeat: {state}, can_id=0x{hb.can_id:X}, "
            f"last_seen={last}, interval={hb.interval_s:.3f}s"
        )

    def cmd_devices(self) -> None:
        """List loaded devices."""
        print("\nDevices:")
        for idx, path in enumerate(self.descriptor_paths):
            node_id = getattr(self.descriptors[idx], "node_id", 0)
            print(f"  Node {idx} (node_id=0x{node_id:X}): " f"{path}")

    def cmd_use(self, args: str) -> None:
        """Select active device."""
        if len(self.descriptors) > 1:
            self.error("use is disabled when multiple nodes are loaded")
            return
        if not args:
            print("ERROR: Usage: use <idx>")
            return
        try:
            idx = int(args.strip())
        except ValueError:
            print("ERROR: Device index must be integer")
            return
        if idx < 0 or idx >= len(self.descriptors):
            self.error("Device index out of range")
            return
        self.active_idx = idx
        self.ok(f"Active device set to {idx}")

    def commands_no_args(self) -> dict[str, Callable[[], None]]:
        """Return CAN commands that do not take arguments."""
        return {
            "l": self.cmd_list,
            "devices": self.cmd_devices,
        }

    def commands_with_args(self) -> dict[str, Callable[[str], None]]:
        """Return CAN commands that take arguments."""
        return {
            "i": self.cmd_info,
            "r": self.cmd_read,
            "w": self.cmd_write,
            "use": self.cmd_use,
            "hb": self.cmd_heartbeat,
        }

    def on_exit_command(self) -> None:
        """Render the CAN exit message."""
        self.info("Exiting CAN console.")


def run_console(
    descriptor_paths: list[str],
    ifname: str = "can0",
    extended: bool | None = None,
    heartbeat_mult: int = 3,
    heartbeat_default_s: float = 1.0,
    kconfig_path: str | None = None,
    kconfig_overrides: list[dict[str, object]] | None = None,
) -> None:  # pragma: no cover
    """Run CAN console."""
    console = CanConsole(
        descriptor_paths=descriptor_paths,
        ifname=ifname,
        extended=extended,
        heartbeat_mult=heartbeat_mult,
        heartbeat_default_s=heartbeat_default_s,
        kconfig_path=kconfig_path,
        kconfig_overrides=kconfig_overrides,
    )
    console.run()
