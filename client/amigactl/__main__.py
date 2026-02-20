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


def cmd_exec(conn, args):
    """Handle the 'exec' subcommand."""
    parts = args.cmd
    # argparse.REMAINDER may include a leading '--'; strip it
    if parts and parts[0] == "--":
        parts = parts[1:]
    if not parts:
        print("Error: no command specified", file=sys.stderr)
        sys.exit(1)
    command = " ".join(parts)
    rc, output = conn.execute(command, timeout=args.timeout, cd=args.C)
    if output:
        sys.stdout.write(output)
    if rc != 0:
        sys.exit(min(rc, 255))


def cmd_run(conn, args):
    """Handle the 'run' subcommand."""
    parts = args.cmd
    if parts and parts[0] == "--":
        parts = parts[1:]
    if not parts:
        print("Error: no command specified", file=sys.stderr)
        sys.exit(1)
    command = " ".join(parts)
    proc_id = conn.execute_async(command, cd=args.C)
    print(proc_id)


def cmd_ps(conn, args):
    """Handle the 'ps' subcommand."""
    procs = conn.proclist()
    if not procs:
        return
    print("{}\t{}\t{}\t{}".format("ID", "COMMAND", "STATUS", "RC"))
    for p in procs:
        rc_str = str(p["rc"]) if p["rc"] is not None else "-"
        print("{}\t{}\t{}\t{}".format(
            p["id"], p["command"], p["status"], rc_str))


def cmd_status(conn, args):
    """Handle the 'status' subcommand."""
    info = conn.procstat(args.id)
    for key in ("id", "command", "status", "rc"):
        if key in info:
            val = info[key]
            if val is None:
                val = "-"
            print("{}={}".format(key, val))


def cmd_signal(conn, args):
    """Handle the 'signal' subcommand."""
    conn.signal(args.id, sig=args.sig)
    print("OK")


def cmd_kill(conn, args):
    """Handle the 'kill' subcommand."""
    conn.kill(args.id)
    print("OK")


def cmd_sysinfo(conn, args):
    """Handle the 'sysinfo' subcommand."""
    info = conn.sysinfo()
    for key, value in info.items():
        print("{}={}".format(key, value))


def cmd_assigns(conn, args):
    """Handle the 'assigns' subcommand."""
    assigns = conn.assigns()
    for name, path in assigns.items():
        print("{}\t{}".format(name, path))


def cmd_ports(conn, args):
    """Handle the 'ports' subcommand."""
    ports = conn.ports()
    for port in ports:
        print(port)


def cmd_volumes(conn, args):
    """Handle the 'volumes' subcommand."""
    vols = conn.volumes()
    if not vols:
        return
    print("{}\t{}\t{}\t{}\t{}".format(
        "NAME", "USED", "FREE", "CAPACITY", "BLOCKSIZE"))
    for v in vols:
        print("{}\t{}\t{}\t{}\t{}".format(
            v["name"], v["used"], v["free"],
            v["capacity"], v["blocksize"]))


def cmd_tasks(conn, args):
    """Handle the 'tasks' subcommand."""
    tasks = conn.tasks()
    if not tasks:
        return
    print("{}\t{}\t{}\t{}\t{}".format(
        "NAME", "TYPE", "PRI", "STATE", "STACK"))
    for t in tasks:
        print("{}\t{}\t{}\t{}\t{}".format(
            t["name"], t["type"], t["priority"],
            t["state"], t["stacksize"]))


def cmd_touch(conn, args):
    """Handle the 'touch' subcommand."""
    datestamp = "{} {}".format(args.date, args.time)
    result = conn.setdate(args.path, datestamp)
    print("datestamp={}".format(result))


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

    p_exec = subparsers.add_parser("exec", help="Execute a CLI command")
    p_exec.add_argument("-C", metavar="DIR", default=None,
                        help="Working directory for the command")
    p_exec.add_argument("--timeout", type=int, default=None, metavar="SECS",
                        help="Socket timeout in seconds for the command")
    p_exec.add_argument("cmd", nargs=argparse.REMAINDER,
                        help="Command to execute (use -- before flags)")

    p_run = subparsers.add_parser("run",
                                  help="Launch a command asynchronously")
    p_run.add_argument("-C", metavar="DIR", default=None,
                       help="Working directory for the command")
    p_run.add_argument("cmd", nargs=argparse.REMAINDER,
                       help="Command to launch (use -- before flags)")

    subparsers.add_parser("ps", help="List daemon-launched processes")

    p_status = subparsers.add_parser("status",
                                     help="Get status of a tracked process")
    p_status.add_argument("id", type=int, help="Process ID")

    p_signal = subparsers.add_parser("signal",
                                     help="Send break signal to a process")
    p_signal.add_argument("id", type=int, help="Process ID")
    p_signal.add_argument("sig", nargs="?", default="CTRL_C",
                          help="Signal name (default: CTRL_C)")

    p_kill = subparsers.add_parser("kill",
                                   help="Force-terminate a tracked process")
    p_kill.add_argument("id", type=int, help="Process ID")

    subparsers.add_parser("sysinfo", help="Show system information")
    subparsers.add_parser("assigns", help="List logical assigns")
    subparsers.add_parser("ports", help="List active Exec message ports")
    subparsers.add_parser("volumes", help="List mounted volumes")
    subparsers.add_parser("tasks", help="List running tasks/processes")

    p_touch = subparsers.add_parser("touch", help="Set file datestamp")
    p_touch.add_argument("path", help="Amiga path")
    p_touch.add_argument("date", help="Date (YYYY-MM-DD)")
    p_touch.add_argument("time", help="Time (HH:MM:SS)")

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
        "exec": cmd_exec,
        "run": cmd_run,
        "ps": cmd_ps,
        "status": cmd_status,
        "signal": cmd_signal,
        "kill": cmd_kill,
        "sysinfo": cmd_sysinfo,
        "assigns": cmd_assigns,
        "ports": cmd_ports,
        "volumes": cmd_volumes,
        "tasks": cmd_tasks,
        "touch": cmd_touch,
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
