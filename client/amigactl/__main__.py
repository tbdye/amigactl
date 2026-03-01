"""CLI entry point for the amigactl client.

Usage::

    amigactl --host 192.168.6.200 version
    amigactl ping
    amigactl shutdown
"""

import argparse
import configparser
import os
import sys

from . import AmigaConnection, AmigactlError, NotFoundError, ProtocolError


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


def cmd_reboot(conn, args):
    """Handle the 'reboot' subcommand."""
    info = conn.reboot()
    if info:
        print(info)
    else:
        print("Reboot initiated")


def cmd_uptime(conn, args):
    """Handle the 'uptime' subcommand."""
    seconds = conn.uptime()
    days = seconds // 86400
    seconds %= 86400
    hours = seconds // 3600
    seconds %= 3600
    minutes = seconds // 60
    secs = seconds % 60
    parts = []
    if days:
        parts.append("{}d".format(days))
    if hours:
        parts.append("{}h".format(hours))
    if minutes:
        parts.append("{}m".format(minutes))
    # Always show seconds (even 0) if nothing else, or if non-zero
    if secs or not parts:
        parts.append("{}s".format(secs))
    print(" ".join(parts))


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
    data = conn.read(args.path, offset=args.offset, length=args.length)
    sys.stdout.buffer.write(data)


def cmd_get(conn, args):
    """Handle the 'get' subcommand."""
    data = conn.read(args.remote, offset=args.offset, length=args.length)
    local = args.local
    if local is None:
        # Extract basename from Amiga path
        name = args.remote.rsplit("/", 1)[-1] if "/" in args.remote \
            else args.remote
        name = name.rsplit(":", 1)[-1] if ":" in name else name
        local = name or args.remote.rstrip(":/")  # volume root fallback
    with open(local, "wb") as f:
        f.write(data)
    print("Downloaded {} bytes to {}".format(len(data), local))


def cmd_put(conn, args):
    """Handle the 'put' subcommand."""
    with open(args.local, "rb") as f:
        data = f.read()
    remote = args.remote
    if remote is None:
        remote = os.path.basename(args.local)
    written = conn.write(remote, data)
    print("Uploaded {} bytes to {}".format(written, remote))


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


def cmd_assign(conn, args):
    """Handle the 'assign' subcommand."""
    mode = None
    if args.late:
        mode = "late"
    elif args.add:
        mode = "add"
    conn.assign(args.name, path=args.path, mode=mode)
    if args.path is not None:
        print("OK")
    else:
        print("Removed")


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
    if len(args.datetime) == 0:
        datestamp = None
    elif len(args.datetime) == 2:
        datestamp = "{} {}".format(args.datetime[0], args.datetime[1])
    else:
        print("Error: provide both DATE and TIME, or neither",
              file=sys.stderr)
        sys.exit(1)

    try:
        result = conn.setdate(args.path, datestamp)
    except NotFoundError:
        # File doesn't exist -- create it (Unix touch semantics)
        conn.write(args.path, b"")
        if datestamp is not None:
            result = conn.setdate(args.path, datestamp)
        else:
            result = None
    if result is not None:
        print("datestamp={}".format(result))
    else:
        print("Created {}".format(args.path))


def cmd_arexx(conn, args):
    """Handle the 'arexx' subcommand."""
    parts = args.command
    # argparse.REMAINDER may include a leading '--'; strip it
    if parts and parts[0] == "--":
        parts = parts[1:]
    if not parts:
        print("Error: no command specified", file=sys.stderr)
        sys.exit(1)
    command = " ".join(parts)
    rc, result = conn.arexx(args.port, command)
    if result:
        print(result)
    if rc != 0:
        sys.exit(min(rc, 255))


def cmd_tail(conn, args):
    """Handle the 'tail' subcommand."""
    def write_chunk(chunk):
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()

    try:
        conn.tail(args.path, write_chunk)
    except KeyboardInterrupt:
        try:
            conn.stop_tail()
        except Exception:
            pass


