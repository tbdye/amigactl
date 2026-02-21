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
    return sys.stdout.isatty()


# ANSI escape sequences
RESET = "\033[0m"
BOLD = "\033[1m"
RED = "\033[31m"
GREEN = "\033[32m"
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

    def write(self, text):
        return text
