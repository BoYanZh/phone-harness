"""Optional color for CLI text. Standard library only.

Color is on only when the stream being written is a real terminal and
NO_COLOR is unset. A pipe, a redirect, or NO_COLOR (set to anything, including
the empty string) stays plain text, so a captured run is byte-for-byte the
same text an agent already parses.
"""
import argparse
import os
import sys

_CODES = {"ok": "32", "err": "31", "note": "2"}


def enabled(stream=None):
    """True when this stream should carry ANSI color."""
    if os.environ.get("NO_COLOR") is not None:
        return False
    stream = sys.stdout if stream is None else stream
    isatty = getattr(stream, "isatty", None)
    if not callable(isatty):
        return False
    try:
        return bool(isatty())
    except OSError:
        return False


def paint(text, kind, stream=None):
    """Wrap text in one color, or return it unchanged."""
    if not text:
        return text
    stream = sys.stdout if stream is None else stream
    if not enabled(stream):
        return text
    return f"\033[{_CODES[kind]}m{text}\033[0m"


def mark(char="✓", stream=None):
    """A green check mark on a terminal, the same character otherwise."""
    return paint(char, "ok", sys.stdout if stream is None else stream)


def err(text, stream=None):
    """An error line, red on a terminal."""
    return paint(text, "err", sys.stderr if stream is None else stream)


def note(text, stream=None):
    """Secondary text, dim on a terminal."""
    return paint(text, "note", sys.stdout if stream is None else stream)


class Parser(argparse.ArgumentParser):
    """argparse that does not accept `--min` as `--minutes`, and colors errors."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)

    def error(self, message):
        self.print_usage(sys.stderr)
        print(err(f"{self.prog}: error: {message}"), file=sys.stderr)
        self.exit(2)


def reject_unknown_flags(parser, argv):
    """Accept this command's flags. Leave other words for the command itself.

    `--help` prints help and exits 0. An unknown flag is an error that names
    the flag and exits 2. Positionals are not rejected here: each command
    already decides how many words it takes.
    """
    _ns, unknown = parser.parse_known_args(argv)
    bad = [tok for tok in unknown if str(tok).startswith("-")]
    if bad:
        parser.error("unrecognized arguments: " + " ".join(bad))
