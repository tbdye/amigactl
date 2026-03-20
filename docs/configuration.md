# Configuration

amigactl reads settings from three sources: CLI flags, environment variables,
and a configuration file. When multiple sources provide the same setting, they
are resolved in a fixed precedence order documented below.

## Global CLI Options

These flags are accepted before any subcommand.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--host` | string | `192.168.6.200` | Hostname or IP address of the amigactld daemon. |
| `--port` | integer | `6800` | TCP port of the amigactld daemon. |
| `--config` | path | `client/amigactl.conf` | Path to the configuration file. |

When `--config` is specified explicitly and the file does not exist or cannot
be parsed, amigactl exits with an error. When the default config path is used,
a missing or unparsable file produces a warning (or is silently ignored if
missing) and processing continues with defaults.

### Examples

```
amigactl --host 10.0.0.5 ping
amigactl --port 7000 version
amigactl --config ~/my-amigactl.conf sysinfo
```

When no subcommand is given, amigactl launches the interactive shell.


## Configuration File

### Format

The configuration file uses INI format, parsed by Python's `configparser`
module. Comments begin with `#`. Section headers are enclosed in square
brackets. Keys and values are separated by `=`.

### Location and Auto-Creation

The default configuration file path is `client/amigactl.conf`, relative to the
amigactl package installation directory (the parent of the `amigactl/` Python
package). Concretely, the code resolves the directory two levels above the
`__main__.py` file. For example, if the package is at
`/home/user/amigactl/client/amigactl/`, the config file is at
`/home/user/amigactl/client/amigactl.conf`.

On first run, if `amigactl.conf` does not exist but `amigactl.conf.example`
does, amigactl automatically copies the example file to create a starter
configuration. When the first run includes `--host` or `--port` flags, those
values are written into the generated config file so they persist for future
invocations. On Windows, the generated config file activates the `notepad`
editor setting instead of `vi`.

### Sections and Keys

#### `[connection]`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `host` | string | `192.168.6.200` | Hostname or IP address of the amigactld daemon. |
| `port` | integer | `6800` | TCP port of the amigactld daemon. |

#### `[editor]`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `command` | string | `vi` (Linux/macOS), `notepad` (Windows) | Editor command for the shell `edit` command. Supports arguments (e.g., `code --wait`). |

### Example Configuration File

```ini
# amigactl client configuration
#
# This file is auto-created from amigactl.conf.example on first run.
# Edit the values below to match your setup.
#
# CLI flags and environment variables override these settings.

[connection]
host = 192.168.6.200
port = 6800

[editor]
# Command to open files for the 'edit' shell command.
# Supports arguments (e.g., "code --wait" for VS Code).
# Override: $VISUAL or $EDITOR environment variables.
# Linux/macOS:
command = vi
# Windows:
# command = notepad
```


## Environment Variables

