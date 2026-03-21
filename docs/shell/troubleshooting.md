# Troubleshooting

This document covers common issues encountered when using the amigactl
interactive shell and how to resolve them. Each section is organized
by the symptom you observe, followed by likely causes and solutions.


## Connection Issues

### Cannot Connect

**Symptom:** The shell prints an error on startup and exits:

```
Failed to connect to 192.168.6.200:6800: [Errno 111] Connection refused
```

or:

```
Failed to connect to 192.168.6.200:6800: timed out
```

**Likely causes and solutions:**

- **Daemon not running.** Start `amigactld` on the Amiga. From the
  CLI, run `amigactld` or launch it from Workbench. Verify it prints
  a banner like `amigactld 0.8.0 listening on port 6800`.

- **Wrong host or port.** Verify the Amiga's IP address and the
  daemon's listening port. The default port is 6800. Set the correct
  host with `--host`:

  ```
  amigactl --host 192.168.6.200 shell
  ```

  The host can also be configured in `client/amigactl.conf` (relative
  to the project directory), or specify a different config file with
  `--config`.

- **Firewall or network issue.** Ensure the client machine can reach
  the Amiga's IP address. Try `ping 192.168.6.200` from the client.
  If the Amiga is on a different subnet (e.g., behind a TAP interface),
  verify that routing is configured correctly.

- **Maximum clients reached.** The daemon supports up to 8 concurrent
  connections. When all 8 slots are occupied, new connections are
  silently closed by the daemon -- the client sees a connection
  established but immediately closed, which produces a protocol error:

  ```
  Failed to connect to 192.168.6.200:6800: Connection closed by server
  ```

  Disconnect idle sessions from other clients or restart the daemon
  to free all slots.

- **Invalid banner.** If the daemon socket is occupied by a different
  service, or the daemon is in a bad state, you may see:

  ```
  Failed to connect to 192.168.6.200:6800: Invalid banner: ...
  ```

  Verify that amigactld is what is actually listening on the specified
  port. If another program is bound to port 6800, configure amigactld
  to use a different port with the `PORT` argument.


### Connection Lost During Session

**Symptom:** A command fails with a connection error, and the prompt
changes to show no connection:

```
amiga@192.168.6.200:SYS:> ls
Connection error: [Errno 104] Connection reset by peer
amiga>
```

The prompt reverts from `amiga@192.168.6.200:SYS:>` to `amiga>`,
indicating the shell has detected a dead connection.

**Likely causes:**

- The daemon crashed or was shut down (Ctrl-C on the Amiga).
- The Amiga was rebooted.
- A network interruption severed the TCP connection.
- The connection timed out during a long-running operation.

**Solution:** Use the `reconnect` command to re-establish the
connection:

```
amiga> reconnect
Reconnected to 192.168.6.200 (amigactld 0.8.0)
amiga@192.168.6.200:SYS:>
```

The shell preserves the current working directory across reconnection
attempts. If the daemon is not available yet (e.g., the Amiga is
still rebooting), `reconnect` will fail with an error -- simply try
again later.

If `reconnect` reports `Already connected.`, the shell thinks the
connection is still alive even though it is not functioning. This
should not normally happen -- the shell sets `conn` to `None` when
it detects a connection error. If it does, use `exit` and start a
new shell session.


### Timeout Errors

**Symptom:** A command hangs for a long time and then fails:

```
amiga@192.168.6.200:SYS:> cat Work:large-file.bin
Connection error: timed out
amiga>
```

**Cause:** The default socket timeout is 30 seconds. Operations that
take longer than this -- large file transfers, slow commands, or
directory listings of very large directories -- will time out.

**Solutions:**

- For the `exec` subcommand (non-interactive CLI mode), use the
  `--timeout` flag to extend the timeout for a specific command:

  ```
  amigactl exec --timeout 120 -- copy SYS:disk.adf RAM:disk.adf
  ```

