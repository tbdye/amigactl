# File Transfer

The amigactl shell provides commands for moving files between the host
computer and the Amiga. `get` downloads files, `put` uploads them,
`append` adds data to an existing remote file, and `edit` combines
download, local editing, and upload into a single workflow with conflict
detection. All path arguments follow the same resolution rules described
in [navigation.md](navigation.md) -- relative paths are joined with the
current working directory, `..` is translated to Amiga `/` parent
navigation, and paths are validated for ISO-8859-1 encoding before
being sent to the daemon.

File data is transferred as raw bytes -- all commands are binary-safe
and work correctly with both text and binary files. Data is sent in 4KB
chunks over the wire, but this is handled transparently by the protocol
layer.


## Downloading Files

### get

Download a file from the Amiga to the host.

```
get REMOTE [LOCAL]
```

| Argument | Description |
|----------|-------------|
| `REMOTE` | Amiga file path (absolute, or relative to the current directory). |
| `LOCAL` | Destination path on the host (default: current host directory, same filename as the remote file). |

When `LOCAL` is omitted, the file is saved in the host's current
working directory using the basename of the remote path. For example,
downloading `SYS:S/Startup-Sequence` saves the file as
`Startup-Sequence` in the directory where you launched the shell.

When `LOCAL` is provided, it specifies the exact path (including
filename) where the file will be saved on the host.

```bash
amiga@192.168.6.228:SYS:> get SYS:C/WhichAmiga
Downloaded 5384 bytes to WhichAmiga
amiga@192.168.6.228:SYS:> get S/Startup-Sequence /tmp/startup.txt
Downloaded 512 bytes to /tmp/startup.txt
```

If the local file already exists, it is overwritten without
confirmation. If the remote file does not exist, an error is printed
and no local file is created:

```bash
amiga@192.168.6.228:SYS:> get NoSuchFile
Error: Object not found
```

If the local path is not writable (e.g., a read-only directory), the
download succeeds but the local write fails with an error message:

```bash
amiga@192.168.6.228:SYS:> get C/Dir /read-only/Dir
Local write error: [Errno 13] Permission denied: '/read-only/Dir'
```

**Tab completion:** The first argument (the remote path) supports tab
completion against the Amiga filesystem. The second argument (the local
path) does not use tab completion.


## Uploading Files

### put

Upload a file from the host to the Amiga.

```
put LOCAL [REMOTE]
```

| Argument | Description |
|----------|-------------|
| `LOCAL` | File on the host to upload. |
| `REMOTE` | Destination Amiga path (default: current Amiga directory, same filename as the local file). |

When `REMOTE` is omitted, the file is uploaded to the current Amiga
working directory using the basename of the local path. For example,
uploading `/tmp/config.txt` places the file at `SYS:config.txt` if the
CWD is `SYS:`.

The file is written atomically on the Amiga side. If the remote file
already exists, it is overwritten without confirmation.

```bash
amiga@192.168.6.228:SYS:> put startup.txt SYS:S/Startup-Sequence
Uploaded 512 bytes to SYS:S/Startup-Sequence
amiga@192.168.6.228:SYS:> put localfile.txt
Uploaded 1024 bytes to SYS:localfile.txt
```

If the local file does not exist or is not readable, an error is
printed and nothing is uploaded:

```bash
amiga@192.168.6.228:SYS:> put nosuchfile.txt
Local read error: [Errno 2] No such file or directory: 'nosuchfile.txt'
```

If the remote path refers to a location that cannot be written (e.g., a
read-only volume or a nonexistent parent directory), the daemon returns
an error:

```bash
amiga@192.168.6.228:SYS:> put data.bin NoSuchDir/data.bin
Error: Object not found
```

**Tab completion:** The first argument (the local path) does not use
tab completion. The second argument (the remote path) supports tab
completion against the Amiga filesystem.


## Appending Data

### append

Append the contents of a local file to a remote file.

```
append LOCAL REMOTE
```

| Argument | Description |
|----------|-------------|
| `LOCAL` | File on the host whose contents will be appended. |
| `REMOTE` | Amiga file to append to (must already exist). |

Both arguments are required. The remote file must already exist -- if
it does not, the daemon returns an error. The local file's contents are
appended to the end of the remote file without modifying the existing
data.

```bash
amiga@192.168.6.228:SYS:> append extra-lines.txt RAM:logfile.txt
Appended 256 bytes to RAM:logfile.txt
```

If the remote file does not exist:

```bash
amiga@192.168.6.228:SYS:> append data.txt RAM:nonexistent.txt
Error: Object not found
```

If the local file cannot be read:

```bash
amiga@192.168.6.228:SYS:> append missing.txt RAM:logfile.txt
Local read error: [Errno 2] No such file or directory: 'missing.txt'
```

**Use cases.** `append` is useful for incrementally building files on
the Amiga, adding entries to log files, or concatenating data without
downloading and re-uploading the entire file. Combined with `tail`, it
can be used to stream data to a monitored file.

**Tab completion:** The first argument (the local path) does not use
tab completion. The second argument (the remote path) supports tab
completion against the Amiga filesystem.


## Editing Remote Files

### edit

Edit a remote file in a local text editor.

```
edit PATH
```

| Argument | Description |
|----------|-------------|
| `PATH` | Amiga file path to edit (absolute or relative). |

`edit` provides a complete round-trip workflow: it downloads the file,
opens it in a local editor, waits for the editor to exit, detects
whether changes were made, checks for remote conflicts, and uploads the
modified file. This makes it convenient for quick edits to configuration
files, scripts, and other text files on the Amiga without manually
juggling `get` and `put` commands.

### Workflow

The `edit` command proceeds through the following steps:

1. **Record remote datestamp.** The shell calls `stat` on the remote
   file to capture its current datestamp. This baseline is used later
   for conflict detection.

