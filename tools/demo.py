"""Demostración guiada del limitador: tres mediciones reales con descargas de verdad.

    tools\\demo.cmd                     (pide UAC una vez y ejecuta la demostración)

Fases:
  1. Referencia sin limitador  -> velocidad real de la línea.
  2. Límite global de N MB/s.
  3. Límite solo a una aplicación (curl.exe).

Al final imprime una tabla comparativa con el porcentaje alcanzado sobre el objetivo.

Protección: hay un vigilante que mata el proceso si se pasa del tiempo máximo; al morir el
proceso se cierran los descriptores de WinDivert y el tráfico vuelve a fluir. Además,
``limitador --unblock`` libera la captura a mano en cualquier momento.
"""

from __future__ import annotations

import argparse
import json
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
from limitador.rules import AppRule, Config, Settings

MB = 1024 * 1024
NUL = "NUL"  # dispositivo nulo de Windows: curl.exe lo acepta tal cual
PROBE_BYTES = 10 * MB
DEFAULT_SECONDS = 12.0
DEFAULT_GLOBAL_MBPS = 5.0
DEFAULT_APP_MBPS = 2.0
TOLERANCE = 0.20  # margen aceptado sobre el objetivo (±20%)
CURL = shutil.which("curl") or r"C:\Windows\System32\curl.exe"

MIRRORS: tuple[str, ...] = (
    "https://nbg1-speed.hetzner.com/1GB.bin",
    "https://proof.ovh.net/files/1Gb.dat",
    "https://fra.download.datapacket.com/1000mb.bin",
    "https://ash-speed.hetzner.com/1GB.bin",
    "https://download.thinkbroadband.com/1GB.zip",
)


# --------------------------------------------------------------------------- safety
def install_watchdog(seconds: float) -> None:
    """Parada dura: matar este proceso libera la captura y la red vuelve."""

    def kill() -> None:
        time.sleep(seconds)
        print(
            f"\n[vigilante] se superó el tiempo máximo ({seconds:.0f}s). "
            "Cerrando el proceso para liberar la red…",
            flush=True,
        )
        os._exit(3)

    threading.Thread(target=kill, daemon=True).start()


# ------------------------------------------------------------------------ downloads
def curl_probe(url: str) -> float:
    """Velocidad de bajada (bytes/s) en una sonda corta, o 0.0 si el espejo no responde."""
    cmd = [
        CURL,
        "-s",
        "-o",
        NUL,
        "-r",
        f"0-{PROBE_BYTES}",
        "--max-time",
        "10",
        "-w",
        "%{http_code} %{speed_download}",
        url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=40, check=False)
    code, _, speed = (result.stdout or "").strip().partition(" ")
    if code.strip() not in ("200", "206"):  # 206 = respuesta a una petición por rangos
        return 0.0
    try:
        return float(speed)
    except ValueError:
        return 0.0


def pick_mirror() -> str:
    """Primer espejo que responde; se elige el más rápido de los que responden."""
    print("Buscando un espejo de pruebas que responda:")
    best_url, best_speed = "", 0.0
    for url in MIRRORS:
        host = url.split("/")[2]
        print(f"  {host:<34}", end="", flush=True)
        try:
            speed = curl_probe(url)
        except Exception as exc:  # red, DNS, timeout
            print(f"error ({type(exc).__name__})")
            continue
        if speed <= 0:
            print("no responde")
            continue
        print(f"{speed / MB:6.1f} MB/s")
        if speed > best_speed:
            best_url, best_speed = url, speed
    return best_url


def measure(url: str, seconds: float, engine: Engine | None = None) -> dict:
    """Descarga durante ``seconds`` segundos y mide la velocidad media real."""
    cmd = [
        CURL,
        "-s",
        "-o",
        NUL,
        "--max-time",
        str(seconds),
        "-w",
        "%{speed_download} %{size_download} %{http_code}",
        url,
    ]
    started = time.monotonic()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    while proc.poll() is None:
        elapsed = time.monotonic() - started
        live = ""
        if engine is not None:
            snapshot = engine.snapshot()
            live = f"  ·  el limitador ve {snapshot.bytes_down / max(elapsed, 0.01) / MB:5.2f} MB/s"
        print(f"\r    descargando… {elapsed:4.1f}s{live}", end="", flush=True)
        time.sleep(0.4)
    print("\r" + " " * 78 + "\r", end="")
    raw = (proc.stdout.read() if proc.stdout else "").strip()
    parts = raw.split()
    if len(parts) < 3:
        return {"ok": False, "error": f"curl no devolvió datos ({raw!r})"}
    speed, size, code = parts[0], parts[1], parts[2]
    if code not in ("200", "206"):
        return {"ok": False, "error": f"HTTP {code}"}
    return {
        "ok": True,
        "mbps": float(speed) / MB,
        "bytes": int(size),
        "seconds": time.monotonic() - started,
    }


def shaper_rate(engine: Engine, key: str) -> float:
    """Velocidad que el propio planificador cree estar aplicando (para contrastar)."""
    snapshot = engine.snapshot()
    entry = snapshot.shapers.get(key) or {}
    for field_name in ("rate_bps", "current_bps", "rate", "bps"):
        if field_name in entry:
            return float(entry[field_name]) / MB
    return 0.0