- For the interactive shell, the timeout is set at startup and
  applies to all operations for the session. There is currently no
  shell command to change it at runtime. If you routinely work with
  large files or slow operations, consider increasing the timeout
  when launching the shell.

- A timeout breaks the connection. After a timeout, use `reconnect`
  to re-establish the session.


## Command Errors

### "Not found" / "Object not found" / "Directory not found"

**Symptom:**

```
amiga@192.168.6.200:SYS:> ls Work:MyProject
Error: Lock failed: object not found
```

```
amiga@192.168.6.200:SYS:> cd NoSuchDir
Directory not found: SYS:NoSuchDir
```

**Likely causes and solutions:**

- **Path does not exist.** Verify the file or directory name with
  `ls` on the parent directory. Remember that Amiga filesystems are
  case-insensitive but case-preserving -- `Startup-Sequence` and
  `startup-sequence` refer to the same file, but the name must match
  at least one existing entry.

- **Volume or assign not mounted.** If the path starts with a volume
  or assign that is not currently mounted (e.g., `DH1:` when no drive
  is configured, or a late-binding assign that has not been resolved),
  the daemon returns error code 200. Use `volumes` to list mounted
  volumes and `assigns` to list active assigns.

- **Wrong path separator.** Amiga paths use `:` to separate the
  volume from the path and `/` between directories. A path like
  `SYS\C\Dir` is invalid -- use `SYS:C/Dir`. The shell also supports
  `..` as a convenience for navigating to parent directories.

- **Relative path with no CWD set.** If you have not used `cd` to
  set a working directory and use a relative path, the daemon
  receives the bare relative name and cannot resolve it. Use an
  absolute path (with a volume name and colon) or first `cd` into a
  directory.


### "Permission denied" / "Object in use"

**Symptom:**

```
amiga@192.168.6.200:SYS:> rm SYS:Devs
Error: Delete failed: object is in use
```

```
amiga@192.168.6.200:SYS:> put file.txt SYS:C/Dir
Error: Open failed: disk is write-protected
```

**Likely causes:**

- **File locked by another process.** Amiga files can be locked by
  programs that have them open. If a program is reading or writing a
  file, attempts to delete, rename, or write to it may fail with
  "object in use". Close the program holding the lock and retry.

- **Directory not empty.** The `rm` command cannot delete a directory
  that contains files. Remove the contents first, or use `exec` to
  run an AmigaOS command that supports recursive deletion.

- **Protection bits deny access.** Amiga protection bits can prevent
  read, write, execute, or delete operations. Use `chmod` to view
  the current protection bits and modify them if needed. The `d`
  (delete) bit must be clear (allowed) to delete a file. The `w`
  (write) bit must be clear to write to it.

- **Write-protected volume.** If the volume is write-protected (e.g.,
  a ROM disk or a floppy with the write-protect tab set), all write
  operations will fail.

- **Daemon configuration restriction.** The `kill`, `shutdown`, and
  `reboot` commands require specific configuration options
  (`ALLOW_REMOTE_SHUTDOWN`, `ALLOW_REMOTE_REBOOT`) to be enabled in
  the daemon's configuration file (`S:amigactld.conf`). Without them,
  the daemon returns:

  ```
  Error: Remote kill not permitted
  Error: Remote shutdown not permitted
  Error: Remote reboot not permitted
  ```


### "Path contains characters not representable in ISO-8859-1"

**Symptom:**

```
amiga@192.168.6.200:SYS:> cat /path/to/file\u2019s-name
Path contains characters not representable in ISO-8859-1: ...
```

**Cause:** The amigactl protocol uses ISO-8859-1 encoding on the
wire. File paths that contain characters outside this encoding (such
as Unicode curly quotes, emoji, CJK characters, or other non-Latin-1
code points) cannot be transmitted to the daemon.

**Solution:** Rename the file on the client side to use only characters
within the ISO-8859-1 range (ASCII plus Western European accented
characters) before transferring or referencing it. This limitation is
inherent to the Amiga's character set and the protocol design.