2. **Download.** The file's contents are downloaded from the Amiga. If
   the file does not exist, the shell prints "File does not exist.
   Creating new file." and proceeds with an empty buffer -- this allows
   `edit` to create new files.

3. **Write to temporary file.** The contents are written to a temporary
   file in a dedicated temporary directory
   (`/tmp/amigactl_edit_XXXX/<filename>`). The temporary file preserves
   the remote file's basename, so the editor can display a meaningful
   filename and apply appropriate syntax highlighting.

4. **Launch editor.** The shell opens the temporary file in a local
   editor (see [Editor Selection](#editor-selection) below) and waits
   for the editor process to exit.

5. **Detect local changes.** After the editor exits, the shell compares
   the temporary file's modification time against the value recorded
   before the editor was launched. If the file was not modified, the
   shell prints "No changes detected." and cleans up the temporary
   file without contacting the Amiga.

6. **Report changes.** If the file was modified, the shell prints a
   size comparison:

   ```
   File changed: 512 -> 540 bytes
   ```

7. **Check for remote conflicts.** Before uploading, the shell calls
   `stat` on the remote file again and compares the current datestamp
   to the baseline recorded in step 1. Two conflict scenarios are
   detected:

   - **Existing file modified remotely.** If the datestamp has changed,
     someone (or something) modified the file on the Amiga while you
     were editing. The shell displays a warning with both datestamps
     and prompts for confirmation:

     ```
     Warning: file was modified remotely while editing.
       Remote datestamp was: 2026-01-10 08:00:00
       Remote datestamp now: 2026-01-10 08:05:30
     Upload anyway? [y/N]
     ```

     Answering `y` proceeds with the upload. Any other answer (or
     pressing Enter) cancels the upload and preserves the local copy.

   - **New file created remotely.** If the file did not exist when the
     edit started but now exists on the Amiga, the shell warns and
     prompts:

     ```
     Warning: file was created remotely while editing.
       Remote datestamp: 2026-01-10 08:05:30
     Overwrite? [y/N]
     ```

   If the remote file still does not exist (creating a new file with
   no conflict) or the conflict check fails with a non-connection
   protocol error, the upload proceeds without prompting. If the
   connection is lost during the conflict check, the upload is aborted
   and the local copy is preserved.

8. **Upload.** The modified file is uploaded to the Amiga:

   ```
   Uploaded 540 bytes to SYS:S/User-Startup
   ```

9. **Clean up.** The temporary file and its directory are removed. If
   any step fails (upload error, connection loss, cancelled conflict),
   the temporary file is preserved and its path is printed so you can
   recover your edits:

   ```
   Local copy preserved at: /tmp/amigactl_edit_abc123/User-Startup
   ```

### Editor Selection

The editor is chosen using the following priority (first non-empty value
wins):

| Priority | Source | Example |
|----------|--------|---------|
| 1 | `$VISUAL` environment variable | `code --wait` |
| 2 | `$EDITOR` environment variable | `nano` |
| 3 | Config file (`[editor]` section, `command` key) | `vim` |
| 4 | Platform default | `vi` (Linux/macOS) or `notepad` (Windows) |

The config file defaults to `client/amigactl.conf` in the package
directory, overridable with the `--config` flag:

```ini
[editor]
command = nano
```

The editor command is parsed with shell-style splitting, so editors
that require flags (such as `code --wait` for VS Code) work correctly.
The temporary file path is appended as the final argument.

### Examples

Edit an existing file:

```bash
amiga@192.168.6.228:SYS:> edit S/User-Startup
# ... editor opens with file contents ...
# ... make changes, save, and quit the editor ...
File changed: 128 -> 192 bytes
Uploaded 192 bytes to SYS:S/User-Startup
```

Edit a file that does not exist (creates it):

```bash
amiga@192.168.6.228:SYS:> edit RAM:newfile.txt
File does not exist. Creating new file.
# ... editor opens with empty buffer ...
# ... type contents, save, and quit ...
File changed: 0 -> 45 bytes
Uploaded 45 bytes to RAM:newfile.txt
```

Quit the editor without saving:

```bash
amiga@192.168.6.228:SYS:> edit S/Startup-Sequence
# ... editor opens, you quit without saving ...
No changes detected.
```

Conflict detected during upload:

```bash
amiga@192.168.6.228:SYS:> edit RAM:shared-config.txt
# ... while you edit, another process modifies the file ...
File changed: 100 -> 120 bytes
Warning: file was modified remotely while editing.
  Remote datestamp was: 2026-03-16 10:00:00
  Remote datestamp now: 2026-03-16 10:02:15
Upload anyway? [y/N] n
Upload cancelled.
Local copy saved at: /tmp/amigactl_edit_a1b2c3/shared-config.txt
```

Connection lost after editing:

```bash
amiga@192.168.6.228:SYS:> edit S/User-Startup
# ... daemon goes away while you edit ...
File changed: 128 -> 160 bytes
Connection lost: [Errno 104] Connection reset by peer
Local copy preserved at: /tmp/amigactl_edit_x9y8z7/User-Startup
```

In both failure cases, the local copy is preserved. You can recover by
reconnecting and using `put` to upload the saved file manually.

**Tab completion:** The path argument supports tab completion against the Amiga filesystem.


## Related Documentation

- [navigation.md](navigation.md) -- Path syntax, `cd`/`pwd`, and path
  resolution rules.
- [file-operations.md](file-operations.md) -- File and directory
  management commands (`ls`, `stat`, `cat`, `rm`, etc.).
- [tab-completion.md](tab-completion.md) -- Tab completion and readline
  integration.
- [architecture.md](architecture.md) -- Shell architecture and path
  resolution internals.
