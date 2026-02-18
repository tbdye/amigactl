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


def cmd_version(conn, args):
    """Handle the 'version' subcommand."""
    print(conn.version())


def cmd_ping(conn, args):
    """Handle the 'ping' subcommand."""
    conn.ping()
    print("OK")


def cmd_shutdown(conn, args):
    """Handle the 'shutdown' subcommand."""
    info = conn.shutdown()
    if info:
        print(info)
    else:
        print("Shutdown initiated")


def cmd_ls(conn, args):
    """Handle the 'ls' subcommand."""
    entries = conn.dir(args.path, recursive=args.recursive)
    for entry in entries:
        print("{}\t{}\t{}\t{}\t{}".format(
            entry["type"], entry["name"], entry["size"],
            entry["protection"], entry["datestamp"]))


def cmd_stat(conn, args):
    """Handle the 'stat' subcommand."""
    info = conn.stat(args.path)
    for key in ("type", "name", "size", "protection", "datestamp", "comment"):
        if key in info:
            print("{}={}".format(key, info[key]))


def cmd_cat(conn, args):
    """Handle the 'cat' subcommand."""
    data = conn.read(args.path)
    sys.stdout.buffer.write(data)


def cmd_get(conn, args):
    """Handle the 'get' subcommand."""
    data = conn.read(args.remote)
    with open(args.local, "wb") as f:
        f.write(data)
    print("Downloaded {} bytes to {}".format(len(data), args.local))


def cmd_put(conn, args):
    """Handle the 'put' subcommand."""
    with open(args.local, "rb") as f:
        data = f.read()
    written = conn.write(args.remote, data)
    print("Uploaded {} bytes to {}".format(written, args.remote))


def cmd_rm(conn, args):
    """Handle the 'rm' subcommand."""
    conn.delete(args.path)
    print("Deleted")


def cmd_mv(conn, args):
    """Handle the 'mv' subcommand."""
    conn.rename(args.old, args.new)
    print("Renamed")


def cmd_mkdir(conn, args):
    """Handle the 'mkdir' subcommand."""
    conn.makedir(args.path)
    print("Created")


def cmd_chmod(conn, args):
    """Handle the 'chmod' subcommand."""
    result = conn.protect(args.path, args.value)
    print("protection={}".format(result))


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

    p_ls = subparsers.add_parser("ls", help="List directory contents")
    p_ls.add_argument("path", help="Amiga path to list")
    p_ls.add_argument("-r", "--recursive", action="store_true",
                      help="Recurse into subdirectories")

    p_stat = subparsers.add_parser("stat", help="Show file/directory metadata")
    p_stat.add_argument("path", help="Amiga path")

    p_cat = subparsers.add_parser("cat", help="Print file contents to stdout")
    p_cat.add_argument("path", help="Amiga file path")

    p_get = subparsers.add_parser("get", help="Download a file")
    p_get.add_argument("remote", help="Amiga file path")
    p_get.add_argument("local", help="Local file path")

    p_put = subparsers.add_parser("put", help="Upload a file")
    p_put.add_argument("local", help="Local file path")
    p_put.add_argument("remote", help="Amiga file path")

    p_rm = subparsers.add_parser("rm", help="Delete a file or empty directory")
    p_rm.add_argument("path", help="Amiga path")

    p_mv = subparsers.add_parser("mv", help="Rename/move a file or directory")
    p_mv.add_argument("old", help="Current Amiga path")
    p_mv.add_argument("new", help="New Amiga path")

    p_mkdir = subparsers.add_parser("mkdir", help="Create a directory")
    p_mkdir.add_argument("path", help="Amiga path")

    p_chmod = subparsers.add_parser("chmod", help="Get or set protection bits")
    p_chmod.add_argument("path", help="Amiga path")
    p_chmod.add_argument("value", nargs="?", default=None,
                         help="Hex protection value to set (omit to get)")

    args = parser.parse_args()

    dispatch = {
        "version": cmd_version,
        "ping": cmd_ping,
        "shutdown": cmd_shutdown,
        "ls": cmd_ls,
        "stat": cmd_stat,
        "cat": cmd_cat,
        "get": cmd_get,
        "put": cmd_put,
        "rm": cmd_rm,
        "mv": cmd_mv,
        "mkdir": cmd_mkdir,
        "chmod": cmd_chmod,
    }

    try:
        with AmigaConnection(args.host, args.port) as conn:
            dispatch[args.command](conn, args)
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
