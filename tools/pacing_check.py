"""Real-time pacing check (no driver, no admin): does the shaper hit its rate?

The unit tests use a fake clock, which cannot catch the mistake that actually happened
here: ``time.sleep`` on Windows only resolves to ~0.5 ms, so a 60 us sleep costs hundreds
of microseconds and the achieved rate collapses to a fraction of the configured one.

    .venv\\Scripts\\python.exe tools\\pacing_check.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from limitador.console import ensure_utf8_console  # noqa: E402
from limitador.shaper import Shaper  # noqa: E402

MB = 1024 * 1024


def measure(rate: float, seconds: float, size: int = 1500) -> float:
    shaper = Shaper(rate, max_delay_ms=50)
    started = time.monotonic()
    total = 0
    while time.monotonic() - started < seconds:
        allow, wait = shaper.plan(size)
        if not allow:
            continue
        shaper.wait(wait)
        total += size
    elapsed = time.monotonic() - started
    return total / elapsed


def main() -> int:
    ensure_utf8_console()
    print("Comprobación de precisión del limitador (tiempo real, 1,5 s por objetivo)\n")
    print(f"{'objetivo':>12} {'logrado':>12} {'relación':>10}")
    failed = []
    for target in (30 * MB, 10 * MB, 3 * MB, 512 * 1024):
        achieved = measure(target, 1.5)
        ratio = achieved / target
        mark = "ok" if 0.85 <= ratio <= 1.05 else "MAL"
        if mark == "MAL":
            failed.append((target, achieved))
        print(f"{target / MB:>9.2f} MB/s {achieved / MB:>9.2f} MB/s {ratio * 100:>9.1f}% {mark}")
    if failed:
        print("\nFallos:")
        for target, achieved in failed:
            print(f"  objetivo {target / MB:.2f} MB/s, logrado {achieved / MB:.2f} MB/s")
        return 1
    print("\nTodos los objetivos dentro del 15%.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