## File Transfer Issues

### Large File Transfers Slow or Timing Out

**Symptom:** Downloading or uploading large files takes a very long
time, or times out before completing.

**Cause:** File transfers use the protocol's DATA/END chunked encoding
over TCP, with a default 4096-byte chunk size. The entire file
content is held in memory on both sides. Very large files (tens of
megabytes) take significant time over a 10 Mbit/s Amiga Ethernet
connection, and may exceed the 30-second socket timeout.

**Solutions:**

- For partial reads, use `cat --offset N --length N` to read a
  specific portion of a file without downloading the whole thing.
- Transfer large files in segments if the timeout is a concern.
- If the transfer times out, the connection is broken. Use
  `reconnect` and retry the operation.


### Edit Conflict Detection

**Symptom:** After saving changes in your editor, the shell warns:

```
Warning: file was modified remotely while editing.
  Remote datestamp was: 2026-03-15 10:30:00
  Remote datestamp now: 2026-03-15 10:35:22
Upload anyway? [y/N]
```

Or for a new file:

```
Warning: file was created remotely while editing.
  Remote datestamp: 2026-03-15 10:35:22
Overwrite? [y/N]
```

**Cause:** The `edit` command downloads the file, opens it in your
local editor, and uploads changes when you save. Before uploading, it
checks whether the file's datestamp on the Amiga has changed since the
download. If another process or user modified the file while you were
editing, the shell warns you to prevent accidentally overwriting their
changes.

**Solutions:**

- Answer `y` if you are sure your version should replace the remote
  one.
- Answer `n` to cancel the upload. The shell preserves your edited
  copy in a temporary directory and prints its path so you can recover
  your changes:

  ```
  Upload cancelled.
  Local copy saved at: /tmp/amigactl_edit_abc123/Startup-Sequence
  ```

- If the connection is lost during the edit session, the local copy
  is also preserved with a message indicating its path.


## Execution Issues

### exec Appears to Hang

**Symptom:** After running `exec` with a command, the shell does not
return to the prompt for a long time.

**Cause:** `exec` is synchronous -- it blocks the shell until the
command on the Amiga finishes. More importantly, the daemon processes
`exec` commands in its main event loop, which means a long-running
`exec` blocks all other connected clients from receiving service.

**Solutions:**

- Press Ctrl-C to interrupt (note: this interrupts the Python
  client's wait, not the command on the Amiga -- the remote command
  may continue running).
- Use `run` instead of `exec` for commands that take more than a few
  seconds. `run` launches the command asynchronously and returns a
  process ID immediately. Use `ps` to monitor progress and `status ID`
  to check the return code when it finishes.
- Be aware that interactive commands expecting stdin input will hang
  indefinitely -- the daemon connects stdin to `NIL:` (AmigaOS
  equivalent of `/dev/null`), so the command sees no input and may
  wait forever. Only use `exec` with non-interactive commands.


### Background Process Won't Respond to signal

**Symptom:**

```
amiga@192.168.6.200:SYS:> signal 1
Signal sent.
```

The signal is reported as sent, but the process does not stop.

**Likely causes:**

- **The program ignores break signals.** AmigaOS break signals are
  cooperative -- the target program must call `CheckSignal()` or
  `SetSignal()` to detect them. If the program does not check for
  signals, it will not respond to `signal`.

- **SystemTags fallback.** When the daemon cannot find the command
  binary directly (e.g., for shell scripts or built-in commands), it
  falls back to `SystemTags()`, which creates a child shell process.
  In this case, the signal is delivered to the wrapper process, not
  the child shell or the actual command. The signal may not propagate
  to the intended target.

**Solutions:**

- Try sending the signal multiple times -- some programs only check
  for signals periodically.
