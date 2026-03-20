"""Standalone CLI entry point for dawnpy-can."""

from dawnpy_can.commands.cmd_can import cmd_can


def main() -> None:
    """Run the CAN CLI."""
    cmd_can(prog_name="dawnpy-can")


if __name__ == "__main__":
    main()