def cmd_trace(conn, args):
    """Handle the 'trace' subcommand."""
    from .colors import ColorWriter, format_trace_event

    sub = args.trace_cmd
    if sub is None:
        print("Usage: amigactl trace {start,stop,status,enable,disable}",
              file=sys.stderr)
        sys.exit(1)

    if sub == "status":
        status = conn.trace_status()
        if not status.get("loaded"):
            print("atrace is not loaded.")
            return
        print("loaded=1")
        print("enabled={}".format(1 if status.get("enabled") else 0))
        for key in ("patches", "events_produced", "events_consumed",
                     "events_dropped", "buffer_capacity", "buffer_used"):
            if key in status:
                print("{}={}".format(key, status[key]))

    elif sub == "enable":
        funcs = args.funcs if args.funcs else None
        conn.trace_enable(funcs=funcs)
        if funcs:
            print("Enabled: {}".format(", ".join(funcs)))
        else:
            print("atrace tracing enabled.")

    elif sub == "disable":
        funcs = args.funcs if args.funcs else None
        conn.trace_disable(funcs=funcs)
        if funcs:
            print("Disabled: {}".format(", ".join(funcs)))
        else:
            print("atrace tracing disabled.")

    elif sub == "stop":
        print("trace stop is only valid during an active trace stream.",
              file=sys.stderr)
        print("Use Ctrl-C to stop a running trace.", file=sys.stderr)
        sys.exit(1)

    elif sub == "start":
        cw = ColorWriter()

        # Column header
        print("{:<10s} {:>13s}  {:<22s} {:<16s} {:<40s} {}".format(
            "SEQ", "TIME", "FUNCTION", "TASK", "ARGS", "RESULT"))

        def trace_callback(event):
            print(format_trace_event(event, cw))

        kwargs = {}
        if args.lib:
            kwargs["lib"] = args.lib
        if args.func:
            kwargs["func"] = args.func
        if args.proc:
            kwargs["proc"] = args.proc
        if args.errors:
            kwargs["errors_only"] = True

        try:
            conn.trace_start(trace_callback, **kwargs)
        except KeyboardInterrupt:
            try:
                conn.stop_trace()
            except Exception:
                pass


def cmd_cp(conn, args):
    """Handle the 'cp' subcommand."""
    conn.copy(args.source, args.dest,
              noclone=args.no_clone, noreplace=args.no_replace)
    print("Copied")


def cmd_append(conn, args):
    """Handle the 'append' subcommand."""
    with open(args.local, "rb") as f:
        data = f.read()
    written = conn.append(args.remote, data)
    print("Appended {} bytes to {}".format(written, args.remote))


def cmd_checksum(conn, args):
    """Handle the 'checksum' subcommand."""
    result = conn.checksum(args.path)
    print("crc32={}".format(result.get("crc32", "")))
    print("size={}".format(result.get("size", "")))


def cmd_setcomment(conn, args):
    """Handle the 'setcomment' subcommand."""
    conn.setcomment(args.path, args.comment)
    print("Comment set")


def cmd_libver(conn, args):
    """Handle the 'libver' subcommand."""
    result = conn.libver(args.name)
    print("name={}".format(result.get("name", "")))
    print("version={}".format(result.get("version", "")))


def cmd_env(conn, args):
    """Handle the 'env' subcommand."""
    result = conn.env(args.name)
    print(result)


def cmd_setenv(conn, args):
    """Handle the 'setenv' subcommand."""
    conn.setenv(args.name, value=args.value,
                volatile=args.volatile)
    if args.value is not None:
        print("Set")
    else:
        print("Deleted")


def cmd_devices(conn, args):
    """Handle the 'devices' subcommand."""
    devs = conn.devices()
    if not devs:
        return
    print("{}\t{}".format("NAME", "VERSION"))
    for d in devs:
        print("{}\t{}".format(d["name"], d["version"]))


def cmd_capabilities(conn, args):
    """Handle the 'capabilities' subcommand."""
    caps = conn.capabilities()
    for key, value in caps.items():
        print("{}={}".format(key, value))


def _default_config_path(host=None, port=None):
    """Return the path to amigactl.conf in the client directory.

    If the file does not exist but amigactl.conf.example does, copy it
    to create a starter config with defaults filled in.  When *host* or
    *port* are provided (from CLI flags), those values are written into
    the generated config so the user's first-run settings are captured.
    """
    client_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    conf = os.path.join(client_dir, "amigactl.conf")
    if not os.path.exists(conf):
        example = os.path.join(client_dir, "amigactl.conf.example")
        if os.path.exists(example):
            try:
                with open(example, "r") as src, open(conf, "w") as dst:
                    content = src.read()
                    if host is not None:
                        content = content.replace(
                            "host = 192.168.6.200",
                            "host = {}".format(host),
                        )
                    if port is not None:
                        content = content.replace(
                            "port = 6800",
                            "port = {}".format(port),
                        )
                    if sys.platform == "win32":
                        content = content.replace(
                            "# Linux/macOS:\ncommand = vi\n"
                            "# Windows:\n# command = notepad",
                            "# Linux/macOS:\n# command = vi\n"
                            "# Windows:\ncommand = notepad",
                        )
                    dst.write(content)
            except OSError:
                pass
    return conf