# ------------------------------------------------------------------------------ main
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Demostración guiada del limitador.")
    parser.add_argument("--seconds", type=float, default=DEFAULT_SECONDS, help="segundos por fase")
    parser.add_argument(
        "--limit", type=float, default=DEFAULT_GLOBAL_MBPS, help="límite global MB/s"
    )
    parser.add_argument(
        "--app-limit", type=float, default=DEFAULT_APP_MBPS, help="límite por app MB/s"
    )
    parser.add_argument("--url", type=str, default="", help="usar este espejo en vez de buscarlo")
    parser.add_argument(
        "--only",
        choices=["all", "base", "global", "app"],
        default="all",
        help="ejecutar solo una fase (para comprobar sin administrador)",
    )
    parser.add_argument("--json", type=str, default="", help="guardar el informe en JSON")
    parser.add_argument("--watchdog", type=float, default=0.0, help="segundos máximos (0 = auto)")
    return parser.parse_args()


def main() -> int:
    ensure_utf8_console()
    args = parse_args()
    phases = ["base", "global", "app"] if args.only == "all" else [args.only]
    needs_admin = any(phase in ("global", "app") for phase in phases)

    if needs_admin and not driver.is_admin():
        print(
            "Esta demostración necesita privilegios de administrador.\n"
            "Ejecuta:  tools\\demo.cmd        (pide UAC una vez)\n"
            "o abre una consola como administrador y repite el comando.",
            file=sys.stderr,
        )
        return 1

    watchdog = args.watchdog if args.watchdog > 0 else args.seconds * len(phases) * 3 + 120
    install_watchdog(watchdog)

    if needs_admin:
        ok, detail = driver.prepare()
        print(f"driver: {detail}")
        if not ok:
            print("No se pudo preparar el driver WinDivert.", file=sys.stderr)
            return 1

    url = args.url or pick_mirror()
    if not url:
        print("Ningún espejo de pruebas responde: comprueba tu conexión.", file=sys.stderr)
        return 1
    print(f"Espejo: {url}\n")

    results: dict[str, dict] = {}
    engine: Engine | None = None

    try:
        if "base" in phases:
            print(f"[1/3] Referencia: sin limitador, {args.seconds:.0f}s de descarga")
            base = measure(url, args.seconds)
            results["base"] = base
            if base["ok"]:
                print(f"      línea base: {base['mbps']:.2f} MB/s")
            else:
                print(f"      fallo en la medición: {base['error']}")
                return 1

        if "global" in phases:
            target = args.limit
            if results.get("base", {}).get("mbps", 0) <= target:
                print(
                    f"\n[2/3] AVISO: tu línea ({results['base']['mbps']:.1f} MB/s) no es más rápida "
                    f"que el límite ({target:g} MB/s); usa --limit menor para que se note."
                )
            print(f"\n[2/3] Límite global de {target:g} MB/s")
            config = Config(
                settings=Settings(
                    global_down_bps=int(target * MB), global_up_bps=0, max_delay_ms=50.0
                )
            )
            engine = Engine(config)
            engine.start()
            try:
                limited = measure(url, args.seconds, engine)
                limited["target"] = target
                limited["shaper"] = shaper_rate(engine, "global:in")
            finally:
                engine.stop()
                engine = None
            results["global"] = limited
            if limited["ok"]:
                print(
                    f"      medido: {limited['mbps']:.2f} MB/s  "
                    f"({limited['mbps'] / target:.0%} del objetivo)"
                )
            else:
                print(f"      fallo en la medición: {limited['error']}")

        if "app" in phases:
            target = args.app_limit
            print(f"\n[3/3] Límite solo a {Path(CURL).name} de {target:g} MB/s")
            config = Config(
                settings=Settings(global_down_bps=0, global_up_bps=0, max_delay_ms=50.0)
            )
            config.set_rule(AppRule(exe=CURL, down_bps=int(target * MB), up_bps=0))
            engine = Engine(config)
            engine.start()
            try:
                limited = measure(url, args.seconds, engine)
                limited["target"] = target
                limited["shaper"] = shaper_rate(engine, f"app:{Config.normalise_exe(CURL)}:in")
            finally:
                engine.stop()
                engine = None
            results["app"] = limited
            if limited["ok"]:
                print(
                    f"      medido: {limited['mbps']:.2f} MB/s  "
                    f"({limited['mbps'] / target:.0%} del objetivo)"
                )
            else:
                print(f"      fallo en la medición: {limited['error']}")
    finally:
        if engine is not None:  # nunca dejar la captura abierta
            engine.stop()

    print("\n" + "=" * 62)
    print("RESULTADO DE LA DEMOSTRACIÓN")
    print("=" * 62)
    verdicts = []
    if "base" in results and results["base"]["ok"]:
        print(f"{'Referencia (sin limitador)':<40}{results['base']['mbps']:>9.2f} MB/s")
    for key, label in (("global", "Límite global"), ("app", "Límite por aplicación")):
        if key not in results:
            continue
        entry = results[key]
        if not entry["ok"]:
            print(f"{label:<40}{'FALLO':>9}   {entry['error']}")
            verdicts.append(False)
            continue
        ratio = entry["mbps"] / entry["target"]
        inside = abs(ratio - 1.0) <= TOLERANCE
        verdicts.append(inside)
        heading = f"{label} ({entry['target']:g} MB/s)"
        state = "OK" if inside else "FUERA DE MARGEN"
        print(f"{heading:<40}{entry['mbps']:>9.2f} MB/s{ratio:>7.0%} del objetivo  {state}")
        if entry.get("shaper"):
            print(f"{'    (lo que aplica el planificador)':<40}{entry['shaper']:>9.2f} MB/s")
    print("=" * 62)
    if not verdicts:
        print("RESULTADO: solo referencia (sin fases de límite, no hay veredicto)")
        ok = True
    else:
        ok = all(verdicts)
        print(f"RESULTADO: {'OK' if ok else 'REVISAR'}")

    if args.json:
        Path(args.json).write_text(
            json.dumps({"url": url, "results": results}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"Informe guardado en {args.json}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