- If the process is truly unresponsive, use `kill ID` as a last
  resort. This forcibly removes the process with `RemTask()`, which
  does not give it any opportunity to clean up. The `kill` command
  requires `ALLOW_REMOTE_SHUTDOWN YES` in the daemon configuration.
- For better signal delivery, run binary executables directly rather
  than through scripts. When the daemon can find the binary on disk,
  it uses `RunCommand()`, which executes the command in the wrapper
  process context where signals are directly visible.


### "Process table full"

**Symptom:**

```
amiga@192.168.6.200:SYS:> run mycommand
Error: Process table full
```

**Cause:** The daemon tracks up to 16 background processes. If all 16
slots are occupied by currently running processes, no new background
processes can be started. Note that exited processes also occupy slots
until they are evicted by a new `run` command (the oldest exited slot
is reused automatically). The error only occurs when all 16 slots hold
running processes with none having exited.

**Solutions:**

- Use `ps` to list all tracked processes. If some are shown as
  `EXITED` in `ps` output, new `run` commands will automatically reuse
  their slots -- the error should not persist.
- If all 16 processes are genuinely still running, wait for some to
  finish, or use `signal` or `kill` to stop processes you no longer
  need.
- Use `exec` instead of `run` for quick commands that do not need
  background execution. `exec` runs synchronously and does not
  consume a process table slot.


## Tab Completion Issues

### Tab Completion Returns No Results

**Symptom:** Pressing Tab produces no completions, even for paths
that you know exist.

**Likely causes:**

- **Connection lost.** If the connection is dead, the completion
  function returns an empty list silently (it does not print an error
  message -- that would disrupt the line being edited). Check the
  prompt: if it shows `amiga>` instead of
  `amiga@192.168.6.200:SYS:>`, the connection is gone. Use
  `reconnect` to restore it.

- **No current working directory.** For relative paths, tab
  completion needs a CWD to resolve the directory to query. If you
  have not used `cd` and type a relative path, completion returns
  nothing. Either use `cd` to set a working directory, or type an
  absolute path (e.g., `SYS:S/`) before pressing Tab.

- **Directory does not exist.** If the directory portion of your
  partial path does not exist on the Amiga, the daemon returns an
  error, which the completion function silently swallows (returning
  an empty list). Verify the path prefix is correct.

- **Cache staleness.** The directory cache has a 5-second TTL. If a
  file was created less than 5 seconds ago by another process, it
  may not appear in completions yet. Wait a moment and try again, or
  run any file-modifying command (`put`, `rm`, `mkdir`, etc.) to
  force a cache invalidation.


### Tab Completion Is Slow

**Symptom:** There is a noticeable delay between pressing Tab and
seeing results.

**Likely causes:**

- **Cache miss.** The first Tab press for a new directory requires a
  round-trip to the daemon to list the directory contents. On a
  10 Mbit/s Amiga Ethernet link, this typically takes 50-200ms for
  small directories, but can be noticeably longer for directories with
  hundreds of entries.

- **Network latency.** If the Amiga is on a remote network or behind
  high-latency links, every uncached completion request adds that
  latency.

- **Large directories.** Directories with many files take longer to
  transfer and parse. The cache mitigates this for subsequent Tab
  presses within the same directory (within the 5-second TTL).

**Solution:** Subsequent Tab presses within the same directory use
the cached result and are instantaneous for 5 seconds. There is no
way to increase the cache TTL at runtime. If completion latency is
consistently a problem, consider narrowing your path prefix to target
smaller directories.


## Related Documentation

- [architecture.md](architecture.md) -- Shell internals, error
  handling, and connection management.
- [navigation.md](navigation.md) -- Path syntax, resolution, and the
  `cd` and `pwd` commands.
- [file-transfer.md](file-transfer.md) -- File transfer commands
  (`get`, `put`, `append`, `edit`).
- [command-execution.md](command-execution.md) -- `exec`, `run`, and
  process management details.
- [tab-completion.md](tab-completion.md) -- Tab completion and
  directory cache behavior.
