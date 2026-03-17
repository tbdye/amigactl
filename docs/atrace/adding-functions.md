# Adding New Traced Functions

This guide walks through every file modification required to add a new
traced function to an existing library, or to add an entirely new library
to atrace. Each step must be completed in order, and the ordering
constraints between files are critical to correct operation.

Adding a single function requires changes in up to five files:

1. `atrace/funcs.c` -- function metadata (Amiga-side)
2. `daemon/trace.c` `func_table[]` -- name lookup and display metadata (Amiga-side)
3. `daemon/trace.c` `format_args()` -- custom argument formatting (optional, Amiga-side)
4. `client/amigactl/trace_tiers.py` -- tier assignment (client-side)
5. `atrace/main.c` and `daemon/trace.c` `noise_func_names[]` -- tier disable tables (Amiga-side, only for non-Basic functions)

If the function belongs to a library not yet traced, additional setup is
required (see [Adding a New Library](#adding-a-new-library) below).


## Finding LVO Offsets and Register Assignments

Every AmigaOS library function has a Library Vector Offset (LVO) -- a
negative byte offset into the library's jump table. To add a function,
you need its LVO and the registers it uses for arguments and return
values.

**Sources for LVO information:**

- **Amiga Developer CD fd files.** The `.fd` files (function definition
  files) in the `NDK/Include/fd/` directory list every function with its
  LVO and register assignments. For example, `exec_lib.fd` contains
  entries like `OpenLibrary(libName,version)(a1,d0)` with bias values.

- **Amiga header files.** The `<clib/*_protos.h>` and `<pragmas/*_pragmas.h>`
  headers encode register assignments in their pragma definitions.

- **Online references.** The Amiga Developer Docs at
  `amigadev.elowar.com` provide searchable function reference with LVO
  offsets, register assignments, and return value conventions.

LVO offsets are always negative multiples of 6 (each jump table entry is
a 6-byte JMP instruction). For example, `OpenLibrary` is at LVO -552,
meaning the JMP instruction at `libbase - 552` dispatches to the
implementation.

Register assignments specify which 68000 registers carry each argument
when the function is called through the library vector. AmigaOS uses a
register-based calling convention where arguments are passed in specific
d0-d7 and a0-a6 registers (never on the stack for library functions).
The library base is always in a6.


## Step 1 -- Define the Function (funcs.c)

Add a `struct func_info` entry to the appropriate library's function
array in `atrace/funcs.c`. The entry goes at the end of the array for
its library.

### struct func_info Fields

```c
struct func_info {
    const char *name;          /* Function name (must match AmigaOS name exactly) */
    WORD lvo_offset;           /* Negative LVO offset (e.g., -552 for OpenLibrary) */
    UBYTE arg_count;           /* Total number of arguments */
    UBYTE arg_regs[8];         /* Register for each captured argument (up to 8) */
    UBYTE ret_reg;             /* Register containing the return value */
    UBYTE string_args;         /* Bitmask: which captured args are C string pointers */
    UBYTE name_deref_type;     /* Indirect name capture strategy (DEREF_* constant) */
    UBYTE skip_null_arg;       /* Register to check for NULL-argument filtering */
    UBYTE padding;             /* Alignment padding (set to 0) */
};
```

### Field-by-field explanation

**name** -- The exact AmigaOS function name as a string literal. This
name is used for enable/disable commands, STATUS display, and tier
lookups. Case matters for display but lookups are case-insensitive.

**lvo_offset** -- The negative Library Vector Offset. Always a negative
number. Obtained from `.fd` files or AmigaOS reference documentation.

**arg_count** -- The total number of arguments the function takes. This
is the real argument count, not the number captured. The stub generator
uses `min(arg_count, 4)` to determine how many arguments to copy into
the event record (the event struct has space for 4 argument slots). For
functions with more than 4 arguments (e.g., `sendto` with 6), set
`arg_count` to the real count but only populate the first 4 slots of
`arg_regs[]` with the most diagnostically useful registers.

**arg_regs[8]** -- Specifies which 68000 register holds each argument, using the `REG_*` constants from `atrace.h`:

| Constant | Value | Register |
|----------|-------|----------|
| `REG_D0` | 0 | d0 |
| `REG_D1` | 1 | d1 |
| `REG_D2` | 2 | d2 |
| `REG_D3` | 3 | d3 |
| `REG_D4` | 4 | d4 |
| `REG_D5` | 5 | d5 |
| `REG_D6` | 6 | d6 |
| `REG_D7` | 7 | d7 |
| `REG_A0` | 8 | a0 |
| `REG_A1` | 9 | a1 |
| `REG_A2` | 10 | a2 |
| `REG_A3` | 11 | a3 |
| `REG_A4` | 12 | a4 |
| `REG_A5` | 13 | a5 |
| `REG_A6` | 14 | a6 |

The order of entries in `arg_regs[]` determines the capture order, not
the register numbers themselves. Slot 0 maps to `ev->args[0]`, slot 1 to
`ev->args[1]`, and so on. Unused slots (beyond `min(arg_count, 4)`)
should be 0.

For functions with more than 4 arguments, choose which 4 registers to
capture based on diagnostic value. For example, `WaitSelect` has 6
arguments but captures `{REG_D0, REG_D1, REG_A0, REG_A1}` to get nfds
and the signal mask (d1) rather than strictly following the positional
argument order.

Note: a5 is not part of the standard MOVEM save frame in the stub. Using
`REG_A5` as an argument register requires special handling in the stub
generator. Most functions do not use a5 for arguments.

**ret_reg** -- The register containing the return value. Almost always
`REG_D0`. Set to `REG_D0` even for void functions (the field is still
required; the daemon's `result_type` field controls how the value is
displayed).

**string_args** -- A bitmask indicating which captured arguments are
pointers to null-terminated C strings. Bit N corresponds to `arg_regs[N]`
(not the register number, but the capture slot position). When a bit is
set, the stub copies up to 63 bytes from that string into the event's
64-byte `string_data` field.

Examples:
- `0x01` (bit 0) -- arg 0 is a string. Used by `FindPort(a1=name)`,
  `Open(d1=name)`, `OpenLibrary(a1=libName)`.
- `0x03` (bits 0 and 1) -- args 0 and 1 are both strings. Used by
  `Rename(d1=oldName, d2=newName)` and `MakeLink(d1=name, d2=dest)`.
- `0x02` (bit 1) -- arg 1 is a string. Used by `FindToolType(a0=array,
  a1=typeName)` where only the second argument is a string.
- `0x04` (bit 2) -- arg 2 is a string. Used by
  `AddAppIconA(d0=id, d1=userdata, a0=text, ...)` where the third
  captured argument is the string.

Only one string can be captured per event (the 64-byte `string_data`
field). When multiple bits are set, the stub captures the first string
argument it encounters (lowest bit set). The remaining string arguments
are recorded as raw pointer values in their `args[]` slots.

**name_deref_type** -- Controls indirect name capture, where the stub
follows a pointer chain from an argument to extract a meaningful string
(like a port name, device name, or window title). This is distinct from
`string_args`, which handles direct C string arguments.

| Constant | Value | Description |
|----------|-------|-------------|
| `DEREF_NONE` | 0 | No indirect capture |
| `DEREF_LN_NAME` | 1 | Follow `arg0->ln_Name` (offset 10). Used for ports, semaphores, libraries. |
| `DEREF_IOREQUEST` | 2 | Two-level: `IORequest->io_Device->ln_Name` plus `io_Command`. Used for DoIO, SendIO, etc. |
| `DEREF_TEXTATTR` | 3 | `TextAttr->ta_Name` (offset 0) plus `ta_YSize`. Used for OpenFont. |
| `DEREF_LOCK_VOLUME` | 4 | Lock BPTR chain: `fl_Volume -> dol_Name` BSTR. Used for CurrentDir. |
| `DEREF_NW_TITLE` | 5 | `NewWindow.Title` at byte offset 26. Used for OpenWindow. |
| `DEREF_WIN_TITLE` | 6 | `Window.Title` at byte offset 32. Used for CloseWindow, ActivateWindow, etc. |
| `DEREF_NS_TITLE` | 7 | `NewScreen.DefaultTitle` at byte offset 20. Used for OpenScreen. |
| `DEREF_SCR_TITLE` | 8 | `Screen.Title` at byte offset 22. Used for CloseScreen. |
| `DEREF_SOCKADDR` | 9 | `sockaddr_in`: 8 bytes into `string_data[0..7]` using `arg_regs[1]`. Used for bind, connect. |
| `DEREF_SOCKADDR_3` | 10 | `sockaddr_in`: 8 bytes into `string_data[0..7]` using `arg_regs[3]`. Used for sendto. |

When `name_deref_type` is non-zero, the stub generates additional code
to dereference the pointer chain and copy the resulting string into
`string_data`. This happens regardless of the `string_args` bitmask.
See [stub-mechanism.md](stub-mechanism.md) for details on how each
dereference type generates machine code.

**skip_null_arg** -- Enables the NULL-argument filter optimization. When
set to a non-zero register constant (e.g., `REG_A1`, `REG_D1`), the stub
checks whether that register is NULL before recording the event. If the
register is NULL, the stub skips the event entirely (transparent
pass-through to the original function). This suppresses high-frequency
calls with NULL arguments that are normal behavior, not diagnostic events.

Examples:
- `FindTask(a1=name)`: `skip_null_arg = REG_A1`. `FindTask(NULL)` means
  "get my own task pointer" -- called constantly by running programs but
  rarely interesting for debugging. Only `FindTask("named_task")` calls
  are recorded.
- `UnLock(d1=lock)`: `skip_null_arg = REG_D1`. `UnLock(NULL)` is a
  documented no-op and not worth recording.
- `LockPubScreen(a0=name)`: `skip_null_arg = REG_A0`. NULL means "lock
  the default public screen" -- routine and not diagnostic.

Set to 0 to disable the filter (record all calls regardless of argument
values).

### Example: Adding a Simple Function

To trace `RemPort` from exec.library (LVO -360, one argument in a1,
returns void):

```c
/* In exec_funcs[] array, after the last entry: */
/* N: RemPort(a1=port) -> void */
{
    "RemPort", -360, 1,
    { REG_A1, 0, 0, 0, 0, 0, 0, 0 },
    REG_D0, 0x00, DEREF_LN_NAME, 0, 0
}
```

This captures the port pointer in a1, uses `DEREF_LN_NAME` to extract
the port name from `port->mp_Node.ln_Name`, has no string arguments
(`0x00`), and no NULL-argument filter (`0`).

After adding the entry, update the `func_count` in the corresponding
`lib_info` entry in the `atrace_libs[]` table at the bottom of funcs.c:

```c
{
    "exec.library",
    exec_funcs,
    32,            /* was 31, now 32 */
    LIB_EXEC,
    0
},
```


## Step 2 -- Add Daemon Lookup Entry (trace.c func_table[])

Add a `struct trace_func_entry` to the `func_table[]` array in
`daemon/trace.c`.

### CRITICAL: Ordering Constraint

**The order of entries in `func_table[]` MUST exactly match the
installation order from `funcs.c`.**

The daemon identifies functions by their index into this table. The
loader installs patches by iterating through `atrace_libs[]` in order,
processing each library's function array sequentially. The global patch
index is assigned as a running count: all of exec's functions first (indices
0-30), then all of dos's functions (indices 31-56), then intuition (57-70),
bsdsocket (71-85), graphics (86-87), icon (88-92), and workbench (93-98).

If `func_table[]` entries are in a different order than `funcs.c`,
enable/disable-by-name operations (which use `find_patch_index_by_name`
to map a function name to its global patch index) will target the wrong
patches, and TRACE STATUS display will show incorrect patch-to-name
mappings. Event display is unaffected because `lookup_func()` matches
events by `lib_id + lvo_offset`, not by index.

When adding a function to the end of a library's section in `funcs.c`,
add the corresponding `func_table[]` entry at the end of that library's
section in `trace.c`.

### struct trace_func_entry Fields

```c
struct trace_func_entry {
    const char *lib_name;    /* Short library name (e.g., "exec", "dos") */
    const char *func_name;   /* Function name (must match funcs.c exactly) */
    UBYTE lib_id;            /* LIB_* constant (must match funcs.c) */
    WORD  lvo_offset;        /* LVO offset (must match funcs.c) */
    UBYTE error_check;       /* ERR_CHECK_* constant */
    UBYTE has_string;        /* 1 if stub captures string_data, 0 if not */
    UBYTE result_type;       /* RET_* constant for return value display */
};
```

**lib_name** -- Short library name without the `.library` suffix: `"exec"`,
`"dos"`, `"intuition"`, `"bsdsocket"`, `"graphics"`, `"icon"`, `"workbench"`.

**func_name** -- Must exactly match the `name` field in `funcs.c`.

**lib_id** -- Must exactly match the `LIB_*` constant used in `funcs.c`.

**lvo_offset** -- Must exactly match the `lvo_offset` in `funcs.c`.
This is a per-entry field accuracy requirement, separate from the table
ordering constraint above: the daemon's `lookup_func()` matches events
to table entries by `lib_id + lvo_offset`, so if either field in a given
entry is wrong, events from that function will go unrecognized.

**error_check** -- Determines what return values constitute an error for
the `ERRORS` filter:

| Constant | Value | Semantics |
|----------|-------|-----------|
| `ERR_CHECK_NULL` | 0 | Error when retval == 0 (most common: NULL pointer = failure) |
| `ERR_CHECK_NZERO` | 1 | Error when retval != 0 (e.g., OpenDevice: 0 = success) |
| `ERR_CHECK_VOID` | 2 | Void function -- never shown in ERRORS mode |
| `ERR_CHECK_ANY` | 3 | No clear error convention -- always show |
| `ERR_CHECK_NONE` | 4 | Never an error (e.g., GetMsg returning NULL is normal, not an error) |
| `ERR_CHECK_RC` | 5 | Return code: error when rc != 0 |
| `ERR_CHECK_NEGATIVE` | 6 | Error when (LONG)retval < 0 (e.g., GetVar: -1 = fail) |
| `ERR_CHECK_NEG1` | 7 | Error when retval == 0xFFFFFFFF (-1 unsigned, used by bsdsocket) |

Choose the error check that matches the function's documented error
return convention.

**has_string** -- Set to 1 if the stub captures any string data (either
via `string_args` bitmask or `name_deref_type`). Set to 0 if the function
has no string capture at all. This flag controls whether the daemon
attempts to read the `string_data` field from the event.

**result_type** -- Controls how the return value is formatted for display:

| Constant | Value | Format |
|----------|-------|--------|
| `RET_PTR` | 0 | Pointer: NULL = "NULL", non-zero = hex address |
| `RET_BOOL_DOS` | 1 | DOS boolean: 0 = "FAIL", non-zero = "OK" |
| `RET_NZERO_ERR` | 2 | 0 = "OK", non-zero = error code (signed decimal) |
| `RET_VOID` | 3 | Always shows "(void)" |
| `RET_MSG_PTR` | 4 | Message pointer: NULL = "empty", non-zero = hex |
| `RET_RC` | 5 | Return code: signed decimal, 0 = success |
| `RET_LOCK` | 6 | BPTR lock: NULL = fail, non-zero = hex |
| `RET_LEN` | 7 | Byte count: -1 = fail, >= 0 = decimal count |
| `RET_OLD_LOCK` | 8 | Old lock from CurrentDir: NULL is OK |
| `RET_PTR_OPAQUE` | 9 | Opaque pointer: non-NULL = OK, NULL = fail |
| `RET_EXECUTE` | 10 | Execute(): raw value, neutral status |
| `RET_IO_LEN` | 11 | I/O count: -1 = error, 0 = EOF, > 0 = bytes |

### Example

Continuing the `RemPort` example:

```c
/* In func_table[], at the end of the exec.library section: */
{ "exec", "RemPort", LIB_EXEC, -360, ERR_CHECK_VOID, 1, RET_VOID },
```

`RemPort` is a void function (ERR_CHECK_VOID, RET_VOID) and the stub
captures the port name via DEREF_LN_NAME (has_string = 1).


## Step 3 -- Add Custom Argument Formatting (trace.c format_args, optional)

The `format_args()` function in `daemon/trace.c` contains per-function
formatting logic organized as a two-level dispatch: the outer level uses
an if/else-if chain on `lib_id`, and each block contains an inner switch
on `lvo_offset`. If the new function needs human-readable argument
display beyond raw hex values, add a case to the inner switch of the
appropriate library's block. When adding a function for a library that
has no existing `format_args` cases, you must add a new
`if (fe->lib_id == LIB_*)` block with its own inner `lvo_offset` switch.

If you do not add a custom format_args case, the daemon falls back to
displaying raw hex values for each argument (`0x<value>,0x<value>,...`).
This is functional but less readable for debugging. Functions with string
capture (via `string_args` or `name_deref_type`) benefit most from
custom formatting that displays the captured string inline.

### Example

For `RemPort`, you could add:

```c
/* In the LIB_EXEC block of format_args(): */
case -360:  /* RemPort(port) */
    if (ev->string_data[0])
        p += snprintf(p, remaining, "\"%s%s\"",
                      ev->string_data, trunc);
    else
        p += snprintf(p, remaining, "port=0x%lx",
                      (unsigned long)ev->args[0]);
    return;
```

This shows the port name as a quoted string when available, falling back
to the raw pointer value when `DEREF_LN_NAME` did not resolve a name.
This pattern (quoted string with hex fallback) is used by many existing
functions -- see `GetMsg`, `ObtainSemaphore`, and `AddPort` for
similar examples.


## Step 4 -- Assign a Tier (trace_tiers.py)

Every traced function must belong to exactly one tier in
`client/amigactl/trace_tiers.py`. Add the function name to the
appropriate frozenset:

| Tier | Set | When to use |
|------|-----|-------------|
| Basic | `TIER_BASIC` | Single event provides immediate diagnostic value. Enabled by default. |
| Detail | `TIER_DETAIL` | Deeper debugging, too noisy for casual use. Disabled by default. |
| Verbose | `TIER_VERBOSE` | High-volume burst events tied to user actions. Disabled by default. |
| Manual | `TIER_MANUAL` | Extreme event rate, only useful with task filtering. Never auto-enabled. |

The module-level assertions at the bottom of `trace_tiers.py` verify
that:
- The total function count across all tiers equals 99 (update this
  assertion when adding functions).
- No function appears in more than one tier.

After adding the function name, update the assertion count:

```python
assert len(_ALL_FUNCTIONS) == 100, \    # was 99
    "Tier sets contain {} functions, expected 100".format(
        len(_ALL_FUNCTIONS))
```


## Step 5 -- Update Tier Disable Tables (main.c and trace.c)

If the function is **Basic tier**, skip this step. Basic-tier functions
are enabled by default at install time and do not appear in any disable
table.

If the function is **Detail, Verbose, or Manual tier**, it must be added
to two tables:

### 5a. Loader tier table (main.c)

Add the function name to the appropriate NULL-terminated array in
`atrace/main.c`:

- `tier_detail_funcs[]` for Detail tier
- `tier_verbose_funcs[]` for Verbose tier
- `tier_manual_funcs[]` for Manual tier

These tables control which functions the loader auto-disables at install
time. The loader iterates all three tables and disables every function
found, so only Basic-tier functions remain enabled after a fresh install.

### 5b. Daemon noise table (trace.c)

Add the function name to the `noise_func_names[]` array in
`daemon/trace.c`. This array must contain the union of all non-Basic
functions (Detail + Verbose + Manual). It is used by the daemon for
TRACE STATUS reporting (`noise_disabled` count) and for validating
consistency with the loader's patch table at startup.

The daemon validates `noise_func_names[]` at startup by looking up each
name in the loaded patch table. Any name that does not match produces a
warning: `WARNING: noise function '<name>' not found in patch table`.
This catches typos and mismatches between the daemon and loader.

### Keeping the tables in sync

Three separate tables must agree on tier membership:

1. `trace_tiers.py` -- the authoritative source of truth
2. `main.c` tier arrays -- must mirror the non-Basic tiers from trace_tiers.py
3. `trace.c` `noise_func_names[]` -- must be the union of all non-Basic tiers

If these fall out of sync, symptoms include:
- Functions auto-disabled that should be enabled (or vice versa)
- Incorrect `noise_disabled` counts in TRACE STATUS
- Startup warnings from the daemon's name validation


## Step 6 -- Add Test Coverage (atrace_test.c)

Add a test block to `testapp/atrace_test.c` that calls the new function
with known, distinctive inputs. The test app is cross-compiled for 68k
and run on the Amiga via `amigactl trace run -- atrace_test`. The test
framework (`test_trace_app.py`) validates that every traced function
appears in the captured output with correct argument formatting.

Follow the existing pattern:
- Call the function with identifiable values (named strings, known sizes,
  etc.).
- Add a `Delay(1)` between test blocks to separate events in the trace
  stream.
- Use `RAM:` paths for any file operations to avoid side effects.
- Clean up any resources created during the test.


## Adding a New Library

Adding a library that atrace does not yet trace requires additional
setup beyond the per-function steps above.

### 1. Define a new LIB_* constant (atrace.h)

Add a new library ID constant in `atrace/atrace.h`, using the next
available integer:

```c
#define LIB_WORKBENCH  6   /* existing */
#define LIB_NEWLIB     7   /* new */
```

Library IDs are sequential integers starting from 0, used as indices
into lookup tables.

### 2. Create the function array (funcs.c)

Add a new `static struct func_info` array for the library's functions:

```c
/* newlib.library functions (N) */
static struct func_info newlib_funcs[] = {
    /* 0: SomeFunc(d0=arg) -> d0=result */
    {
        "SomeFunc", -30, 1,
        { REG_D0, 0, 0, 0, 0, 0, 0, 0 },
        REG_D0, 0x00, DEREF_NONE, 0, 0
    },
    /* ... additional functions ... */
};
```

### 3. Add a lib_info entry (funcs.c)

Append the new library to the `atrace_libs[]` table and increment
`atrace_lib_count`:

```c
struct lib_info atrace_libs[] = {
    /* ... existing libraries ... */
    {
        "newlib.library",
        newlib_funcs,
        1,              /* func_count */
        LIB_NEWLIB,     /* lib_id = 7 */
        0               /* padding */
    }
};

int atrace_lib_count = 8;   /* was 7 */
```

The order of entries in `atrace_libs[]` determines the global patch
index assignment. New libraries are appended at the end, so existing
indices are unaffected.

### 4. Add func_table entries (trace.c)

Append `trace_func_entry` entries for all the new library's functions at
the end of `func_table[]` in `daemon/trace.c`. The entries must appear
in the same order as the `funcs.c` array.

### 5. Update lib_short_names (main.c)

Add the short library name to the `lib_short_names[]` array in
`atrace/main.c`. This array is indexed by `lib_id`, so the new entry
must be at the position corresponding to the new `LIB_*` constant:

```c
static const char *lib_short_names[] = {
    "exec", "dos", "intuition", "bsdsocket", "graphics",
    "icon", "workbench", "newlib"   /* lib_id 7 */
};
```

### 6. Add tier assignments and noise entries

Follow Steps 4-5 from the per-function guide above for each function in
the new library. Every function must appear in exactly one tier in
`trace_tiers.py`, and non-Basic functions must appear in the loader tier
tables and `noise_func_names[]`.

### 7. Library opening behavior

The loader opens each library via `OpenLibrary()` during installation.
If the library is not present on the system, the loader prints a warning
and skips all patches for that library. The library is intentionally
never closed -- the patches point into the library's jump table and must
remain valid for the lifetime of the system.


## Validation Checklist

After making all changes, verify:

- [ ] **Total function count** -- The assertion in `trace_tiers.py` must
  pass with the updated count (99 + number of new functions).

- [ ] **func_table[] ordering** -- Walk through `funcs.c` library by
  library, function by function, and verify each entry in `func_table[]`
  appears at the corresponding position. A single misalignment shifts
  every subsequent function's name.

- [ ] **noise_func_names consistency** -- Every function in
  `tier_detail_funcs[]`, `tier_verbose_funcs[]`, and
  `tier_manual_funcs[]` in `main.c` must also appear in
  `noise_func_names[]` in `trace.c`. The daemon validates this at
  startup and logs warnings for any mismatches.

- [ ] **Tier table consistency** -- The non-Basic frozensets in
  `trace_tiers.py` must match the corresponding arrays in `main.c`.
  There is no automated cross-check between these files -- manual
  verification is required.

- [ ] **func_count values** -- The `func_count` field in each
  `atrace_libs[]` entry must equal the actual number of entries in the
  corresponding function array.

- [ ] **Build clean** -- `make` must complete with zero warnings for both
  `atrace_loader` and `amigactld`.

- [ ] **Runtime test** -- Run `atrace_loader STATUS` to verify the new
  patch appears in the patch listing. Run `atrace_test` under
  `amigactl trace run` and verify the new function's events appear with
  correct argument formatting and return value display.


## Cross-References

- [stub-mechanism.md](stub-mechanism.md) -- How the stub generator
  translates `func_info` metadata into 68k machine code, including
  argument capture, string copy, and indirect dereference sequences.
- [traced-functions.md](traced-functions.md) -- Complete reference of all
  currently traced functions with their LVOs, argument signatures, tier
  assignments, and error check types.
