"""Demostración del motivo del proyecto: el ping bajo carga (bufferbloat).

Necesita administrador (usa el motor). Mide la latencia real en tres situaciones, con una
descarga de fondo de verdad:

  1. **En reposo** - referencia: qué ping tienes cuando no hay nada más.
  2. **Con carga y SIN limitador** - el bufferbloat: tu propio tráfico llena la cola del enlace
     y el ping se dispara aunque la velocidad sea alta. Es lo que se siente como "me va a
     tirones" o "el hit no registra".
  3. **Con carga y el limitador en modo juego** - el tráfico de fondo se recorta y el
     presupuesto de retardo baja a 20 ms, así que el ping se mantiene cerca del de reposo.

Uso:

    powershell -NoProfile -ExecutionPolicy Bypass -File tools\\run_elevated.ps1 ^
        -Script latency_probe.py -ScriptArgs "--host 1.1.1.1 --seconds 6" -TimeoutSec 600

Para que la medida sirva para jugar, apunta ``--host`` a la IP del servidor al que te conectas.
"""

from __future__ import annotations

import argparse
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
from limitador.latency import LatencyMonitor, LatencyStats
from limitador.rules import (
    GAME_MODE_MAX_DELAY_MS,
    AppRule,
    Config,
    Settings,
)

MB = 1024 * 1024
NUL = "NUL"
CURL = shutil.which("curl") or r"C:\Windows\System32\curl.exe"
URL = "https://fra.download.datapacket.com/1000mb.bin"
LINE_SECONDS = 5.0
#: El recorte del modo juego: se deja un margen para que la cola no se llene nunca.
GAME_CAP_FRACTION = 0.85
#: Recorte suave: el uso correcto del modo juego (no obliga a descartar).
GENTLE_CAP_FRACTION = 0.95


def install_watchdog(seconds: float) -> None:
    def kill() -> None:
        time.sleep(seconds)
        print(f"\n[vigilante] más de {seconds:.0f}s: cerrando para liberar la red…", flush=True)
        os._exit(3)

    threading.Thread(target=kill, daemon=True).start()


