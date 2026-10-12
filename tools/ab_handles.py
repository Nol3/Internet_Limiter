"""A/B: descriptores separados para capturar e inyectar, frente a uno solo (por defecto).

Necesita administrador. Mide en la MISMA sesión y con el MISMO espejo, alternando los dos
modos, para que un espejo que estrangule la IP no se confunda con el efecto del cambio.

Uso:

    powershell -NoProfile -ExecutionPolicy Bypass -File tools\\run_elevated.ps1 ^
        -Script ab_handles.py -ScriptArgs "--url https://... --seconds 6" -TimeoutSec 900
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for path in (str(ROOT), str(ROOT / "tools")):  # raíz del proyecto y tools/ (rate_probe)
    if path not in sys.path:
        sys.path.insert(0, path)

from rate_probe import MB, download

from limitador import driver
from limitador.console import ensure_utf8_console
from limitador.engine import SPLIT_HANDLES_ENV, Engine
from limitador.rules import Config, Settings

DEFAULT_URL = "https://fra.download.datapacket.com/1000mb.bin"
TARGETS: tuple[float, ...] = (30.0, 15.0, 8.0)


def install_watchdog(seconds: float) -> None:
    def kill() -> None:
        time.sleep(seconds)
        print(f"\n[vigilante] más de {seconds:.0f}s: cerrando para liberar la red…", flush=True)
        os._exit(3)

    threading.Thread(target=kill, daemon=True).start()


def measure(target: float, url: str, seconds: float, split: bool) -> tuple[float, float]:
    """Devuelve (velocidad del archivo, velocidad capturada por el motor), en MB/s."""
    if split:
        os.environ[SPLIT_HANDLES_ENV] = "1"
    else:
        os.environ.pop(SPLIT_HANDLES_ENV, None)
    config = Config(settings=Settings(global_down_bps=int(target * MB), global_up_bps=0))
    engine = Engine(config)
    engine.start()
    try:
        file_rate = download(seconds, url)
        captured = engine.snapshot().bytes_down / MB / seconds
        return file_rate, captured
    finally:
        engine.stop()
        os.environ.pop(SPLIT_HANDLES_ENV, None)


def main() -> int:
    ensure_utf8_console()
    if not driver.is_admin():
        print("Necesita administrador.", file=sys.stderr)
        return 1
    parser = argparse.ArgumentParser(description="A/B de descriptores separados.")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--seconds", type=float, default=6.0)
    args = parser.parse_args()

    driver.prepare()
    install_watchdog(len(TARGETS) * 2 * (args.seconds + 4) + 120)

    print(f"Espejo: {args.url}  ·  {args.seconds:.0f}s por medida")
    print("Alternando modos en la misma sesión (la línea se mide antes y después)\n")
    before = download(args.seconds, args.url)
    print(f"línea antes de empezar:  {before:6.2f} MB/s\n")

    print(f"{'objetivo':>9} | {'un descriptor':>22} | {'descriptores separados':>24}")
    print(f"{'':>9} | {'archivo':>10} {'motor':>10} | {'archivo':>10} {'motor':>10}")

    for target in TARGETS:
        single_file, single_cap = measure(target, args.url, args.seconds, split=False)
        split_file, split_cap = measure(target, args.url, args.seconds, split=True)
        print(
            f"{target:>7.1f}M | {single_file:>10.2f} {single_cap:>10.2f} | "
            f"{split_file:>10.2f} {split_cap:>10.2f}"
        )

    after = download(args.seconds, args.url)
    print(f"\nlínea al terminar:       {after:6.2f} MB/s")
    print(
        "Si las dos columnas se parecen, el descriptor no era el cuello y la velocidad la fija\n"
        "el emisor (o el espejo). Si 'descriptores separados' es claramente mayor, el cambio\n"
        "sirve; si es menor, hay que revertirlo."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
