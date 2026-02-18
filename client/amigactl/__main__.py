"""CLI entry point for the amigactl client.

Usage::

    amigactl --host 192.168.6.200 version
    amigactl ping
    amigactl shutdown
"""

import argparse
import os
import sys

from . import AmigaConnection, AmigactlError, ProtocolError


def cmd_version(conn: AmigaConnection) -> None:
    """Handle the 'version' subcommand."""
    print(conn.version())


def cmd_ping(conn: AmigaConnection) -> None:
    """Handle the 'ping' subcommand."""
    conn.ping()
    print("OK")


def cmd_shutdown(conn: AmigaConnection) -> None:
    """Handle the 'shutdown' subcommand."""
    info = conn.shutdown()
    if info:
        print(info)
    else:
        print("Shutdown initiated")


def main() -> None:
    """Parse arguments and dispatch to the appropriate subcommand."""
    default_host = os.environ.get("AMIGACTL_HOST", "192.168.6.200")
    try:
        default_port = int(os.environ.get("AMIGACTL_PORT", "6800"))
    except ValueError:
        print(
            "Error: AMIGACTL_PORT must be an integer, got: {!r}".format(
                os.environ["AMIGACTL_PORT"]
            ),
            file=sys.stderr,
        )
        sys.exit(1)

    parser = argparse.ArgumentParser(
        prog="amigactl",
        description="Amiga remote access client",
    )
    parser.add_argument(
        "--host",
        default=default_host,
        help="Daemon hostname or IP (default: %(default)s)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=default_port,
        help="Daemon port (default: %(default)s)",
    )

    subparsers = parser.add_subparsers(dest="command")
    subparsers.required = True

    subparsers.add_parser("version", help="Print daemon version")
    subparsers.add_parser("ping", help="Ping the daemon")
    subparsers.add_parser("shutdown", help="Shut down the daemon (sends SHUTDOWN CONFIRM)")

    args = parser.parse_args()

    dispatch = {
        "version": cmd_version,
        "ping": cmd_ping,
        "shutdown": cmd_shutdown,
    }

    try:
        with AmigaConnection(args.host, args.port) as conn:
            dispatch[args.command](conn)
    except ConnectionRefusedError:
        print(
            "Error: could not connect to {}:{}".format(args.host, args.port),
            file=sys.stderr,
        )
        sys.exit(1)
    except OSError as e:
        print("Error: {}".format(e), file=sys.stderr)
        sys.exit(1)
    except AmigactlError as e:
        print("Error: {}".format(e.message), file=sys.stderr)
        sys.exit(1)
    except ProtocolError as e:
        print("Error: {}".format(e), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