def _load_config(path, explicit):
    """Load settings from a config file.

    Args:
        path: File path to read.
        explicit: True if the user passed --config (errors are fatal).

    Returns a dict with keys 'host', 'port', 'editor' (any may be None).
    """
    if not os.path.exists(path):
        if explicit:
            print("Error: config file not found: {}".format(path),
                  file=sys.stderr)
            sys.exit(1)
        return {}

    config = configparser.ConfigParser()
    try:
        config.read(path)
    except configparser.Error as e:
        if explicit:
            print("Error: failed to parse config file: {}".format(e),
                  file=sys.stderr)
            sys.exit(1)
        print("Warning: failed to parse config file: {}".format(e),
              file=sys.stderr)
        return {}

    result = {}

    # Host
    host = config.get("connection", "host", fallback=None)
    if host is not None:
        host = host.strip() or None
    result["host"] = host

    # Port
    try:
        port = config.getint("connection", "port", fallback=None)
    except ValueError as e:
        if explicit:
            print("Error: invalid port in config file: {}".format(e),
                  file=sys.stderr)
            sys.exit(1)
        print("Warning: invalid port in config file: {}".format(e),
              file=sys.stderr)
        port = None
    result["port"] = port

    # Editor
    editor = config.get("editor", "command", fallback=None)
    if editor is not None:
        editor = editor.strip() or None
    result["editor"] = editor

    return result


