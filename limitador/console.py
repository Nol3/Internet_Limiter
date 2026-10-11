"""Console output helpers.

Windows consoles use a legacy code page by default (cp1252 in Spain), and printing
``↓``/``↑`` to one raises ``UnicodeEncodeError`` and kills the process. Any program that
prints progress to a console has to make its own output safe: this switches the standard
streams to UTF-8 and, more importantly, tells them to replace anything unencodable instead
of raising.
"""

from __future__ import annotations

import ctypes
import sys
from contextlib import suppress


def ensure_utf8_console() -> None:
    """Make stdout/stderr UTF-8 and never raise on an unencodable character."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # pragma: no cover - already replaced/wrapped
            continue
        with suppress(ValueError, OSError):  # pragma: no cover - consola desacoplada
            reconfigure(encoding="utf-8", errors="replace")
    # Esfuerzo extra: la página de códigos de la consola.
    with suppress(Exception):  # pragma: no cover - sin consola
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
