"""ANSI terminal color support for amigactl shell output."""

import os
import sys


def _supports_color():
    """Detect whether the terminal supports ANSI color."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("AMIGACTL_COLOR", "").lower() == "never":
        return False
    if os.environ.get("AMIGACTL_COLOR", "").lower() == "always":
        return True
    if not hasattr(sys.stdout, "isatty"):
        return False
    if not sys.stdout.isatty():
        return False
    # Windows-specific VT processing check
    if sys.platform == "win32":
        # Windows Terminal natively supports ANSI
        if os.environ.get("WT_SESSION"):
            return True
        # Try to enable VT processing via ctypes
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
            mode = ctypes.c_ulong()
            kernel32.GetConsoleMode(handle, ctypes.byref(mode))
            # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
            if mode.value & 0x0004:
                return True
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
            return True
        except Exception:
            return False
    return True


# ANSI escape sequences
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
CYAN = "\033[36m"


class ColorWriter:
    """Write colorized text, falling back to plain text if unsupported.

    Usage:
        cw = ColorWriter()
        cw.error("Something failed")     # red
        cw.success("Done")               # green
        cw.directory("Work:")             # blue
        cw.key("size")                    # cyan
        cw.bold("HEADER")                # bold
        cw.write("plain text")           # default color
    """

    def __init__(self, force_color=None):
        if force_color is not None:
            self.enabled = force_color
        else:
            self.enabled = _supports_color()

    def _wrap(self, code, text):
        if self.enabled:
            return "{}{}{}".format(code, text, RESET)
        return text

    def error(self, text):
        return self._wrap(RED, text)

    def success(self, text):
        return self._wrap(GREEN, text)

    def directory(self, text):
        return self._wrap(BLUE, text)

    def key(self, text):
        return self._wrap(CYAN, text)

    def bold(self, text):
        return self._wrap(BOLD, text)

    def warning(self, text):
        return self._wrap(YELLOW, text)

    def dim(self, text):
        return self._wrap(DIM, text)

    def yellow(self, text):
        return self._wrap(YELLOW, text)

    def cyan(self, text):
        return self._wrap(CYAN, text)

    def green(self, text):
        return self._wrap(GREEN, text)

    def write(self, text):
        return text


TRACE_HEADER = "{:<10s} {:>13s}  {:<28s} {:<20s} {:<40s} {}".format(
    "SEQ", "TIME", "FUNCTION", "TASK", "ARGS", "RESULT")


def format_trace_event(event, cw):
    """Format a trace event dict for columnar terminal output.

    Returns a string ready to print, or None for comment events
    (which are printed directly by the caller via cw.warning).

    This function is shared between the shell and CLI so both display
    identical output.  cw is a ColorWriter instance.
    """
    if event.get("type") == "comment":
        return cw.warning("# {}".format(event.get("text", "")))

    retval = event.get("retval", "")
    status = event.get("status", "-")

    # Color retval based on daemon-provided status classification
    if status == "E":
        retval_formatted = cw.error(retval)
    elif status == "O":
        retval_formatted = cw.success(retval)
    else:
        retval_formatted = retval  # neutral: no color

    seq_str = cw.dim(str(event.get("seq", "")))
    lib_str = event.get("lib", "")
    func_str = event.get("func", "")
    lib_func = "{}.{}".format(cw.cyan(lib_str), cw.yellow(func_str))
    task_str = cw.green(event.get("task", ""))

    # For column alignment, compute visible widths and pad manually
    # since ANSI escape codes add invisible characters.
    seq_vis = len(str(event.get("seq", "")))
    lib_func_vis = len(lib_str) + 1 + len(func_str)
    task_vis = len(event.get("task", ""))

    return "{}{} {:>13s}  {}{} {}{} {:<40s} {}".format(
        seq_str,
        " " * max(0, 10 - seq_vis),
        event.get("time", ""),
        lib_func,
        " " * max(0, 28 - lib_func_vis),
        task_str,
        " " * max(0, 20 - task_vis),
        event.get("args", ""),
        retval_formatted)
