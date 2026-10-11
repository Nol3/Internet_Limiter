"""Rate/size parsing and formatting helpers (single source of truth for the UI)."""
from __future__ import annotations

MB = 1024 * 1024
KB = 1024

UNITS: dict[str, float] = {"KB/s": KB, "MB/s": MB, "Mbit/s": 125_000.0}


def parse_rate(text: str, unit: str = "MB/s") -> float:
    """Parse a user-typed rate into bytes/second. Empty or invalid -> 0.0 (= sin límite).

    Accepts both ``1,5`` and ``1.5`` because Spanish keyboards produce commas.
    """
    if text is None:
        return 0.0
    cleaned = str(text).strip().replace(",", ".")
    if not cleaned:
        return 0.0
    try:
        value = float(cleaned)
    except ValueError:
        raise ValueError(f"'{text}' no es un número válido") from None
    if value < 0:
        raise ValueError("el límite no puede ser negativo")
    return value * UNITS.get(unit, MB)


def format_rate(bps: float, unit: str | None = None) -> str:
    """Human readable rate. ``0`` renders as 'sin límite'."""
    if bps <= 0:
        return "sin límite"
    if unit is None:
        if bps >= MB:
            unit = "MB/s"
        elif bps >= KB:
            unit = "KB/s"
        else:
            unit = "B/s"
    divisor = {"B/s": 1.0, "KB/s": KB, "MB/s": MB, "Mbit/s": 125_000.0}.get(unit, MB)
    value = bps / divisor
    if unit == "B/s":
        return f"{value:.0f} {unit}"
    return f"{value:.2f}".rstrip("0").rstrip(".") + f" {unit}"


def format_bytes(n: float) -> str:
    for unit, div in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if abs(n) >= div:
            return f"{n / div:.2f} {unit}"
    return f"{n:.0f} B"


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def to_display(bps: float, unit: str = "MB/s") -> str:
    """Rate in bytes/s -> plain number in the given unit, for editing in a text box."""
    if bps <= 0:
        return ""
    return f"{bps / UNITS.get(unit, MB):g}"