class Load:
    """Descarga de fondo en un hilo, para generar carga mientras se mide el ping."""

    def __init__(self, url: str) -> None:
        self.url = url
        self._process: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._process = subprocess.Popen(
            [CURL, "-s", "-o", NUL, "--max-time", "600", self.url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def stop(self) -> None:
        if self._process is not None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - raro
                self._process.kill()
            self._process = None


def measure_line(url: str, seconds: float) -> float:
    """Velocidad de la línea (MB/s) con una descarga sin límites."""
    result = subprocess.run(
        [CURL, "-s", "-o", NUL, "--max-time", str(seconds), "-w", "%{speed_download}", url],
        capture_output=True,
        text=True,
        timeout=seconds + 30,
        check=False,
    )
    try:
        return float((result.stdout or "0").strip()) / MB
    except ValueError:
        return 0.0


def phase(
    name: str,
    host: str,
    seconds: float,
    *,
    load: Load | None = None,
    engine: Engine | None = None,
) -> LatencyStats:
    """Mide latencia durante ``seconds``, con carga y/o motor si se indican."""
    if engine is not None:
        engine.start()
    if load is not None:
        load.start()
    monitor = LatencyMonitor(host=host, interval=0.25, window=int(seconds / 0.25) + 4)
    monitor.start()
    try:
        time.sleep(seconds)
    finally:
        monitor.stop()
        if load is not None:
            load.stop()
        if engine is not None:
            engine.stop()
    stats = monitor.stats()
    print(f"   {name:<34} {stats.line()}")
    return stats


def main() -> int:
    ensure_utf8_console()
    if not driver.is_admin():
        print("Necesita administrador (carga el motor WinDivert).", file=sys.stderr)
        return 1
    parser = argparse.ArgumentParser(description="Ping bajo carga: el bufferbloat medido.")
    parser.add_argument("--host", default="1.1.1.1", help="a quién se mide el ping")
    parser.add_argument("--seconds", type=float, default=6.0, help="segundos por fase")
    parser.add_argument("--url", default=URL, help="espejo para generar la carga")
    args = parser.parse_args()

    driver.prepare()
    install_watchdog(args.seconds * 3 + LINE_SECONDS + 90)

    print(f"Objetivo del ping: {args.host}  ·  {args.seconds:.0f}s por fase\n")
    line = measure_line(args.url, LINE_SECONDS)
    print(f"línea medida (sin límites): {line:.2f} MB/s\n")

    load = Load(args.url)
    idle = phase("1. en reposo (sin carga)", args.host, args.seconds)

    loaded = phase("2. con carga, SIN limitador", args.host, args.seconds, load=load)

    cap = max(line * GAME_CAP_FRACTION, 0.5)
    config = Config(
        settings=Settings(
            global_down_bps=int(cap * MB),
            global_up_bps=int(cap * MB),
            max_delay_ms=GAME_MODE_MAX_DELAY_MS,
            game_mode=True,
        )
    )
    engine = Engine(config)
    limited = phase(
        f"3. con carga, modo juego ({cap:.1f} MB/s)",
        args.host,
        args.seconds,
        load=load,
        engine=engine,
    )

    gentle_cap = max(line * GENTLE_CAP_FRACTION, 0.5)

    # Cuarta fase (la prueba decisiva): el motor capturando y reinyectando SIN ningún límite.
    # Separa "el motor está en el camino" de "el motor está limitando": si el ping también se
    # dispara aquí, el coste es del propio motor, no del recorte.
    plain_config = Config(settings=Settings())
    engine_plain = Engine(plain_config)
    passthrough = phase(
        "4. con carga, motor capturando SIN límites",
        args.host,
        args.seconds,
        load=load,
        engine=engine_plain,
    )

    # Quinta fase: EL ARREGLO ARQUITECTÓNICO. Se limita SOLO la descarga (curl) y el motor
    # captura únicamente su tráfico: el ping no entra en el motor, así que nada puede retrasarlo.
    # Es el uso recomendado para jugar: limitar lo que molesta y no tocar lo demás.
    scoped_config = Config(
        settings=Settings(
            capture_scope="limited",
            max_delay_ms=GAME_MODE_MAX_DELAY_MS,
            game_mode=True,
        )
    )
    scoped_config.set_rule(
        AppRule(exe=CURL, down_bps=int(gentle_cap * MB), up_bps=int(gentle_cap * MB))
    )
    engine_scoped = Engine(scoped_config)
    scoped = phase(
        "5. con carga, limite por aplicacion con alcance 'limited'",
        args.host,
        args.seconds,
        load=load,
        engine=engine_scoped,
    )

    print("\n" + "=" * 62)
    print("  QUÉ HA HECHO EL MOTOR (para que 'ping bajo' no sea 'no capturó nada')")
    print("=" * 62)
    for name, engine_obj in (
        ("sin límites", engine_plain),
        ("modo juego (todo)", engine),
        ("alcance limited", engine_scoped),
    ):
        snap = engine_obj.snapshot()
        seen = (snap.bytes_down + snap.bytes_up) / MB
        forwarded = (snap.forwarded_down + snap.forwarded_up) / MB
        dropped = (snap.dropped_down + snap.dropped_up) / MB
        print(
            f"  {name:<20} visto {seen:7.1f} MB · reenviado {forwarded:7.1f} MB"
            f" · descartado {dropped:6.1f} MB · paquetes descartados {snap.dropped_packets:>7}"
        )
    print("  (con alcance 'limited' el motor solo ve el tráfico de lo que limita: por eso el ping")
    print("   de otra aplicación no puede ser retrasado, y aun así la descarga sí se limita)")

    print("\n" + "=" * 62)
    print("  QUÉ HA PASADO")
    print("=" * 62)
    base = idle.avg_ms or 1.0
    print(f"  ping en reposo:                            {idle.avg_ms:6.0f} ms")
    print(
        f"  con carga, sin limitador:                  {loaded.avg_ms:6.0f} ms  "
        f"({loaded.avg_ms - base:+.0f} ms)"
    )
    print(
        f"  con carga, modo juego (un descriptor):     {limited.avg_ms:6.0f} ms  "
        f"({limited.avg_ms - base:+.0f} ms)"
    )
    print(
        f"  con carga, motor sin límites:              {passthrough.avg_ms:6.0f} ms  "
        f"({passthrough.avg_ms - base:+.0f} ms)"
    )
    print(
        f"  pico peor: sin motor {loaded.max_ms:.0f} / motor sin límites {passthrough.max_ms:.0f} / "
        f"modo juego {limited.max_ms:.0f} ms"
    )
    print(
        f"  jitter:    sin motor {loaded.jitter_ms:.0f} / motor sin límites "
        f"{passthrough.jitter_ms:.0f} / alcance limited {scoped.jitter_ms:.0f} ms"
    )

    print(
        f"  con carga, motor sin límites (capturando todo): {passthrough.avg_ms:6.0f} ms  "
        f"({passthrough.avg_ms - base:+.0f} ms)"
    )
    print(
        f"  con carga, alcance 'limited' (solo lo que se limita): {scoped.avg_ms:6.0f} ms  "
        f"({scoped.avg_ms - base:+.0f} ms)"
    )
    best = min(limited.avg_ms, scoped.avg_ms)
    reference = max(loaded.avg_ms, idle.avg_ms)
    if scoped.avg_ms + 20 < limited.avg_ms:
        print(
            "\n  RESULTADO: el arreglo funciona. Capturando solo lo que se limita, el ping se queda"
        )
        print(
            f"             en {scoped.avg_ms:.0f} ms ({scoped.avg_ms - base:+.0f} ms), frente a "
            f"{limited.avg_ms:.0f} ms ({limited.avg_ms - base:+.0f} ms) capturando todo."
        )
        print(
            "             Lo que no entra en el motor no puede esperar en la cola del driver: esa"
        )
        print(
            "             es la palanca de verdad, y por eso el alcance 'limited' es lo recomendado"
        )
        print("             para jugar (limitar lo que molesta y no tocar lo demás).")
    elif passthrough.avg_ms > reference + 20:
        print("\n  RESULTADO: el coste está en el MOTOR, no en el recorte. Sin ningún límite, solo")
        print(
            f"             capturando y reinyectando, el ping sube a {passthrough.avg_ms:.0f} ms "
            f"({passthrough.avg_ms - base:+.0f} ms)."
        )
        print(
            "             Cada paquete pasa por Python y con una descarga a tope ese coste se pone"
        )
        print(
            "             delante de tus paquetes. El arreglo es capturar menos: alcance 'limited'."
        )
    elif best <= reference + 5:
        print("\n  RESULTADO: el limitador no añade latencia apreciable bajo carga.")
    else:
        print(
            f"\n  RESULTADO: bajo carga el limitador añade {best - reference:.0f} ms de más "
            "(eso es lo que hay que corregir)."
        )
    if loaded.avg_ms > idle.avg_ms + 20:
        print("\n  NOTA: esta línea SÍ tiene bufferbloat (el ping sube con carga sin limitar):")
        print("        ahí el recorte del modo juego es lo que lo corrige.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
