"""Console output helpers.

Windows consoles use a legacy code page by default (cp1252 in Spain), and printing
``↓``/``↑`` to one raises ``UnicodeEncodeError`` and kills the process. Any program that
prints progress to a console has to make its own output safe: this switches the standard
streams to UTF-8 and, more importantly, tells them to replace anything unencodable instead
of raising.
"""
from __future__ import annotations

import sys


def ensure_utf8_console() -> None:
    """Make stdout/stderr UTF-8 and never raise on an unencodable character."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # pragma: no cover - already replaced/wrapped
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # pragma: no cover - detached console
            pass
    try:  # best effort on the console code page too
        import ctypes

        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:  # pragma: no cover - not a console
        pass