| Variable | Valid Values | Description |
|----------|-------------|-------------|
| `AMIGACTL_HOST` | Hostname or IP address | Overrides the config file `host` setting. Overridden by `--host`. An empty string is treated as unset (falls through to config/default). |
| `AMIGACTL_PORT` | Integer (TCP port number) | Overrides the config file `port` setting. Overridden by `--port`. A non-integer value causes an immediate error at startup. |
| `AMIGACTL_COLOR` | `always`, `never` | Controls ANSI color output. `always` forces color even when stdout is not a TTY. `never` disables color unconditionally. When unset, color is auto-detected based on TTY status. Case-insensitive. |
| `NO_COLOR` | Any non-empty value | Disables ANSI color output. Follows the [no-color.org](https://no-color.org/) convention. When set to any non-empty string, color is disabled regardless of TTY status. Takes highest priority in color resolution; no other setting overrides it. |
| `VISUAL` | Editor command string | Preferred editor for the shell `edit` command. Takes highest priority in editor resolution. Supports arguments (parsed by `shlex.split`). |
| `EDITOR` | Editor command string | Fallback editor for the shell `edit` command. Used when `VISUAL` is not set. Supports arguments (parsed by `shlex.split`). |


## Resolution Order

Each configurable setting has its own precedence chain. When a value is found
at a higher-priority source, lower-priority sources are not consulted.

### Host

| Priority | Source | Example |
|----------|--------|---------|
| 1 (highest) | `--host` CLI flag | `--host 10.0.0.5` |
| 2 | `AMIGACTL_HOST` environment variable | `export AMIGACTL_HOST=10.0.0.5` |
| 3 | `[connection] host` in config file | `host = 10.0.0.5` |
| 4 (lowest) | Hardcoded default | `192.168.6.200` |

### Port

| Priority | Source | Example |
|----------|--------|---------|
| 1 (highest) | `--port` CLI flag | `--port 7000` |
| 2 | `AMIGACTL_PORT` environment variable | `export AMIGACTL_PORT=7000` |
| 3 | `[connection] port` in config file | `port = 7000` |
| 4 (lowest) | Hardcoded default | `6800` |

### Editor

The editor is only used by the interactive shell's `edit` command. It is
resolved at the moment `edit` is invoked, not at startup.

| Priority | Source | Example |
|----------|--------|---------|
| 1 (highest) | `VISUAL` environment variable | `export VISUAL="code --wait"` |
| 2 | `EDITOR` environment variable | `export EDITOR=nano` |
| 3 | `[editor] command` in config file | `command = vi` |
| 4 (lowest) | Platform default | `vi` (Linux/macOS), `notepad` (Windows) |

Note: the config file editor value is loaded at startup and passed to the
shell. Environment variables `VISUAL` and `EDITOR` are checked at edit-time,
so they can be changed during a session without restarting.

### Color

Color resolution does not follow the same cascading pattern as other settings.
Instead, it uses a decision tree evaluated each time a `ColorWriter` is
instantiated:

1. If `NO_COLOR` is set (any non-empty value): color is **disabled**.
2. If `AMIGACTL_COLOR` is `never`: color is **disabled**.
3. If `AMIGACTL_COLOR` is `always`: color is **enabled**.
4. If stdout is not a TTY: color is **disabled**.
5. On Windows, if running in Windows Terminal (`WT_SESSION` is set) or VT
   processing can be enabled via the console API: color is **enabled**.
6. On non-Windows platforms with a TTY: color is **enabled**.

The `NO_COLOR` variable is checked first, but `AMIGACTL_COLOR=always` at
step 3 overrides the TTY check at step 4, effectively forcing color on even
when piping output. There is no config file setting for color.


## Connection Behavior

### Protocol

amigactl communicates with amigactld over a plain TCP connection. The wire
protocol is text-based, using ISO-8859-1 encoding with bare LF (`\n`) as the
line terminator for outgoing commands. Incoming lines strip a trailing CR if
present, providing CRLF compatibility.

### Default Port

The default TCP port is **6800**. Both the client and daemon use this default.

### Banner Exchange

Immediately after the TCP connection is established, amigactld sends a banner
line. The client reads this line and validates that it begins with the prefix
`AMIGACTL `. If the banner does not match, the connection is closed and a
`ProtocolError` is raised with the message `Invalid banner: <repr>` where
`<repr>` is the Python repr of the received banner string.

The banner string is available via the `AmigaConnection.banner` property after
a successful connection.

### Socket Timeout

The client sets a socket timeout of **30 seconds** by default (the `timeout`
parameter to `AmigaConnection.__init__`). This timeout applies to all socket
read and write operations. There is no CLI flag or config file key to change
this value; it can only be set programmatically via the Python API.

The `exec` subcommand accepts a `--timeout` flag (in seconds) that sets the
socket timeout for that specific command execution, allowing long-running
commands to complete without timing out.

### Connection Refused

When the daemon is not running or the host is unreachable, the TCP connection
raises `ConnectionRefusedError`. The CLI catches this and prints:

```
Error: could not connect to <host>:<port>
```

The interactive shell prints `Failed to connect to <host>:<port>: <error>` and
exits with code 1. The shell catches all connection exceptions (not just
`ConnectionRefusedError`), so this covers DNS failures, timeouts, and other
network errors.

### Connection Lifecycle

For CLI subcommands, each invocation opens a fresh connection, executes the
command, sends `QUIT`, and closes the socket. The connection is managed via a
context manager (`with AmigaConnection(...) as conn:`).

The interactive shell maintains a persistent connection for the duration of
the session. If the connection drops (network error, daemon restart), the
shell detects this when the next command fails and sets the connection to
`None`. The user can re-establish the connection with the `reconnect` command;
the current working directory is preserved across reconnections.

### ACL Rejection

The daemon can be configured with `ALLOW` directives in its config file
(`S:amigactld.conf` on the Amiga). When `ALLOW` lines are present, only
connections from listed IP addresses are accepted. The ACL is enforced on the
daemon side; from the client's perspective, a rejected connection manifests as
a closed connection or connection refused.

### Command History

The interactive shell stores command history in `~/.amigactl_history` using
Python's `readline` module. History is loaded on shell startup and saved on
exit.