def main() -> None:
    """Parse arguments and dispatch to the appropriate subcommand."""
    DEFAULT_HOST = "192.168.6.200"
    DEFAULT_PORT = 6800

    # --- Pre-parse: resolve env vars for help string defaults ---
    env_host = os.environ.get("AMIGACTL_HOST") or None
    env_port_str = os.environ.get("AMIGACTL_PORT")
    env_port = None
    if env_port_str:
        try:
            env_port = int(env_port_str)
        except ValueError:
            print(
                "Error: AMIGACTL_PORT must be an integer, got: {!r}".format(
                    env_port_str
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
        default=None,
        help="Daemon hostname or IP (default: {})".format(
            env_host if env_host is not None else DEFAULT_HOST),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Daemon port (default: {})".format(
            env_port if env_port is not None else DEFAULT_PORT),
    )
    parser.add_argument(
        "--config",
        default=None,
        metavar="PATH",
        help="Path to config file (default: client/amigactl.conf)",
    )

    subparsers = parser.add_subparsers(dest="command")
    subparsers.required = False

    subparsers.add_parser("version", help="Print daemon version")
    subparsers.add_parser("ping", help="Ping the daemon")
    subparsers.add_parser("shutdown", help="Shut down the daemon (sends SHUTDOWN CONFIRM)")
    subparsers.add_parser("reboot", help="Reboot the Amiga (sends REBOOT CONFIRM)")

    p_ls = subparsers.add_parser("ls", help="List directory contents")
    p_ls.add_argument("path", help="Amiga path to list")
    p_ls.add_argument("-r", "--recursive", action="store_true",
                      help="Recurse into subdirectories")

    p_stat = subparsers.add_parser("stat", help="Show file/directory metadata")
    p_stat.add_argument("path", help="Amiga path")

    p_cat = subparsers.add_parser("cat", help="Print file contents to stdout")
    p_cat.add_argument("path", help="Amiga file path")
    p_cat.add_argument("--offset", type=int, default=None,
                       help="Start reading at byte offset")
    p_cat.add_argument("--length", type=int, default=None,
                       help="Read at most this many bytes")

    p_get = subparsers.add_parser("get", help="Download a file")
    p_get.add_argument("remote", help="Amiga file path")
    p_get.add_argument("local", nargs="?", default=None,
                       help="Local file path (default: same name in "
                            "current directory)")
    p_get.add_argument("--offset", type=int, default=None,
                       help="Start reading at byte offset")
    p_get.add_argument("--length", type=int, default=None,
                       help="Read at most this many bytes")

    p_put = subparsers.add_parser("put", help="Upload a file")
    p_put.add_argument("local", help="Local file path")
    p_put.add_argument("remote", nargs="?", default=None,
                       help="Amiga file path (default: same name in "
                            "current Amiga directory)")

    p_append = subparsers.add_parser("append",
                                      help="Append local file to remote file")
    p_append.add_argument("local", help="Local file to append")
    p_append.add_argument("remote", help="Amiga file to append to")

    p_rm = subparsers.add_parser("rm", help="Delete a file or empty directory")
    p_rm.add_argument("path", help="Amiga path")

    p_mv = subparsers.add_parser("mv", help="Rename/move a file or directory")
    p_mv.add_argument("old", help="Current Amiga path")
    p_mv.add_argument("new", help="New Amiga path")

    p_cp = subparsers.add_parser("cp", help="Copy a file on the Amiga")
    p_cp.add_argument("source", help="Source Amiga path")
    p_cp.add_argument("dest", help="Destination Amiga path")
    p_cp.add_argument("-P", "--no-clone", action="store_true",
                      help="Do not copy metadata (protection, date, comment)")
    p_cp.add_argument("-n", "--no-replace", action="store_true",
                      help="Fail if destination already exists")

    p_mkdir = subparsers.add_parser("mkdir", help="Create a directory")
    p_mkdir.add_argument("path", help="Amiga path")

    p_chmod = subparsers.add_parser("chmod", help="Get or set protection bits")
    p_chmod.add_argument("path", help="Amiga path")
    p_chmod.add_argument("value", nargs="?", default=None,
                         help="Hex protection value to set (omit to get)")

    p_checksum = subparsers.add_parser("checksum",
                                        help="Compute CRC32 checksum of a file")
    p_checksum.add_argument("path", help="Amiga file path")

    p_setcomment = subparsers.add_parser("setcomment",
                                          help="Set file comment")
    p_setcomment.add_argument("path", help="Amiga file path")
    p_setcomment.add_argument("comment", help="Comment string (use '' to clear)")

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

    p_libver = subparsers.add_parser("libver",
                                      help="Get library or device version")
    p_libver.add_argument("name",
                           help="Library or device name (e.g. exec.library)")

    p_env = subparsers.add_parser("env",
                                   help="Get an environment variable")
    p_env.add_argument("name", help="Variable name")

    p_setenv = subparsers.add_parser("setenv",
                                      help="Set or delete an environment variable")
    p_setenv.add_argument("-v", "--volatile", action="store_true",
                          help="Volatile only (not persisted to ENVARC:)")
    p_setenv.add_argument("name", help="Variable name")
    p_setenv.add_argument("value", nargs="?", default=None,
                           help="Value to set (omit to delete)")

    subparsers.add_parser("assigns", help="List logical assigns")

    p_assign = subparsers.add_parser("assign",
                                      help="Create, modify, or remove an assign")
    mode_group = p_assign.add_mutually_exclusive_group()
    mode_group.add_argument("--late", action="store_true",
                            help="Late-binding assign (path resolved on access)")
    mode_group.add_argument("--add", action="store_true",
                            help="Add to existing multi-directory assign")
    p_assign.add_argument("name", help="Assign name with colon (e.g., TEST:)")
    p_assign.add_argument("path", nargs="?", default=None,
                          help="Target path (omit to remove the assign)")

    subparsers.add_parser("ports", help="List active Exec message ports")
    subparsers.add_parser("volumes", help="List mounted volumes")
    subparsers.add_parser("tasks", help="List running tasks/processes")
    subparsers.add_parser("devices", help="List Exec devices")
    subparsers.add_parser("capabilities",
                           help="Show daemon capabilities")
    subparsers.add_parser("uptime", help="Show daemon uptime")

    p_touch = subparsers.add_parser("touch", help="Set file datestamp (creates file if missing)")
    p_touch.add_argument("path", help="Amiga path")
    p_touch.add_argument("datetime", nargs="*", metavar="DATETIME",
                         help="Date (YYYY-MM-DD) and time (HH:MM:SS). "
                              "Default: current time")

    p_arexx = subparsers.add_parser("arexx",
                                     help="Send ARexx command to named port")
    p_arexx.add_argument("port", help="ARexx port name")
    p_arexx.add_argument("command", nargs=argparse.REMAINDER,
                          help="ARexx command string (use -- before flags)")

    p_tail = subparsers.add_parser("tail",
                                    help="Stream file appends (Ctrl-C to stop)")
    p_tail.add_argument("path", help="Amiga file path to tail")

    p_trace = subparsers.add_parser("trace",
                                     help="Control library call tracing")
    trace_sub = p_trace.add_subparsers(dest="trace_cmd")

    p_trace_start = trace_sub.add_parser("start", help="Start tracing")
    p_trace_start.add_argument("--lib",
                                help="Filter by library name")
    p_trace_start.add_argument("--func",
                                help="Filter by function name")
    p_trace_start.add_argument("--proc",
                                help="Filter by process name")
    p_trace_start.add_argument("--errors", action="store_true",
                                help="Only show error returns")

    trace_sub.add_parser("stop", help="Stop tracing")
    trace_sub.add_parser("status", help="Show atrace status")

    p_trace_enable = trace_sub.add_parser(
        "enable", help="Enable atrace globally or specific functions")
    p_trace_enable.add_argument(
        "funcs", nargs="*",
        help="Function names to enable (all if omitted)")

    p_trace_disable = trace_sub.add_parser(
        "disable", help="Disable atrace globally or specific functions")
    p_trace_disable.add_argument(
        "funcs", nargs="*",
        help="Function names to disable (all if omitted)")

    subparsers.add_parser("shell", help="Interactive shell mode")

    args = parser.parse_args()

    # --- Load config file ---
    config_path = (args.config if args.config
                    else _default_config_path(args.host, args.port))
    explicit_config = bool(args.config)
    cfg = {}
    if config_path:
        cfg = _load_config(config_path, explicit_config)

    # --- Resolve host (CLI > env > config > default) ---
    if args.host is not None:
        host = args.host
    elif env_host is not None:
        host = env_host
    elif cfg.get("host") is not None:
        host = cfg["host"]
    else:
        host = DEFAULT_HOST

    # --- Resolve port (CLI > env > config > default) ---
    if args.port is not None:
        port = args.port
    elif env_port is not None:
        port = env_port
    elif cfg.get("port") is not None:
        port = cfg["port"]
    else:
        port = DEFAULT_PORT

    # --- Resolve editor (config only; env vars handled in shell) ---
    editor = cfg.get("editor")

    # Default to interactive shell when no subcommand is given
    if args.command is None:
        args.command = "shell"

    dispatch = {
        "append": cmd_append,
        "arexx": cmd_arexx,
        "assign": cmd_assign,
        "assigns": cmd_assigns,
        "capabilities": cmd_capabilities,
        "cat": cmd_cat,
        "checksum": cmd_checksum,
        "chmod": cmd_chmod,
        "cp": cmd_cp,
        "devices": cmd_devices,
        "env": cmd_env,
        "exec": cmd_exec,
        "get": cmd_get,
        "kill": cmd_kill,
        "libver": cmd_libver,
        "ls": cmd_ls,
        "mkdir": cmd_mkdir,
        "mv": cmd_mv,
        "ping": cmd_ping,
        "ports": cmd_ports,
        "ps": cmd_ps,
        "put": cmd_put,
        "reboot": cmd_reboot,
        "rm": cmd_rm,
        "run": cmd_run,
        "setcomment": cmd_setcomment,
        "setenv": cmd_setenv,
        "shutdown": cmd_shutdown,
        "signal": cmd_signal,
        "stat": cmd_stat,
        "status": cmd_status,
        "sysinfo": cmd_sysinfo,
        "tail": cmd_tail,
        "tasks": cmd_tasks,
        "trace": cmd_trace,
        "touch": cmd_touch,
        "uptime": cmd_uptime,
        "version": cmd_version,
        "volumes": cmd_volumes,
    }

    # Shell subcommand manages its own connection lifecycle
    if args.command == "shell":
        from .shell import AmigaShell
        sh = AmigaShell(host, port, editor=editor)
        try:
            sh.cmdloop()
        except KeyboardInterrupt:
            print()
        return

    try:
        with AmigaConnection(host, port) as conn:
            dispatch[args.command](conn, args)
    except ConnectionRefusedError:
        print(
            "Error: could not connect to {}:{}".format(host, port),
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
