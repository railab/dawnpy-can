# tools/dawnpy/src/dawnpy/commands/cmd_can.py
#
# SPDX-License-Identifier: Apache-2.0
#

"""Module containing CAN command."""

import click
from dawnpy.cli.device_registry import DeviceConflictError
from dawnpy.cli.environment import Environment, pass_environment
from dawnpy.cli.options import build_kconfig_overrides, configure_cli_logging

from dawnpy_can.console import run_console

###############################################################################
# Command: cmd_can
###############################################################################


@click.command(name="can")
@click.argument(
    "descriptors",
    type=click.Path(resolve_path=False),
    required=True,
    nargs=-1,
)
@click.option(
    "--ifname",
    default="can0",
    show_default=True,
    help="SocketCAN interface name",
)
@click.option(
    "--extended",
    "extended",
    flag_value=True,
    default=None,
    help="Force extended CAN IDs",
)
@click.option(
    "--standard",
    "extended",
    flag_value=False,
    default=None,
    help="Force standard CAN IDs",
)
@click.option(
    "--heartbeat-mult",
    default=3,
    show_default=True,
    help="Heartbeat timeout multiplier",
)
@click.option(
    "--heartbeat-default",
    default=1.0,
    show_default=True,
    help="Default heartbeat interval (seconds) if not in descriptor",
)
@click.option(
    "--kconfig-var",
    "kconfig_var",
    help="Kconfig symbol name to override (e.g., CONFIG_SIM_CAN_NODEID)",
)
@click.option(
    "--kconfig-values",
    "kconfig_values",
    help="Comma-separated values for the Kconfig override",
)
@click.option(
    "--debug/--no-debug",
    default=False,
    is_flag=True,
    envvar="DAWNPY_DEBUG",
)
@pass_environment
def cmd_can(
    ctx: Environment,
    descriptors: tuple[str, ...],
    ifname: str,
    extended: bool | None,
    heartbeat_mult: int,
    heartbeat_default: float,
    kconfig_var: str | None,
    kconfig_values: str | None,
    debug: bool,
) -> bool:
    """Run CAN console for interactive device communication."""
    ctx.debug = debug
    configure_cli_logging(debug)

    try:
        descriptor_list = list(descriptors)
        kconfig_overrides = build_kconfig_overrides(
            descriptor_list,
            kconfig_var,
            kconfig_values,
        )
        if kconfig_overrides and len(descriptor_list) == 1:
            if len(kconfig_overrides) > 1:
                descriptor_list = descriptor_list * len(kconfig_overrides)
        run_console(
            descriptor_paths=descriptor_list,
            ifname=ifname,
            extended=extended,
            heartbeat_mult=heartbeat_mult,
            heartbeat_default_s=heartbeat_default,
            kconfig_overrides=kconfig_overrides,
        )
    except DeviceConflictError as exc:
        raise click.ClickException(exc.format_message()) from exc
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    return True
