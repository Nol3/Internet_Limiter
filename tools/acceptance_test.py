"""Acceptance test: proves the limiter actually limits, on the real network.

Run it elevated (it loads the WinDivert driver):

    .venv\\Scripts\\python.exe tools\\acceptance_test.py --seconds 5

Phases, in this order, so a broken build can never cost you the connection:

0. **Forwarding** - the engine captures with no limits and a real HTTP request must still
   succeed. If this fails the test aborts *before* any limiting is applied.
1. **Baseline** - throughput with no limit (what the line + mirror give).
2. **Global limit** - the same download with a limit set to a third of the baseline: the
   measured rate must sit at the target, not at the line rate.
3. **Per-application limit** - the download runs in ``curl.exe`` while only that process is
   limited; the limiter must create that rule's shaper and cap the traffic.

Safety nets: a watchdog thread kills this process if the whole run exceeds
``--watchdog`` seconds (process death closes the WinDivert handles, so the network comes
back immediately), and ``tools\\run_elevated.ps1`` kills the process tree on a timeout.

Two independent measurements per phase: the size of the file curl writes, and the engine's
own byte counters. A phase whose download transferred almost nothing is reported as a
*failed measurement* (mirror blocked / rate limited), never as a limiter failure.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from limitador import driver
from limitador.console import ensure_utf8_console
from limitador.engine import Engine, LimiterError
from limitador.rules import AppRule, Config, Settings
from limitador.units import MB

CURL = r"C:\Windows\System32\curl.exe"
CREATE_NO_WINDOW = 0x08000000
NUL = "NUL"  # Windows device: accepted natively by curl.exe
PROBE_BYTES = 10 * MB
MIN_MEASURABLE = 1 * MB


# --------------------------------------------------------------------------- safety
def install_watchdog(seconds: float) -> None:
    """Hard stop: killing this process releases the capture and the network."""

    def kill() -> None:
        time.sleep(seconds)
        print(
            f"\n[vigilante] se superó el tiempo máximo ({seconds:.0f}s). Cerrando el proceso "
            "para liberar la red (al morir el proceso se cierran los descriptores de "
            "WinDivert).",
            flush=True,
        )
        os._exit(9)

    threading.Thread(target=kill, name="watchdog", daemon=True).start()


# ------------------------------------------------------------------------ downloads
class Mirror:
    """A big test file on a public speed-test server, probed before use."""

    CANDIDATES: tuple[str, ...] = (
        "https://nbg1-speed.hetzner.com/1GB.bin",
        "https://proof.ovh.net/files/1Gb.dat",
        "https://fra.download.datapacket.com/1000mb.bin",
        "https://download.thinkbroadband.com/1GB.zip",
        "https://ash-speed.hetzner.com/1GB.bin",
    )

    def __init__(self, url: str, speed: float) -> None:
        self.url = url
        self.speed = speed

    @property
    def name(self) -> str:
        return self.url.split("/")[2]

    @classmethod
    def _probe(cls, url: str) -> float:
        """Bytes/second of a ranged request, or 0.0 if the server is not usable."""
        argv = [
            CURL,
            "-sS",
            "-r",
            f"0-{PROBE_BYTES}",
            "-o",
            NUL,
            "--max-time",
            "8",
            "-w",
            "%{http_code} %{size_download} %{speed_download}",
            url,
        ]
        try:
            result = subprocess.run(
                argv, capture_output=True, text=True, creationflags=CREATE_NO_WINDOW, timeout=25
            )
        except (OSError, subprocess.TimeoutExpired):
            return 0.0
        parts = (result.stdout or "").strip().split()
        if len(parts) != 3 or parts[0] not in ("200", "206"):
            return 0.0
        try:
            received, speed = float(parts[1]), float(parts[2])
        except ValueError:
            return 0.0
        # A tiny body for a 10 MB request means an error page (403/429 bodies are ~1 byte).
        if received < PROBE_BYTES * 0.95:
            return 0.0
        return speed

    @classmethod
    def select(cls, verbose: bool = True) -> Mirror | None:
        best: Mirror | None = None
        for url in cls.CANDIDATES:
            speed = cls._probe(url)
            if verbose:
                mark = f"{speed / MB:.1f} MB/s" if speed else "no disponible"
                print(f"   {url.split('/')[2]:<34} {mark}")
            if speed > 2 * MB and (best is None or speed > best.speed):
                best = cls(url, speed)
        if best and verbose:
            print(f"   -> espejo elegido: {best.name} ({best.speed / MB:.1f} MB/s)")
        return best


class Download:
    """One curl.exe process pulling a single large file; its size is the arrival rate."""

    def __init__(self, mirror: Mirror) -> None:
        self.mirror = mirror
        self.dir = Path(tempfile.mkdtemp(prefix="limitador-acceptance-"))
        self.file = self.dir / "download.bin"
        self.error_path = self.dir / "curl.err"
        self.process: subprocess.Popen | None = None

    def start(self) -> None:
        with open(self.error_path, "wb") as error_stream:
            self.process = subprocess.Popen(
                [CURL, "-sS", "-o", str(self.file), self.mirror.url],
                stdout=subprocess.DEVNULL,
                stderr=error_stream,
                creationflags=CREATE_NO_WINDOW,
            )

    def file_bytes(self) -> int:
        try:
            return self.file.stat().st_size
        except OSError:
            return 0

    def curl_error(self) -> str:
        try:
            return self.error_path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return ""

    def cleanup(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_for(self, seconds: float, engine: Engine | None = None) -> dict[str, float]:
        self.start()
        started = time.monotonic()
        samples: list[tuple[float, int]] = [(0.0, 0)]
        engine_samples: list[tuple[float, int]] = []
        finished_early = False
        try:
            end = started + seconds
            while time.monotonic() < end:
                if self.process.poll() is not None:
                    finished_early = True
                    break
                time.sleep(0.25)
                samples.append((time.monotonic() - started, self.file_bytes()))
                if engine is not None:
                    engine_samples.append(
                        (time.monotonic() - started, engine.snapshot().bytes_down)
                    )
        finally:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:  # pragma: no cover
                    self.process.kill()

        result = {
            "file_rate": _rate(samples),
            "total_mb": samples[-1][1] / MB,
            "finished": float(finished_early),
            "error": self.curl_error(),
        }
        if engine_samples:
            result["engine_rate"] = _rate(engine_samples)
        self.cleanup()
        return result


def _rate(samples: list[tuple[float, int]], mode: str = "mean", skip_seconds: float = 1.2) -> float:
    """Sustained bytes/second from a counter sampled at irregular intervals.

    The first ``skip_seconds`` are ignored: TCP ramp-up, and the up to one second it takes
    the connection table to attribute a flow to a process, would inflate the average.
    """
    deltas = [
        (samples[i][1] - samples[i - 1][1]) / (samples[i][0] - samples[i - 1][0])
        for i in range(1, len(samples))
        if samples[i][0] > samples[i - 1][0]
        and samples[i][1] > samples[i - 1][1]
        and samples[i][0] >= skip_seconds
    ]
    if not deltas:
        return 0.0
    return max(deltas) if mode == "peak" else sum(deltas) / len(deltas)


# ---------------------------------------------------------------------------- engine
def start_engine(config: Config) -> Engine:
    engine = Engine(config, on_event=lambda event: print(f"    · {event.level}: {event.message}"))
    try:
        engine.start()
    except (LimiterError, OSError) as exc:
        print(f"[FALLA] no se pudo iniciar el motor: {exc}")
        sys.exit(3)
    return engine


def http_probe() -> tuple[bool, str]:
    """A tiny real request: proves packets are being reinjected while we capture."""
    argv = [
        CURL,
        "-sS",
        "-o",
        NUL,
        "--max-time",
        "8",
        "-w",
        "%{http_code} %{time_total}s",
        "https://proof.ovh.net/files/1Mb.dat",
    ]
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, creationflags=CREATE_NO_WINDOW, timeout=20
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"curl no respondió: {exc}"
    output = (result.stdout or "").strip()
    error = (result.stderr or "").strip()
    return output.startswith("200"), output or error


def label(value: float) -> str:
    return f"{value / MB:.2f} MB/s"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=5.0, help="duración de cada fase")
    parser.add_argument(
        "--watchdog",
        type=float,
        default=0.0,
        help="segundos máximos de toda la prueba (0 = automático)",
    )
    args = parser.parse_args()

    watchdog = args.watchdog if args.watchdog > 0 else args.seconds * 12 + 90
    install_watchdog(watchdog)
    ensure_utf8_console()

    if not driver.is_admin():
        print("[FALLA] ejecútame como administrador (necesita el driver kernel).")
        return 3
    if not Path(CURL).exists():
        print(f"[FALLA] no encuentro {CURL}")
        return 3

    _ok, detail = driver.prepare()
    print(f"driver: {detail}")
    print(driver.diagnose())
    print()

    # ------------------------------------------------- 0. forwarding with no limits
    print("0) COMPROBACIÓN DE REENVÍO (motor capturando, sin límites)…")
    monitor = start_engine(Config(settings=Settings(global_down_bps=0, global_up_bps=0)))
    time.sleep(1.0)
    forwarded, probe_detail = http_probe()
    snapshot = monitor.snapshot()
    monitor.stop()
    print(f"   petición HTTP con el motor activo: {probe_detail}")
    print(
        f"   paquetes reenviados: bajada {snapshot.forwarded_down} B, "
        f"subida {snapshot.forwarded_up} B"
    )
    print(f"   capturados: bajada {snapshot.bytes_down} B, subida {snapshot.bytes_up} B")
    if not forwarded or snapshot.bytes_down <= 0:
        print("\n[FALLA] con el motor activo no hay conectividad. Se aborta sin aplicar límites.")
        print(f"   liberación de emergencia: {driver.stop_and_delete_service()[1]}")
        return 2

    # ------------------------------------------------------------ mirror + baseline
    print("\nBuscando un espejo de descarga…")
    mirror = Mirror.select()
    if mirror is None:
        print("[FALLA] ningún espejo de prueba responde: no se puede medir")
        return 2

    print(f"\n1) BASELINE sin límite ({args.seconds:.0f}s)…")
    baseline = Download(mirror).run_for(args.seconds)
    line_rate = baseline["file_rate"]
    print(f"   velocidad de línea: {label(line_rate)} ({baseline['total_mb']:.0f} MB)")
    if line_rate < 1 * MB:
        print(
            "[FALLA] el espejo no dio ancho de banda suficiente para medir: "
            f"{baseline['error'][:120]}"
        )
        return 2

    target = max(line_rate / 3.0, 512 * 1024)
    invalid: list[str] = []

    # ------------------------------------------------------------ 2. global limit
    print(f"\n2) LÍMITE GLOBAL a {label(target)} ({args.seconds:.0f}s)…")
    config = Config(settings=Settings(global_down_bps=target, max_delay_ms=50))
    engine = start_engine(config)
    limited = Download(mirror).run_for(args.seconds, engine)
    snapshot = engine.snapshot()
    engine.stop()
    del config
    print(
        f"   medido por archivo:  {label(limited['file_rate'])} "
        f"({limited['file_rate'] / target * 100:.0f}% del objetivo, "
        f"{limited['total_mb']:.0f} MB)"
    )
    print(f"   medido por el motor: {label(limited['engine_rate'])}")
    if limited["total_mb"] < 1:
        invalid.append(f"fase 2 sin datos ({limited['error'][:80]})")

    # ------------------------------------------------------- 3. per-app limit (curl)
    print(f"\n3) LÍMITE POR APLICACIÓN ({label(target)}) sobre curl.exe ({args.seconds:.0f}s)…")
    config = Config(settings=Settings(global_down_bps=0, max_delay_ms=50))
    config.set_rule(AppRule(exe=CURL, name="curl.exe", down_bps=target))
    engine = start_engine(config)
    per_app = Download(mirror).run_for(args.seconds, engine)
    snapshot_app = engine.snapshot()
    engine.stop()
    app_shaper = snapshot_app.shapers.get("app:" + Config.normalise_exe(CURL) + ":in")
    print(
        f"   medido por archivo:  {label(per_app['file_rate'])} "
        f"({per_app['file_rate'] / target * 100:.0f}% del objetivo, "
        f"{per_app['total_mb']:.0f} MB)"
    )
    print(f"   medido por el motor: {label(per_app['engine_rate'])}")
    print(f"   shaper de la regla por aplicación: {'creado' if app_shaper else 'NO CREADO'}")
    print(
        f"   capa de sockets: {'activa' if snapshot_app.socket_events_ok else 'no disponible'} · "
        f"puertos identificados: {snapshot_app.port_entries}"
    )
    if per_app["total_mb"] < 1:
        invalid.append(f"fase 3 sin datos ({per_app['error'][:80]})")

    # ------------------------------------------------------------------ veredicto
    print("\n" + "=" * 62)
    print("  VEREDICTO")
    print("=" * 62)
    problems: list[str] = []

    def check(name: str, value: float) -> None:
        if value > target * 1.25:
            problems.append(f"{name}: no se aplica el límite ({label(value)} > {label(target)})")
        elif value < target * 0.6:
            problems.append(f"{name}: muy por debajo del objetivo ({label(value)})")

    if not invalid:
        check("límite global (archivo)", limited["file_rate"])
        check("límite global (motor)", limited["engine_rate"])
        check("límite por aplicación (archivo)", per_app["file_rate"])
        check("límite por aplicación (motor)", per_app["engine_rate"])
    if not app_shaper:
        problems.append("no se creó el shaper de la regla por aplicación")

    print(f"  espejo:                 {mirror.name}")
    print(f"  línea:                  {label(line_rate)}")
    print(f"  objetivo:               {label(target)}")
    print(
        f"  límite global:          {label(limited['file_rate'])} "
        f"(motor {label(limited['engine_rate'])})"
    )
    print(
        f"  límite por aplicación:  {label(per_app['file_rate'])} "
        f"(motor {label(per_app['engine_rate'])})"
    )
    dropped = (snapshot.dropped_down + snapshot_app.dropped_down) / MB
    print(f"  descartado (bajada):    {dropped:.2f} MB")
    released = snapshot.released or snapshot_app.released
    print(f"  captura liberada:       {'SÍ (fallo de seguridad)' if released else 'no'}")
    if released:
        problems.append("el motor liberó la captura durante la prueba")
    for phase_errors in (snapshot.errors, snapshot_app.errors):
        for error in phase_errors[:3]:
            print(f"   error: {error}")

    if invalid:
        print("\n  RESULTADO: MEDICIÓN NO VÁLIDA")
        for item in invalid:
            print(f"   - {item}")
        print("   (el espejo dejó de servir datos; no es un fallo del limitador)")
        return 2
    if problems:
        print("\n  RESULTADO: FALLA")
        for problem in problems:
            print(f"   - {problem}")
        return 1
    print("\n  RESULTADO: OK - el limitador limita de verdad (global y por aplicación)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
