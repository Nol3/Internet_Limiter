"""Experimento: que velocidad real se consigue con distintas tasas y presupuestos de retardo.

Necesita administrador. Para cada combinación arranca el motor con un límite global, descarga
de verdad y compara tres cosas:

* la velocidad que ve el archivo descargado (lo que llega al usuario),
* la velocidad que el motor captura,
* los bytes que el **planificador** dice haber liberado.

Si el planificador libera la tasa pedida pero el archivo recibe mucho menos, el cuello no es el
limitador: es TCP reaccionando al retardo añadido. Si el planificador libera menos de lo pedido,
el cuello está en el motor.

Uso:

    powershell -NoProfile -ExecutionPolicy Bypass -File tools\\run_elevated.ps1 ^
        -Script rate_probe.py -TimeoutSec 600
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from limitador import driver
from limitador.console import ensure_utf8_console
from limitador.engine import Engine
from limitador.rules import Config, Settings

MB = 1024 * 1024
NUL = "NUL"
SECONDS = 6.0
URL = "https://proof.ovh.net/files/1Gb.dat"
CURL = shutil.which("curl") or r"C:\Windows\System32\curl.exe"

#: (objetivo MB/s, presupuesto de retardo ms)
CASES: tuple[tuple[float, float], ...] = (
    (60.0, 50.0),
    (30.0, 50.0),
    (30.0, 5.0),
    (15.0, 50.0),
    (15.0, 5.0),
    (10.0, 50.0),
    (5.0, 50.0),
)


def install_watchdog(seconds: float) -> None:
    def kill() -> None:
        time.sleep(seconds)
        print(f"\n[vigilante] más de {seconds:.0f}s: cerrando para liberar la red…", flush=True)
        os._exit(3)

    threading.Thread(target=kill, daemon=True).start()


def download(seconds: float) -> float:
    """Velocidad media (MB/s) de una descarga real."""
    cmd = [
        CURL,
        "-s",
        "-o",
        NUL,
        "--max-time",
        str(seconds),
        "-w",
        "%{speed_download}",
        URL,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=seconds + 30, check=False)
    try:
        return float((result.stdout or "0").strip()) / MB
    except ValueError:
        return 0.0


def main() -> int:
    ensure_utf8_console()
    if not driver.is_admin():
        print("Necesita administrador.", file=sys.stderr)
        return 1
    driver.prepare()
    install_watchdog(len(CASES) * (SECONDS + 4) + 90)

    print(f"Espejo: {URL}  ·  {SECONDS:.0f}s por caso\n")
    print(
        f"{'objetivo':>9} {'retardo':>8} {'archivo':>9} {'motor':>9} {'planificador':>13}"
        f" {'%obj':>5} {'descartes':>10} {'cola':>8} {'ret.máx':>8}"
    )

    for target, delay in CASES:
        config = Config(
            settings=Settings(
                global_down_bps=int(target * MB),
                global_up_bps=0,
                max_delay_ms=delay,
            )
        )
        engine = Engine(config)
        engine.start()
        try:
            measured = download(SECONDS)
            snapshot = engine.snapshot()
            shaper = snapshot.shapers.get("global:in", {})
            released = float(shaper.get("sent_bytes", 0)) / MB / SECONDS
            print(
                f"{target:>7.1f}M {delay:>7.1f}m {measured:>8.2f}M "
                f"{snapshot.bytes_down / MB / SECONDS:>8.2f}M {released:>12.2f}M "
                f"{measured / target:>4.0%} {snapshot.dropped_packets:>10} "
                f"{snapshot.queued_down / MB:>7.2f}M "
                f"{float(shaper.get('max_observed_delay_ms', 0)):>7.1f}m"
            )
        except Exception as exc:
            print(f"{target:>7.1f}M {delay:>7.1f}m   error: {type(exc).__name__}: {exc}")
        finally:
            engine.stop()

    print(
        "\nLectura:\n"
        "  · 'planificador' se queda corto y la cola está VACÍA -> el motor libera todo lo que le\n"
        "    llega: quien no manda más es el emisor (espejo que estrangula la IP, o TCP bajando su\n"
        "    ritmo por los descartes). No es un fallo del limitador.\n"
        "  · 'planificador' se queda corto y la cola está LLENA -> el cuello está en el motor\n"
        "    (o en el presupuesto de retardo).\n"
        "  · 'planificador' llega al objetivo y 'archivo' no -> el limitador cumple; la velocidad\n"
        "    final la fija el emisor.\n"
        "Ojo: la cola vacía distingue un caso del otro, y mirar solo los MB/s lleva a culpar al\n"
        "limitador de un recorte que hace el servidor (pasó, y costó horas)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
