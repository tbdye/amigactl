# Traced Functions Reference

atrace captures calls to 99 functions across 7 AmigaOS libraries. This
document is the canonical reference for per-function metadata: argument
registers, string capture behavior, indirect dereference types, return value
semantics, error classification, and tier assignment.

For the binary layout of captured events, see [event-format.md](event-format.md).
For how to interpret formatted output, see [reading-output.md](reading-output.md).
For tier system details, see [output-tiers.md](output-tiers.md).

---

## How to Read the Tables

Each library section contains a summary table with these columns:

| Column | Meaning |
|--------|---------|
| Function | AmigaOS function name |
| LVO | Library Vector Offset (negative, as used in the jump table) |
| Args | Number of arguments in the AmigaOS prototype. For functions with more than 4 args, only the first 4 register values are captured in the event. |
| Arg Registers | Which 68k registers carry the captured arguments, in capture order |
| String | How string data is captured (see [String Capture](#string-capture-modes) below) |
| Deref | Indirect dereference type applied to arg0 (see [Dereference Types](#dereference-types) below) |
| Return | Return value type (see [Return Types](#return-types) below) |
| Error Check | How errors are detected (see [Error Check Types](#error-check-types) below) |
| Tier | Output tier: Basic, Detail, Verbose, or Manual |

**Special annotations in the function detail sections:**

- **skip_null_arg** -- When a specific register is NULL, the stub suppresses
  the event entirely. This filters noise from common NULL-argument calls
  (e.g., `FindTask(NULL)` to find the current task).
- **Dual-string** -- Two strings are captured into string_data, split at
  offset 32 (first string in bytes 0-31, second in bytes 32-63, each with
  a 31-character maximum before truncation).

---

## String Capture Modes

The `string_args` field is a bitmask indicating which arguments are captured
as NUL-terminated strings into the event's 64-byte `string_data` field:

| Mode | string_args | Behavior |
|------|-------------|----------|
| None | 0x00 | No string capture. `string_data` is unused (or populated by a deref type). |
| Arg0 | 0x01 | Argument 0 is read as a C string pointer. Up to 63 characters are copied into `string_data`. |
| Arg1 | 0x02 | Argument 1 is read as a C string pointer. |
| Arg0+Arg1 | 0x03 | Both arg0 and arg1 are strings. Dual-string mode: arg0 occupies bytes 0-31, arg1 occupies bytes 32-63. Each string has a 31-character maximum. |
| Arg2 | 0x04 | Argument 2 (capture slot index) is read as a C string pointer. |

When a string argument exceeds the available space, the daemon appends `...`
to indicate truncation in formatted output.

---

## Dereference Types

When `name_deref_type` is non-zero, the stub follows pointer chains from arg0
to extract a meaningful name into `string_data`. This happens instead of (or
in addition to) direct string capture.

| Constant | Value | Behavior |
|----------|-------|----------|
| DEREF_NONE | 0 | No indirect capture. String data comes from `string_args` or is empty. |
| DEREF_LN_NAME | 1 | One-level dereference: reads `struct Node.ln_Name` at offset 10 from the pointer in arg0. Used for named exec objects (ports, semaphores, libraries, devices). |
| DEREF_IOREQUEST | 2 | Two-level dereference: reads `IORequest->io_Device->ln_Name` to get the device name, and captures `io_Command` into `args[1]`. Shows the device name and command number for I/O operations. |
| DEREF_TEXTATTR | 3 | Reads `TextAttr->ta_Name` (offset 0) as a string, and copies `ta_YSize` (font size, offset 4) into `args[1]`. Used for OpenFont to show font name and size. |
| DEREF_LOCK_VOLUME | 4 | Follows a BPTR lock through `fl_Volume` to `dol_Name` (a BSTR) to extract the volume name. Used by CurrentDir to identify which volume a lock refers to. |
| DEREF_NW_TITLE | 5 | Reads `NewWindow.Title` at offset 26 from the structure pointer. Used by OpenWindow and OpenWindowTagList to capture the window title before the window is created. |
| DEREF_WIN_TITLE | 6 | Reads `Window.Title` at offset 32 from the window pointer. Used by CloseWindow, ActivateWindow, WindowToFront, WindowToBack. |
| DEREF_NS_TITLE | 7 | Reads `NewScreen.DefaultTitle` at offset 20. Used by OpenScreen and OpenScreenTagList. |
| DEREF_SCR_TITLE | 8 | Reads `Screen.Title` at offset 22. Used by CloseScreen. |

---

## Return Types

The daemon formats return values according to per-function semantics. The
`result_type` field determines both the display format and the status
indicator character (`O` for OK, `E` for error, `-` for neutral/void).

| Constant | Value | Display Format | Status Logic |
|----------|-------|----------------|--------------|
| RET_PTR | 0 | `0x%08lx` for non-NULL, `NULL` for zero | O if non-NULL, E if NULL |
| RET_BOOL_DOS | 1 | `OK` for non-zero, `FAIL` for zero | O if non-zero, E if zero. Handles both DOSTRUE (-1) and standard TRUE (1). |
| RET_NZERO_ERR | 2 | `OK` for zero, `err=N` for non-zero | O if zero, E if non-zero. Used where 0 means success (OpenDevice, DoIO). |
| RET_VOID | 3 | `(void)` always | `-` always. Function has no meaningful return value. |
| RET_MSG_PTR | 4 | `0x%08lx` for non-NULL, `(empty)` for NULL | O if non-NULL, `-` if NULL. NULL is normal (no message available), not an error. |
| RET_RC | 5 | `rc=N` (signed decimal) | O if rc=0, E if rc!=0 |
| RET_LOCK | 6 | `0x%08lx` for non-NULL, `NULL` for zero | O if non-NULL, E if NULL. Displayed as hex BPTR address. |
| RET_LEN | 7 | Signed decimal; `-1` for failure, `N` for count | O if >=0, E if -1 |
| RET_OLD_LOCK | 8 | Cached path name if known, `(none)` for NULL, hex otherwise | `-` always. Informational only (CurrentDir's previous lock). |
| RET_PTR_OPAQUE | 9 | `OK` for non-NULL, `NULL` for zero | O if non-NULL, E if NULL. Unlike RET_PTR, does not show the hex address -- the pointer value itself is not useful to the user. |
| RET_EXECUTE | 10 | `rc=0` for zero, `OK` for DOSTRUE (-1), `rc=N` otherwise | `-` always. Execute() return semantics are ambiguous between shell rc and DOS boolean; neutral status avoids false error classification. |
| RET_IO_LEN | 11 | Signed decimal; `-1` for error, `N` for count | O if >=0, E if -1. Used for I/O byte counts (Read, Write, Seek) and BSD socket return values. |

---

## Error Check Types

The `error_check` field determines whether a call is shown in `--errors`
filtered output. It classifies which return values indicate failure.

| Constant | Value | Condition Shown in --errors |
|----------|-------|-----------------------------|
| ERR_CHECK_NULL | 0 | retval == 0 (NULL or FALSE) |
| ERR_CHECK_NZERO | 1 | retval != 0 (0 means success) |
| ERR_CHECK_VOID | 2 | Never shown -- void functions cannot fail |
| ERR_CHECK_ANY | 3 | Always shown -- no clear error convention, user must interpret |
| ERR_CHECK_NONE | 4 | Never shown -- NULL/zero is a normal expected result, not an error |
| ERR_CHECK_RC | 5 | retval != 0 (return code convention: 0 is success) |
| ERR_CHECK_NEGATIVE | 6 | (LONG)retval < 0 (signed comparison; -1 is failure, >=0 is count) |
| ERR_CHECK_NEG1 | 7 | retval == 0xFFFFFFFF (-1 as unsigned; the standard BSD socket error) |

**IoErr annotation:** For dos.library functions that return with status `E`,
the daemon appends the AmigaOS IoErr code and its symbolic name when available
(e.g., `NULL (205, OBJECT_NOT_FOUND)`). This only appears when the event's
`FLAG_HAS_IOERR` flag is set and the event has completed (valid==1).

---

## exec.library (31 functions)

| Function | LVO | Args | Arg Registers | String | Deref | Return | Error Check | Tier |
|----------|-----|------|---------------|--------|-------|--------|-------------|------|
| FindPort | -390 | 1 | a1 | arg0 | NONE | PTR_OPAQUE | NULL | Manual |
| FindResident | -96 | 1 | a1 | arg0 | NONE | PTR_OPAQUE | NULL | Basic |
| FindSemaphore | -594 | 1 | a1 | arg0 | NONE | PTR_OPAQUE | NULL | Manual |
| FindTask | -294 | 1 | a1 | arg0 | NONE | PTR_OPAQUE | NULL | Manual |
| OpenDevice | -444 | 4 | a0, d0, a1, d1 | arg0 | NONE | NZERO_ERR | NZERO | Basic |
| OpenLibrary | -552 | 2 | a1, d0 | arg0 | NONE | PTR | NULL | Basic |
| OpenResource | -498 | 1 | a1 | arg0 | NONE | PTR_OPAQUE | NULL | Basic |
| GetMsg | -372 | 1 | a0 | -- | LN_NAME | MSG_PTR | NONE | Manual |
| PutMsg | -366 | 2 | a0, a1 | -- | LN_NAME | VOID | VOID | Manual |
| ObtainSemaphore | -564 | 1 | a0 | -- | LN_NAME | VOID | VOID | Manual |
| ReleaseSemaphore | -570 | 1 | a0 | -- | LN_NAME | VOID | VOID | Manual |
| AllocMem | -198 | 2 | d0, d1 | -- | NONE | PTR | NULL | Manual |
| DoIO | -456 | 1 | a1 | -- | IOREQUEST | NZERO_ERR | NZERO | Manual |
| SendIO | -462 | 1 | a1 | -- | IOREQUEST | VOID | VOID | Manual |
| WaitIO | -474 | 1 | a1 | -- | IOREQUEST | NZERO_ERR | NZERO | Manual |
| AbortIO | -480 | 1 | a1 | -- | IOREQUEST | NZERO_ERR | ANY | Manual |
| CheckIO | -468 | 1 | a1 | -- | IOREQUEST | PTR | ANY | Manual |
| FreeMem | -210 | 2 | a1, d0 | -- | NONE | VOID | VOID | Manual |
| AllocVec | -684 | 2 | d0, d1 | -- | NONE | PTR | NULL | Manual |
| FreeVec | -690 | 1 | a1 | -- | NONE | VOID | VOID | Manual |
| Wait | -318 | 1 | d0 | -- | NONE | PTR | ANY | Manual |
| Signal | -324 | 2 | a1, d0 | -- | NONE | VOID | VOID | Manual |
| AllocSignal | -330 | 1 | d0 | -- | NONE | IO_LEN | NEG1 | Detail |
| FreeSignal | -336 | 1 | d0 | -- | NONE | VOID | VOID | Detail |
| CreateMsgPort | -666 | 0 | -- | -- | NONE | PTR | NULL | Detail |
| DeleteMsgPort | -672 | 1 | a0 | -- | LN_NAME | VOID | VOID | Detail |
| CloseLibrary | -414 | 1 | a1 | -- | LN_NAME | VOID | VOID | Detail |
| CloseDevice | -450 | 1 | a1 | -- | IOREQUEST | VOID | VOID | Basic |
| ReplyMsg | -378 | 1 | a1 | -- | NONE | VOID | VOID | Manual |
| AddPort | -354 | 1 | a1 | -- | LN_NAME | VOID | VOID | Manual |
| WaitPort | -384 | 1 | a0 | -- | LN_NAME | MSG_PTR | ANY | Manual |

### exec.library Function Details

**FindPort** (-390) -- Searches the system port list by name. Arg0 (a1) is
captured as a string (the port name). Returns PTR_OPAQUE: displays `OK` if
found, `NULL` if not. Manual tier due to extreme call frequency in
message-passing code.

**FindResident** (-96) -- Searches the resident module list by name. Arg0 (a1)
is captured as a string. Returns PTR_OPAQUE. Basic tier -- typically called
only during startup.

**FindSemaphore** (-594) -- Searches the system semaphore list by name. Arg0
(a1) is the semaphore name string. Returns PTR_OPAQUE. Manual tier.

**FindTask** (-294) -- Finds a task/process by name. Arg0 (a1) is captured as
a string. **skip_null_arg=a1**: when a1 is NULL (meaning "find myself"), the
event is suppressed entirely because self-lookups are extremely frequent and
uninformative. Returns PTR_OPAQUE. Manual tier.

**OpenDevice** (-444) -- Opens an Amiga device. Four arguments captured: a0
(device name string), d0 (unit number), a1 (IORequest pointer), d1 (flags).
Arg0 is a string. Formatted output shows the device name, unit number, and
flags (if non-zero). Returns NZERO_ERR (0 = success, non-zero = error code).
Error check NZERO: shown in `--errors` when the return is non-zero. Basic tier.

**OpenLibrary** (-552) -- Opens an Amiga shared library. Two arguments: a1
(library name string), d0 (minimum version). Formatted as `"name",vN`.
Returns PTR (hex address of library base, or NULL on failure). Basic tier.

**OpenResource** (-498) -- Opens an Amiga resource. Arg0 (a1) is the resource
name string. Returns PTR_OPAQUE. Basic tier.

**GetMsg** (-372) -- Retrieves a message from a port. Arg0 (a0) is the port
pointer; DEREF_LN_NAME extracts the port's `ln_Name`. Returns MSG_PTR: shows
the message address if available, `(empty)` if the port queue was empty. Error
check NONE: a NULL return (empty queue) is normal, never shown in `--errors`.
Manual tier.

**PutMsg** (-366) -- Sends a message to a port. Two args: a0 (port), a1
(message). DEREF_LN_NAME extracts the port name from a0. Formatted as the
port name and message address. Returns VOID. Manual tier.

**ObtainSemaphore** (-564) -- Acquires a semaphore (blocks if held). Arg0 (a0)
is the semaphore pointer; DEREF_LN_NAME extracts the semaphore name. Returns
VOID. Manual tier.

**ReleaseSemaphore** (-570) -- Releases a semaphore. Arg0 (a0) with
DEREF_LN_NAME. Returns VOID. Manual tier.

**AllocMem** (-198) -- Allocates memory. Two args: d0 (size in bytes), d1
(requirements flags). The daemon formats d1 as symbolic MEMF flags
(e.g., `MEMF_PUBLIC|MEMF_CLEAR`). Returns PTR (address of allocated block, or
NULL on failure). Manual tier due to extreme frequency.

**DoIO** (-456) -- Performs synchronous I/O. Arg0 (a1) is the IORequest;
DEREF_IOREQUEST extracts the device name and command number. Formatted as
`"device.name" CMD N`. Returns NZERO_ERR. Manual tier.

**SendIO** (-462) -- Initiates asynchronous I/O. Arg0 (a1) with
DEREF_IOREQUEST. Returns VOID. Manual tier.

**WaitIO** (-474) -- Waits for asynchronous I/O to complete. Arg0 (a1) with
DEREF_IOREQUEST. Returns NZERO_ERR (0 = success). Manual tier.

**AbortIO** (-480) -- Aborts an in-progress I/O request. Arg0 (a1) with
DEREF_IOREQUEST. Returns NZERO_ERR. Error check ANY: always shown in
`--errors` because the return semantics are device-dependent. Manual tier.

**CheckIO** (-468) -- Checks whether an I/O request has completed. Arg0 (a1)
with DEREF_IOREQUEST. Returns PTR (non-NULL if complete, NULL if still
pending). Error check ANY: the NULL/non-NULL meaning depends on context.
Manual tier.

**FreeMem** (-210) -- Frees memory. Two args: a1 (address), d0 (size).
Formatted as `0xADDR,SIZE`. Returns VOID. Manual tier.

**AllocVec** (-684) -- Allocates memory with tracked size. Two args: d0
(size), d1 (requirements). Formatted with symbolic MEMF flags. Returns PTR.
Manual tier.

**FreeVec** (-690) -- Frees AllocVec'd memory. Arg0 (a1) is the address.
Returns VOID. Manual tier.

**Wait** (-318) -- Waits for signals. Arg0 (d0) is the signal mask, formatted
as symbolic signal names (e.g., `SIGF_SINGLE|SIGF_DOS`). Returns PTR (the
received signal mask, displayed as hex). Error check ANY. Manual tier.

**Signal** (-324) -- Sends signals to a task. Two args: a1 (task pointer), d0
(signal set). The daemon resolves the task pointer to a name via cache lookup,
and formats the signal set as symbolic names. Returns VOID. Manual tier.

**AllocSignal** (-330) -- Allocates a signal bit. Arg0 (d0) is the requested
signal number (-1 for any). Formatted as `sig=N`. Returns IO_LEN (the
allocated signal number as signed decimal; -1 indicates failure). Error check
NEG1. Detail tier.

**FreeSignal** (-336) -- Frees a signal bit. Arg0 (d0) formatted as `sig=N`.
Returns VOID. Detail tier.

**CreateMsgPort** (-666) -- Creates a new message port. No arguments. Returns
PTR (the port address, or NULL on failure). Detail tier.

**DeleteMsgPort** (-672) -- Deletes a message port. Arg0 (a0) with
DEREF_LN_NAME to show the port name. Returns VOID. Detail tier.

**CloseLibrary** (-414) -- Closes a shared library. Arg0 (a1) with
DEREF_LN_NAME to show the library name. Returns VOID. Detail tier.

**CloseDevice** (-450) -- Closes a device. Arg0 (a1) with DEREF_IOREQUEST to
show the device name and command. Returns VOID. Basic tier.

**ReplyMsg** (-378) -- Replies to a message. Arg0 (a1) is the message pointer,
formatted as `msg=0xADDR`. No deref, no string capture. Returns VOID. Manual
tier.

**AddPort** (-354) -- Adds a named port to the system list. Arg0 (a1) with
DEREF_LN_NAME to show the port name. Returns VOID. Manual tier.

**WaitPort** (-384) -- Waits for a message to arrive at a port. Arg0 (a0) with
DEREF_LN_NAME to show the port name. Returns MSG_PTR. Error check ANY. Manual
tier.

---

## dos.library (26 functions)

| Function | LVO | Args | Arg Registers | String | Deref | Return | Error Check | Tier |
|----------|-----|------|---------------|--------|-------|--------|-------------|------|
| Open | -30 | 2 | d1, d2 | arg0 | NONE | PTR | NULL | Basic |
| Close | -36 | 1 | d1 | -- | NONE | BOOL_DOS | NULL | Basic |
| Lock | -84 | 2 | d1, d2 | arg0 | NONE | LOCK | NULL | Basic |
| DeleteFile | -72 | 1 | d1 | arg0 | NONE | BOOL_DOS | NULL | Basic |
| Execute | -222 | 3 | d1, d2, d3 | arg0 | NONE | EXECUTE | NONE | Basic |
| GetVar | -906 | 4 | d1, d2, d3, d4 | arg0 | NONE | LEN | NEGATIVE | Basic |
| FindVar | -918 | 2 | d1, d2 | arg0 | NONE | PTR_OPAQUE | NULL | Basic |
| LoadSeg | -150 | 1 | d1 | arg0 | NONE | PTR | NULL | Basic |
| NewLoadSeg | -768 | 2 | d1, d2 | arg0 | NONE | PTR | NULL | Basic |
| CreateDir | -120 | 1 | d1 | arg0 | NONE | LOCK | NULL | Basic |
| MakeLink | -444 | 3 | d1, d2, d3 | arg0+arg1 | NONE | BOOL_DOS | NULL | Basic |
| Rename | -78 | 2 | d1, d2 | arg0+arg1 | NONE | BOOL_DOS | NULL | Basic |
| RunCommand | -504 | 4 | d1, d2, d3, d4 | -- | NONE | RC | RC | Basic |
| SetVar | -900 | 4 | d1, d2, d3, d4 | arg0 | NONE | BOOL_DOS | NULL | Basic |
| DeleteVar | -912 | 2 | d1, d2 | arg0 | NONE | BOOL_DOS | NULL | Basic |
| SystemTagList | -606 | 2 | d1, d2 | arg0 | NONE | RC | RC | Basic |
| AddDosEntry | -678 | 1 | d1 | -- | NONE | BOOL_DOS | NULL | Basic |
| CurrentDir | -126 | 1 | d1 | -- | LOCK_VOLUME | OLD_LOCK | VOID | Basic |
| Read | -42 | 3 | d1, d2, d3 | -- | NONE | IO_LEN | NEG1 | Manual |
| Write | -48 | 3 | d1, d2, d3 | -- | NONE | IO_LEN | NEG1 | Manual |
| UnLock | -90 | 1 | d1 | -- | NONE | VOID | VOID | Detail |
| Examine | -102 | 2 | d1, d2 | -- | NONE | BOOL_DOS | NULL | Detail |
| ExNext | -108 | 2 | d1, d2 | -- | NONE | BOOL_DOS | NULL | Verbose |
| Seek | -66 | 3 | d1, d2, d3 | -- | NONE | IO_LEN | NEG1 | Detail |
| SetProtection | -186 | 2 | d1, d2 | arg0 | NONE | BOOL_DOS | NULL | Basic |
| UnLoadSeg | -156 | 1 | d1 | -- | NONE | VOID | VOID | Detail |

### dos.library Function Details

**Open** (-30) -- Opens a file or device. Two args: d1 (filename string), d2
(access mode). The daemon formats the access mode as a symbolic name
(MODE_OLDFILE, MODE_NEWFILE, MODE_READWRITE). Returns PTR (file handle
address). The daemon maintains a file handle cache so that subsequent Close
calls can display the filename. Basic tier.

**Close** (-36) -- Closes a file handle. Arg0 (d1) is the file handle. The
daemon looks up the file handle in its cache to display the associated
filename, then removes the cache entry. Returns BOOL_DOS. Basic tier.

**Lock** (-84) -- Obtains a lock on a file or directory. Two args: d1 (path
string), d2 (lock type). The daemon formats the lock type as SHARED_LOCK or
EXCLUSIVE_LOCK. Returns LOCK (BPTR address). The daemon caches the
lock-to-path mapping for later display by UnLock and CurrentDir. Basic tier.

**DeleteFile** (-72) -- Deletes a file or empty directory. Arg0 (d1) is the
path string. Returns BOOL_DOS. Basic tier.

**Execute** (-222) -- Executes a CLI command string. Three args: d1 (command
string), d2 (input file handle), d3 (output file handle). Formatted output
shows the command string and non-zero I/O handles. Returns EXECUTE with
neutral status: rc=0 means the shell completed successfully, DOSTRUE (-1) is
displayed as `OK`, and other values are shown as `rc=N`. Error check NONE:
never shown in `--errors` because the return semantics are ambiguous. Basic
tier.

**GetVar** (-906) -- Reads an environment variable. Four args: d1 (variable
name string), d2 (buffer), d3 (buffer size), d4 (flags). The daemon decodes
d4 to show scope (GLOBAL, LOCAL, or ANY based on GVF_GLOBAL_ONLY and
GVF_LOCAL_ONLY flags). Returns LEN (character count on success, -1 on
failure). Error check NEGATIVE: shown in `--errors` when the signed return is
negative. Basic tier.

**FindVar** (-918) -- Finds a local variable in the current process. Two args:
d1 (name string), d2 (type code). The daemon decodes d2 as LV_VAR or
LV_ALIAS. Returns PTR_OPAQUE. Basic tier.

**LoadSeg** (-150) -- Loads an executable from disk. Arg0 (d1) is the filename
string. Returns PTR (seglist BPTR). The daemon caches the seglist-to-name
mapping for later display by RunCommand and UnLoadSeg. Basic tier.

**NewLoadSeg** (-768) -- Loads an executable with tag options. Two args: d1
(filename string), d2 (taglist). Returns PTR. Basic tier.

**CreateDir** (-120) -- Creates a directory. Arg0 (d1) is the path string.
Returns LOCK (a lock on the new directory). Basic tier.

**MakeLink** (-444) -- Creates a filesystem link. Three args: d1 (link name),
d2 (destination path or lock), d3 (soft link flag). **Dual-string capture**
(string_args=0x03): link name in string_data[0..31], destination in
string_data[32..63]. Formatted as `"linkName" -> "destPath" soft/hard`.
Returns BOOL_DOS. Basic tier.

**Rename** (-78) -- Renames a file or directory. Two args: d1 (old name), d2
(new name). **Dual-string capture** (string_args=0x03): old name in
string_data[0..31], new name in string_data[32..63]. Formatted as
`"oldName" -> "newName"`. Returns BOOL_DOS. Basic tier.

**RunCommand** (-504) -- Runs a loaded program. Four args: d1 (seglist BPTR),
d2 (stack size), d3 (parameter string pointer), d4 (parameter length). No
direct string capture -- the daemon resolves the seglist to a name via its
cache (populated by LoadSeg). Formatted as `"progname",stack=N,paramlen`.
Returns RC (signed return code; 0 = success). Basic tier.

**SetVar** (-900) -- Sets an environment variable. Four args: d1 (name
string), d2 (value buffer), d3 (value size), d4 (flags). The daemon decodes
scope from d4. Returns BOOL_DOS. Basic tier.

**DeleteVar** (-912) -- Deletes an environment variable. Two args: d1 (name
string), d2 (flags). The daemon decodes scope from d2. Returns BOOL_DOS.
Basic tier.

**SystemTagList** (-606) -- Executes a command string with tag options. Two
args: d1 (command string), d2 (taglist). Returns RC. Basic tier.

**AddDosEntry** (-678) -- Adds an entry to the DOS device list. Arg0 (d1) is
the DosList pointer, formatted as hex. No string capture. Returns BOOL_DOS.
Basic tier.

**CurrentDir** (-126) -- Changes the current directory. Arg0 (d1) is the new
lock. DEREF_LOCK_VOLUME extracts the volume name from the lock's fl_Volume
field (e.g., `"SYS:?"`; the `?` suffix indicates the subdirectory path is not
available from the stub data alone). The daemon also consults a lock cache for
full path names. Returns OLD_LOCK with neutral status: shows the previous
directory lock as a path name (from cache), `(none)` if NULL, or hex if
unknown. Error check VOID: CurrentDir cannot fail. Basic tier.

**Read** (-42) -- Reads bytes from a file. Three args: d1 (file handle), d2
(buffer), d3 (requested length). Formatted as `fh=0xADDR,len=N`. Returns
IO_LEN (-1 on error, 0 on EOF, positive for bytes read). Error check NEG1.
Manual tier due to extreme frequency in I/O-heavy programs.

**Write** (-48) -- Writes bytes to a file. Three args: d1 (file handle), d2
(buffer), d3 (length). Formatted as `fh=0xADDR,len=N`. Returns IO_LEN. Error
check NEG1. Manual tier.

**UnLock** (-90) -- Releases a filesystem lock. Arg0 (d1) is the lock BPTR.
**skip_null_arg=d1**: events are suppressed when d1 is NULL (unlocking nothing
is a no-op). The daemon resolves the lock to a path via cache, then removes
the cache entry. Returns VOID. Detail tier.

**Examine** (-102) -- Examines a locked file or directory. Two args: d1
(lock), d2 (FileInfoBlock). Formatted as `lock=0xADDR,fib=0xADDR`. Returns
BOOL_DOS. Detail tier.

**ExNext** (-108) -- Gets the next entry during directory scanning. Two args:
d1 (lock), d2 (FileInfoBlock). Formatted as `lock=0xADDR,fib=0xADDR`. Returns
BOOL_DOS. Verbose tier because directory scans generate a burst of events per
entry.

**Seek** (-66) -- Seeks within a file. Three args: d1 (file handle), d2
(position), d3 (offset mode). The daemon formats d3 as a symbolic name
(OFFSET_BEGINNING, OFFSET_CURRENT, OFFSET_END). Returns IO_LEN (old position,
or -1 on error). Error check NEG1. Detail tier.

**SetProtection** (-186) -- Sets file protection bits. Two args: d1 (filename
string), d2 (protection mask). The daemon formats d2 as an `hsparwed` string
where HSPA bits (7-4) are active-high and RWED bits (3-0) are active-low
(e.g., `----rwed` for default permissions). Returns BOOL_DOS. Basic tier.

**UnLoadSeg** (-156) -- Frees a loaded program's seglist. Arg0 (d1) is the
seglist BPTR. The daemon resolves it to a name via cache, then removes the
cache entry. Returns VOID. Detail tier.

---

## intuition.library (14 functions)

| Function | LVO | Args | Arg Registers | String | Deref | Return | Error Check | Tier |
|----------|-----|------|---------------|--------|-------|--------|-------------|------|
| OpenWindow | -204 | 1 | a0 | -- | NW_TITLE | PTR | NULL | Basic |
| CloseWindow | -72 | 1 | a0 | -- | WIN_TITLE | VOID | VOID | Basic |
| OpenScreen | -198 | 1 | a0 | -- | NS_TITLE | PTR | NULL | Basic |
| CloseScreen | -66 | 1 | a0 | -- | SCR_TITLE | VOID | VOID | Basic |
| ActivateWindow | -450 | 1 | a0 | -- | WIN_TITLE | VOID | VOID | Basic |
| WindowToFront | -312 | 1 | a0 | -- | WIN_TITLE | VOID | VOID | Basic |
| WindowToBack | -306 | 1 | a0 | -- | WIN_TITLE | VOID | VOID | Basic |
| ModifyIDCMP | -150 | 2 | a0, d0 | -- | NONE | VOID | VOID | Detail |
| OpenWorkBench | -210 | 0 | -- | -- | NONE | PTR | ANY | Basic |
| CloseWorkBench | -78 | 0 | -- | -- | NONE | BOOL_DOS | ANY | Basic |
| LockPubScreen | -510 | 1 | a0 | arg0 | NONE | PTR | NULL | Basic |
| OpenWindowTagList | -606 | 2 | a0, a1 | -- | NW_TITLE | PTR | NULL | Basic |
| OpenScreenTagList | -612 | 2 | a0, a1 | -- | NS_TITLE | PTR | NULL | Basic |
| UnlockPubScreen | -516 | 2 | a0, a1 | arg0 | NONE | VOID | VOID | Detail |

### intuition.library Function Details

**OpenWindow** (-204) -- Opens a window. Arg0 (a0) is the NewWindow structure;
DEREF_NW_TITLE reads the Title field at offset 26. Formatted as the window
title string. Returns PTR (window pointer). Basic tier.

**CloseWindow** (-72) -- Closes a window. Arg0 (a0) is the window pointer;
DEREF_WIN_TITLE reads the Title field at offset 32. Returns VOID. Basic tier.

**OpenScreen** (-198) -- Opens a screen. Arg0 (a0) is the NewScreen structure;
DEREF_NS_TITLE reads DefaultTitle at offset 20. Returns PTR. Basic tier.

**CloseScreen** (-66) -- Closes a screen. Arg0 (a0) is the screen pointer;
DEREF_SCR_TITLE reads the Title field at offset 22. Returns VOID. Basic tier.

**ActivateWindow** (-450) -- Makes a window the active input window. Arg0 (a0)
with DEREF_WIN_TITLE. Returns VOID. Basic tier.

**WindowToFront** (-312) -- Brings a window to the front of its screen. Arg0
(a0) with DEREF_WIN_TITLE. Returns VOID. Basic tier.

**WindowToBack** (-306) -- Sends a window to the back of its screen. Arg0 (a0)
with DEREF_WIN_TITLE. Returns VOID. Basic tier.

**ModifyIDCMP** (-150) -- Changes a window's IDCMP flags. Two args: a0
(window pointer), d0 (new IDCMP flags). The daemon formats d0 as symbolic
IDCMP flag names (e.g., `IDCMP_MOUSEBUTTONS|IDCMP_CLOSEWINDOW`). No deref --
the window pointer is displayed as hex. Returns VOID. Detail tier.

**OpenWorkBench** (-210) -- Opens the Workbench screen. No arguments. Returns
PTR. Error check ANY: always shown in `--errors` because the return
convention is non-standard. Basic tier.

**CloseWorkBench** (-78) -- Closes the Workbench screen. No arguments. Returns
BOOL_DOS. Error check ANY. Basic tier.

**LockPubScreen** (-510) -- Locks a public screen by name. Arg0 (a0) is the
screen name string (string_args=0x01). **skip_null_arg=a0**: when a0 is NULL
(meaning "lock the default public screen"), the event is suppressed entirely --
no ring buffer entry is created. The call still proceeds to the original
function. Returns PTR. Basic tier.

**OpenWindowTagList** (-606) -- Opens a window with tag list. Two args: a0
(NewWindow structure, may be NULL), a1 (tag list). DEREF_NW_TITLE extracts the
title from a0. Formatted as the title and tag list address. Returns PTR. Basic
tier.

**OpenScreenTagList** (-612) -- Opens a screen with tag list. Two args: a0
(NewScreen structure, may be NULL), a1 (tag list). DEREF_NS_TITLE. Returns
PTR. Basic tier.

**UnlockPubScreen** (-516) -- Unlocks a public screen. Two args: a0 (screen
name string), a1 (screen pointer). string_args=0x01 captures the name from
a0. **skip_null_arg=a0**: when a0 is NULL, the event is suppressed entirely --
no ring buffer entry is created. The call still proceeds to the original
function. Returns VOID. Detail tier.

---

## bsdsocket.library (15 functions)

All bsdsocket.library functions use ERR_CHECK_NEG1 (error when return
== -1) and RET_IO_LEN (signed decimal display). None use string capture
or dereference types.

| Function | LVO | Args | Arg Registers | String | Deref | Return | Error Check | Tier |
|----------|-----|------|---------------|--------|-------|--------|-------------|------|
| socket | -30 | 3 | d0, d1, d2 | -- | NONE | IO_LEN | NEG1 | Basic |
| bind | -36 | 3 | d0, a0, d1 | -- | NONE | IO_LEN | NEG1 | Basic |
| listen | -42 | 2 | d0, d1 | -- | NONE | IO_LEN | NEG1 | Basic |
| accept | -48 | 3 | d0, a0, a1 | -- | NONE | IO_LEN | NEG1 | Basic |
| connect | -54 | 3 | d0, a0, d1 | -- | NONE | IO_LEN | NEG1 | Basic |
| sendto | -60 | 6 | d0, a0, d1, d2 | -- | NONE | IO_LEN | NEG1 | Detail |
| send | -66 | 4 | d0, a0, d1, d2 | -- | NONE | IO_LEN | NEG1 | Manual |
| recvfrom | -72 | 6 | d0, a0, d1, d2 | -- | NONE | IO_LEN | NEG1 | Detail |
| recv | -78 | 4 | d0, a0, d1, d2 | -- | NONE | IO_LEN | NEG1 | Manual |
| shutdown | -84 | 2 | d0, d1 | -- | NONE | IO_LEN | NEG1 | Basic |
| setsockopt | -90 | 5 | d0, d1, d2, a0 | -- | NONE | IO_LEN | NEG1 | Basic |
| getsockopt | -96 | 5 | d0, d1, d2, a0 | -- | NONE | IO_LEN | NEG1 | Basic |
| IoctlSocket | -114 | 3 | d0, d1, a0 | -- | NONE | IO_LEN | NEG1 | Basic |
| CloseSocket | -120 | 1 | d0 | -- | NONE | IO_LEN | NEG1 | Basic |
| WaitSelect | -126 | 6 | d0, d1, a0, a1 | -- | NONE | IO_LEN | NEG1 | Manual |

### bsdsocket.library Function Details

**Note on argument capture:** Functions with more than 4 arguments (sendto,
recvfrom, setsockopt, getsockopt, WaitSelect) have their full arg_count
recorded in the event but only the first 4 register values are captured in
`args[0..3]`. The Arg Registers column above shows only the captured registers.

**socket** (-30) -- Creates a socket. Three args: d0 (domain), d1 (type), d2
(protocol). The daemon formats d0 as symbolic address family (AF_INET) and d1
as socket type (SOCK_STREAM, SOCK_DGRAM). Returns the file descriptor as
signed decimal (or -1 on error). Basic tier.

**bind** (-36) -- Binds a socket to an address. Three args: d0 (fd), a0
(sockaddr pointer), d1 (address length). Formatted as `fd=N,addr=0xADDR,len=N`.
Basic tier.

**listen** (-42) -- Marks a socket as listening. Two args: d0 (fd), d1
(backlog). Formatted as `fd=N,backlog=N`. Basic tier.

**accept** (-48) -- Accepts an incoming connection. Three args: d0 (fd), a0
(address buffer), a1 (address length pointer). Formatted as `fd=N,addr=0xADDR`.
Returns the new socket fd. Basic tier.

**connect** (-54) -- Connects to a remote address. Three args: d0 (fd), a0
(sockaddr pointer), d1 (address length). Formatted as `fd=N,addr=0xADDR,len=N`.
Basic tier.

**sendto** (-60) -- Sends data to a specific address. 6 args total, first 4
captured: d0 (fd), a0 (buffer), d1 (length), d2 (flags). Formatted as
`fd=N,len=N,flags=0xN`. Detail tier.

**send** (-66) -- Sends data on a connected socket. Four args: d0 (fd), a0
(buffer), d1 (length), d2 (flags). Formatted as `fd=N,len=N,flags=0xN`. Manual
tier due to high frequency.

**recvfrom** (-72) -- Receives data with source address. 6 args total, first 4
captured: d0 (fd), a0 (buffer), d1 (length), d2 (flags). Formatted as
`fd=N,len=N,flags=0xN`. Detail tier.

**recv** (-78) -- Receives data from a connected socket. Four args: d0 (fd),
a0 (buffer), d1 (length), d2 (flags). Formatted as `fd=N,len=N,flags=0xN`.
Manual tier due to high frequency.

**shutdown** (-84) -- Shuts down part of a connection. Two args: d0 (fd), d1
(how). The daemon formats d1 as symbolic name (SHUT_RD, SHUT_WR, SHUT_RDWR).
Basic tier.

**setsockopt** (-90) -- Sets a socket option. 5 args total, first 4 captured:
d0 (fd), d1 (level), d2 (option name), a0 (option value pointer). Formatted as
`fd=N,level=N,opt=N`. Basic tier.

**getsockopt** (-96) -- Gets a socket option. 5 args total, first 4 captured:
d0 (fd), d1 (level), d2 (option name), a0 (option value pointer). Formatted as
`fd=N,level=N,opt=N`. Basic tier.

**IoctlSocket** (-114) -- Socket I/O control. Three args: d0 (fd), d1
(request code), a0 (argument pointer). Formatted as `fd=N,req=0xN`. Basic tier.

**CloseSocket** (-120) -- Closes a socket. Arg0 (d0) is the fd. Formatted as
`fd=N`. Basic tier.

**WaitSelect** (-126) -- Waits for activity on multiple sockets with signal
support. 6 args total, first 4 captured: d0 (nfds), d1 (signal mask), a0
(read fd set), a1 (write fd set). **Custom arg_regs order**: the capture
registers are `{d0, d1, a0, a1}` rather than the standard BSD parameter order,
to prioritize capturing nfds and the Amiga-specific signal mask (the two most
diagnostically useful values). The daemon formats the signal mask as symbolic
signal names. Manual tier due to high frequency in event loops.

---

## graphics.library (2 functions)

| Function | LVO | Args | Arg Registers | String | Deref | Return | Error Check | Tier |
|----------|-----|------|---------------|--------|-------|--------|-------------|------|
| OpenFont | -72 | 1 | a0 | -- | TEXTATTR | PTR | NULL | Verbose |
| CloseFont | -78 | 1 | a1 | -- | NONE | VOID | VOID | Verbose |

### graphics.library Function Details

**OpenFont** (-72) -- Opens a font matching a TextAttr specification. Arg0 (a0)
is the TextAttr pointer. DEREF_TEXTATTR reads `ta_Name` (offset 0) as a string
into `string_data`, and copies `ta_YSize` (font size, offset 4) into `args[1]`.
Formatted as `"fontname.font",SIZE` (e.g., `"topaz.font",8`). Returns PTR.
Verbose tier because programs that render text generate many OpenFont calls.

**CloseFont** (-78) -- Closes a font. Arg0 (a1) is the TextFont pointer.
No deref or string capture; formatted as `font=0xADDR`. Returns VOID. Verbose
tier.

---

## icon.library (5 functions)

| Function | LVO | Args | Arg Registers | String | Deref | Return | Error Check | Tier |
|----------|-----|------|---------------|--------|-------|--------|-------------|------|
| GetDiskObject | -78 | 1 | a0 | arg0 | NONE | PTR | NULL | Basic |
| PutDiskObject | -84 | 2 | a0, a1 | arg0 | NONE | BOOL_DOS | NULL | Basic |
| FreeDiskObject | -90 | 1 | a0 | -- | NONE | VOID | VOID | Basic |
| FindToolType | -96 | 2 | a0, a1 | arg1 | NONE | PTR | NULL | Basic |
| MatchToolValue | -102 | 2 | a0, a1 | -- | NONE | BOOL_DOS | NONE | Basic |

### icon.library Function Details

**GetDiskObject** (-78) -- Reads a .info file. Arg0 (a0) is the filename
string (without .info extension). Returns PTR (DiskObject pointer, or NULL on
failure). The daemon caches the DiskObject-to-name mapping for display by
FreeDiskObject. Basic tier.

**PutDiskObject** (-84) -- Writes a .info file. Two args: a0 (filename string),
a1 (DiskObject pointer). Formatted as `"filename",obj=0xADDR`. Returns
BOOL_DOS. Basic tier.

**FreeDiskObject** (-90) -- Frees a DiskObject. Arg0 (a0) is the DiskObject
pointer. No string capture, but the daemon resolves the pointer to a filename
via cache (from a prior GetDiskObject), then removes the cache entry. Returns
VOID. Basic tier.

**FindToolType** (-96) -- Searches a tool type array for a named type. Two
args: a0 (tool type array), a1 (type name). **string_args=0x02**: arg1 (the
type name, from a1) is captured as the string, not arg0. Formatted as the
quoted type name. Returns PTR (pointer to the value string, or NULL if not
found). Basic tier.

**MatchToolValue** (-102) -- Checks whether a tool type string contains a
specific value. Two args: a0 (type string pointer), a1 (value pointer).
No string capture (both args are pointers displayed as hex). Returns BOOL_DOS.
Error check NONE: FALSE is a normal expected result (value not present), not an
error condition. Basic tier.

---

## workbench.library (6 functions)

| Function | LVO | Args | Arg Registers | String | Deref | Return | Error Check | Tier |
|----------|-----|------|---------------|--------|-------|--------|-------------|------|
| AddAppIconA | -60 | 7 | d0, d1, a0, a1 | arg2 | NONE | PTR | NULL | Basic |
| RemoveAppIcon | -66 | 1 | a0 | -- | NONE | BOOL_DOS | NULL | Basic |
| AddAppWindowA | -48 | 5 | d0, d1, a0, a1 | -- | NONE | PTR | NULL | Basic |
| RemoveAppWindow | -54 | 1 | a0 | -- | NONE | BOOL_DOS | NULL | Basic |
| AddAppMenuItemA | -72 | 5 | d0, d1, a0, a1 | arg2 | NONE | PTR | NULL | Basic |
| RemoveAppMenuItem | -78 | 1 | a0 | -- | NONE | BOOL_DOS | NULL | Basic |

### workbench.library Function Details

**Note on argument capture:** AddAppIconA (7 args), AddAppWindowA (5 args),
and AddAppMenuItemA (5 args) all have more than 4 arguments. Only the first 4
register values are captured. The remaining arguments (lock, diskobj, taglist)
are not recorded in the event.

**AddAppIconA** (-60) -- Registers an AppIcon on the Workbench. 7 total args;
first 4 captured: d0 (id), d1 (userdata), a0 (text label), a1 (message port).
**string_args=0x04**: arg2 in capture order (which is a0, the text label) is
captured as a string. Formatted as `id=N,"label"`. Returns PTR (AppIcon handle).
Basic tier.

**RemoveAppIcon** (-66) -- Removes an AppIcon. Arg0 (a0) is the AppIcon
handle. Formatted as `icon=0xADDR`. Returns BOOL_DOS (TRUE=1 on success; note
that workbench.library returns standard TRUE (1), not DOSTRUE (-1), but
RET_BOOL_DOS handles both correctly by treating any non-zero value as success).
Basic tier.

**AddAppWindowA** (-48) -- Registers an AppWindow for drag-and-drop. 5 total
args; first 4 captured: d0 (id), d1 (userdata), a0 (window), a1 (message
port). No string capture. Formatted as `id=N,win=0xADDR`. Returns PTR. Basic
tier.

**RemoveAppWindow** (-54) -- Removes an AppWindow. Arg0 (a0) is the AppWindow
handle. Formatted as `win=0xADDR`. Returns BOOL_DOS. Basic tier.

**AddAppMenuItemA** (-72) -- Registers a menu item on the Workbench Tools
menu. 5 total args; first 4 captured: d0 (id), d1 (userdata), a0 (menu text),
a1 (message port). **string_args=0x04**: arg2 in capture order (a0, the text)
is captured as a string. Formatted as `id=N,"label"`. Returns PTR. Basic tier.

**RemoveAppMenuItem** (-78) -- Removes an AppMenuItem. Arg0 (a0) is the handle.
Formatted as `item=0xADDR`. Returns BOOL_DOS. Basic tier.

---

## Summary Statistics

| Library | Functions | Basic | Detail | Verbose | Manual |
|---------|-----------|-------|--------|---------|--------|
| exec.library | 31 | 5 | 5 | 0 | 21 |
| dos.library | 26 | 19 | 4 | 1 | 2 |
| intuition.library | 14 | 12 | 2 | 0 | 0 |
| bsdsocket.library | 15 | 10 | 2 | 0 | 3 |
| graphics.library | 2 | 0 | 0 | 2 | 0 |
| icon.library | 5 | 5 | 0 | 0 | 0 |
| workbench.library | 6 | 6 | 0 | 0 | 0 |
| **Total** | **99** | **57** | **13** | **3** | **26** |

---

## Cross-References

- [event-format.md](event-format.md) -- Binary layout of the 128-byte event
  structure, field offsets for args[], string_data, retval, ioerr, task_name.
- [reading-output.md](reading-output.md) -- How to interpret formatted output
  lines, status indicators, and IoErr annotations.
- [output-tiers.md](output-tiers.md) -- Tier system design, cumulative tier
  behavior, Manual tier semantics.
- [stub-mechanism.md](stub-mechanism.md) -- How stubs capture arguments,
  perform dereferences, and write events to the ring buffer.
- [filtering.md](filtering.md) -- Client-side and server-side filtering,
  including `--errors` mode that uses the error check types documented here.
