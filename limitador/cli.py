"""Headless mode.

Three uses:

* ``--check``      preflight + diagnostics, no driver needed to report problems;
* ``--list-apps``  which applications are using the network right now;
* everything else  actually limit traffic for a while and print measured rates, which
  makes the engine verifiable without clicking anything.

Examples
--------
    limitador --cli --down 5 --up 1 --duration 30
    limitador --cli --app "C:\\Program Files\\App\\app.exe=1:0.2" --duration 60
    limitador --cli --check
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import driver
from .console import ensure_utf8_console
from .engine import Engine, LimiterError
from .procmap import ProcessMap
from .rules import DEFAULT_LATENCY_HOST, AppRule, Config, Settings
from .units import format_bytes, format_rate, parse_rate

#: Muestras mínimas para poder descartar la de arranque.
MIN_SAMPLES_WITH_RAMP_UP = 3

PROGRESS_TEMPLATE = (
    "[{elapsed:>5.0f}s] ↓ {down:>8} ↑ {up:>8} · "
    "objetivo ↓ {target_down:>8} ↑ {target_up:>8} · "
    "cola {queue:>7} · descartado {dropped:>7}"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="limitador --cli",
        description="Limitador en modo consola (sin interfaz gráfica).",
    )
    parser.add_argument("--cli", action="store_true", help="modo consola (implícito aquí)")
    parser.add_argument(
        "--down", type=str, default="0", help="límite de bajada en MB/s (0 = sin límite)"
    )
    parser.add_argument(
        "--up", type=str, default="0", help="límite de subida en MB/s (0 = sin límite)"
    )
    parser.add_argument("--unit", choices=["MB/s", "KB/s", "Mbit/s"], default="MB/s")
    parser.add_argument(
        "--app",
        action="append",
        default=[],
        metavar="RUTA=BAJADA:SUBIDA",
        help='límite por aplicación (repetible). Ej: "C:\\...\\app.exe=2:0.5"',
    )
    parser.add_argument(
        "--duration", type=float, default=0.0, help="segundos a limitar (0 = hasta Ctrl+C)"
    )
    parser.add_argument("--interval", type=float, default=1.0, help="periodo de muestreo")
    parser.add_argument(
        "--max-delay",
        type=float,
        default=50.0,
        help="presupuesto de retardo en ms (control de bufferbloat)",
    )
    parser.add_argument(
        "--no-control-exemption", action="store_true", help="limitar también ACK/DNS/ICMP"
    )
    parser.add_argument(
        "--limit-lan", action="store_true", help="limitar también el tráfico de la red local"
    )
    parser.add_argument("--no-ipv6", action="store_true", help="no limitar IPv6")
    parser.add_argument(
        "--exempt",
        action="append",
        default=[],
        metavar="RUTA",
        help="aplicación con prioridad absoluta: nunca se limita (repetible). Para juegos.",
    )
    parser.add_argument(
        "--game-mode",
        action="store_true",
        help="perfil de juego: presupuesto de retardo corto (menos bufferbloat)",
    )
    parser.add_argument(
        "--latency",
        type=str,
        default="",
        metavar="HOST",
        help="mide el ping a HOST durante la sesión y lo informa (idealmente, tu servidor)",
    )
    parser.add_argument("--check", action="store_true", help="solo diagnóstico, no limita")
    parser.add_argument("--list-apps", action="store_true", help="listar apps con conexiones")
    parser.add_argument(
        "--unblock",
        action="store_true",
        help="emergencia: libera la captura de red y sale (necesita administrador)",
    )
    parser.add_argument("--json", type=str, default="", help="guardar el informe en JSON")
    parser.add_argument(
        "--screenshot",
        type=str,
        default="",
        metavar="CARPETA",
        help="abre la interfaz y guarda una captura de cada pestaña (documentación)",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="con --screenshot: límites de ejemplo solo en memoria (no guarda nada)",
    )
    return parser


def parse_app_specs(specs: list[str], unit: str) -> list[AppRule]:
    rules = []
    for spec in specs:
        if "=" not in spec:
            raise ValueError(f"--app necesita el formato RUTA=BAJADA:SUBIDA (recibido '{spec}')")
        path, rates = spec.rsplit("=", 1)
        if ":" in rates:
            down_text, up_text = rates.split(":", 1)
        else:
            down_text, up_text = rates, "0"
        rules.append(
            AppRule(
                exe=str(Path(path).resolve()),
                down_bps=parse_rate(down_text, unit),
                up_bps=parse_rate(up_text, unit),
            )
        )
    return rules


def config_from_args(args: argparse.Namespace) -> Config:
    settings = Settings(
        global_down_bps=parse_rate(args.down, args.unit),
        global_up_bps=parse_rate(args.up, args.unit),
        max_delay_ms=args.max_delay,
        exempt_control=not args.no_control_exemption,
        exclude_lan=not args.limit_lan,
        limit_ipv6=not args.no_ipv6,
        game_mode=bool(getattr(args, "game_mode", False)),
        monitor_latency=bool(getattr(args, "latency", "")),
        latency_host=getattr(args, "latency", "") or DEFAULT_LATENCY_HOST,
    )
    config = Config(settings=settings)
    for rule in parse_app_specs(args.app, args.unit):
        config.set_rule(rule)
    for path in getattr(args, "exempt", []):
        # Prioridad absoluta: para el juego. Manda sobre cualquier límite, también el global.
        config.set_rule(AppRule(exe=str(Path(path).resolve()), exempt=True))
    return config


def run_check() -> int:
    _ok, detail = driver.prepare()
    print(f"driver: {detail}")
    print()
    report = driver.diagnose()
    print(report)
    print()
    critical = [c for c in driver.preflight() if c.critical and not c.ok]
    if critical:
        print("Resultado: faltan requisitos para limitar tráfico.")
        return 1
    print("Resultado: todo listo.")
    return 0


def run_list_apps() -> int:
    pmap = ProcessMap()
    pmap.refresh()
    pids = pmap.pids()
    if not pids:
        print("No se han encontrado conexiones.")
        return 0
    rows = sorted(((pmap.name_for(p), p, pmap.port_count(p)) for p in pids), key=lambda r: -r[2])
    print(f"{'APLICACIÓN':<38} {'PID':>7} {'PUERTOS':>8}")
    for name, pid, ports in rows:
        print(f"{name[:38]:<38} {pid:>7} {ports:>8}")
    print(f"\n{len(rows)} procesos con conexiones ({pmap.size} puertos).")
    return 0


def _shaped_totals(snapshot) -> tuple[float, float]:
    """Bytes actually released by the shapers, (down, up)."""
    down = up = 0.0
    for name, data in snapshot.shapers.items():
        if name.endswith(":in"):
            down += data.get("sent_bytes", 0)
        elif name.endswith(":out"):
            up += data.get("sent_bytes", 0)
    return down, up


def _fmt(db: float) -> str:
    return format_rate(db) if db > 0 else "sin límite"


def run_limiter(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    if not config.settings.has_global_limit and not config.active_rules():
        print("No hay ningún límite definido (usa --down/--up o --app).", file=sys.stderr)
        return 2
    engine = Engine(
        config, on_event=lambda event: print(f"  · {event.level}: {event.message}", file=sys.stderr)
    )
    try:
        engine.start()
    except LimiterError as exc:
        print(f"No se pudo iniciar: {exc}", file=sys.stderr)
        return 3
    except OSError as exc:
        print(f"No se pudo iniciar (driver): {exc}", file=sys.stderr)
        return 3

    print(
        f"Limitando: ↓ {_fmt(config.settings.global_down_bps)} · "
        f"↑ {_fmt(config.settings.global_up_bps)}"
        + (
            f" · {len(config.active_rules())} regla(s) por aplicación"
            if config.active_rules()
            else ""
        )
    )
    print(
        f"Retardo máximo inyectado: {config.settings.max_delay_ms:.0f} ms · "
        f"control exento: {'sí' if config.settings.exempt_control else 'no'} · "
        f"LAN excluida: {'sí' if config.settings.exclude_lan else 'no'}"
    )
    print()

    samples: list[dict] = []
    last = engine.snapshot()
    last_shaped = _shaped_totals(last)
    last_time = time.monotonic()
    started = last_time
    try:
        while True:
            time.sleep(max(args.interval, 0.2))
            now = time.monotonic()
            snapshot = engine.snapshot()
            span = max(now - last_time, 1e-6)
            shaped_now = _shaped_totals(snapshot)
            down = (snapshot.bytes_down - last.bytes_down) / span
            up = (snapshot.bytes_up - last.bytes_up) / span
            elapsed = now - started
            sample = {
                "t": round(elapsed, 3),
                "down_bps": down,
                "up_bps": up,
                "shaped_down_bps": max(shaped_now[0] - last_shaped[0], 0) / span,
                "shaped_up_bps": max(shaped_now[1] - last_shaped[1], 0) / span,
                "queued": snapshot.queued_down + snapshot.queued_up,
                "dropped": snapshot.dropped_down + snapshot.dropped_up,
            }
            samples.append(sample)
            if elapsed >= 1.0:  # skip the ramp-up second in the progress display
                print(
                    PROGRESS_TEMPLATE.format(
                        elapsed=elapsed,
                        down=format_rate(down) if down > 0 else "0 B/s",
                        up=format_rate(up) if up > 0 else "0 B/s",
                        target_down=_fmt(config.settings.global_down_bps),
                        target_up=_fmt(config.settings.global_up_bps),
                        queue=format_bytes(sample["queued"]),
                        dropped=format_bytes(snapshot.dropped_down + snapshot.dropped_up),
                    )
                )
            last, last_shaped, last_time = snapshot, shaped_now, now
            if args.duration and elapsed >= args.duration:
                break
    except KeyboardInterrupt:
        print("\nInterrumpido por el usuario.")
    finally:
        final = engine.snapshot()
        engine.stop()

    report = summarise(samples, config, final)
    print_report(report)
    if args.json:
        Path(args.json).write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"\nInforme guardado en {args.json}")
    return 0


def summarise(samples: list[dict], config: Config, final) -> dict:
    """Mean/percentile rates over the measurement window (first second dropped)."""
    # La primera muestra es la referencia: se descarta salvo que haya muy pocas.
    usable = samples[1:] if len(samples) >= MIN_SAMPLES_WITH_RAMP_UP else samples
    down = [s["down_bps"] for s in usable]
    up = [s["up_bps"] for s in usable]
    shaped_down = [s["shaped_down_bps"] for s in usable if s["shaped_down_bps"] > 0]
    shaped_up = [s["shaped_up_bps"] for s in usable if s["shaped_up_bps"] > 0]

    def mean(values: list[float]) -> float:
        return sum(values) / len(values) if values else 0.0

    def peak(values: list[float]) -> float:
        return max(values) if values else 0.0

    target_down = config.settings.global_down_bps
    target_up = config.settings.global_up_bps
    return {
        "seconds_measured": round(
            sum(s["t"] for s in samples) / max(len(samples), 1) * len(usable), 2
        ),
        "samples": len(usable),
        "target_down_bps": target_down,
        "target_up_bps": target_up,
        "mean_down_bps": mean(down),
        "mean_up_bps": mean(up),
        "peak_down_bps": peak(down),
        "peak_up_bps": peak(up),
        "mean_shaped_down_bps": mean(shaped_down),
        "mean_shaped_up_bps": mean(shaped_up),
        "ratio_down": (mean(shaped_down) / target_down) if target_down else None,
        "ratio_up": (mean(shaped_up) / target_up) if target_up else None,
        "total_down_bytes": final.bytes_down,
        "total_up_bytes": final.bytes_up,
        "dropped_down_bytes": final.dropped_down,
        "dropped_up_bytes": final.dropped_up,
        "dropped_packets": final.dropped_packets,
        "queue_overflows": final.queue_overflows,
        "event_layer": final.event_layer or None,
        "per_pid": {
            str(pid): {"down_bytes": d, "up_bytes": u} for pid, (d, u) in final.per_pid.items()
        },
        "shapers": final.shapers,
        "errors": final.errors,
    }


def print_report(report: dict) -> None:
    print("\n" + "=" * 62)
    print("  RESUMEN")
    print("=" * 62)
    print(
        f"  Muestras: {report['samples']} · "
        f"descarga total: {format_bytes(report['total_down_bytes'])} · "
        f"subida total: {format_bytes(report['total_up_bytes'])}"
    )
    print(
        f"  Bajada medida: media {format_rate(report['mean_down_bps'])}, "
        f"pico {format_rate(report['peak_down_bps'])} "
        f"(objetivo {format_rate(report['target_down_bps'])})"
    )
    print(
        f"  Subida medida: media {format_rate(report['mean_up_bps'])}, "
        f"pico {format_rate(report['peak_up_bps'])} "
        f"(objetivo {format_rate(report['target_up_bps'])})"
    )
    for label, value, ratio in (
        ("bajada", report["mean_shaped_down_bps"], report.get("ratio_down")),
        ("subida", report["mean_shaped_up_bps"], report.get("ratio_up")),
    ):
        if ratio is not None:
            print(
                f"  Tráfico {label} efectivamente limitado: {format_rate(value)} "
                f"= {ratio * 100:.1f}% del objetivo"
            )
    print(
        f"  Descartado: bajada {format_bytes(report['dropped_down_bytes'])}, "
        f"subida {format_bytes(report['dropped_up_bytes'])} "
        f"({report['dropped_packets']} paquetes, {report['queue_overflows']} desbordes de cola)"
    )
    print(
        f"  Capa de sockets (atribución por proceso): "
        f"{report['event_layer'] or 'no disponible (tabla de conexiones)'}"
    )
    if report["per_pid"]:
        print("  Procesos con tráfico (bytes bajada/subida):")
        for pid, data in sorted(report["per_pid"].items(), key=lambda kv: -kv[1]["down_bytes"])[
            :10
        ]:
            print(
                f"    pid {pid:>6}: ↓ {format_bytes(data['down_bytes']):>10} "
                f"↑ {format_bytes(data['up_bytes']):>10}"
            )
    if report["errors"]:
        print("  Errores:")
        for error in report["errors"]:
            print(f"    - {error}")
    print("=" * 62)


def run_unblock() -> int:
    """Emergency: stop and remove the WinDivert service so traffic flows again.

    Use this if the app was killed while limiting and the connection looks stuck: with a
    divert handle open, matching packets are held until the driver's queue expires, so
    removing the service (or just killing the app) gives the network back.
    """
    if not driver.is_admin():
        print(
            "Necesitas privilegios de administrador.\n"
            "Abre el símbolo del sistema como administrador y repite, o usa "
            "tools\\unblock.cmd con el botón derecho > Ejecutar como administrador.",
            file=sys.stderr,
        )
        return 1
    print(f"Estado del servicio: {driver.service_state()}")
    print(f"Imagen registrada:   {driver.service_image_path()}")
    _ok, detail = driver.stop_and_delete_service()
    print(f"Liberando: {detail}")
    state = driver.service_state()
    print(f"Estado tras liberar: {state}")
    if state == "missing":
        print("Listo: la captura está liberada y el tráfico de red vuelve a fluir sin límites.")
        return 0
    print(
        "No se pudo eliminar el servicio: cierra Limitador (Administrador de tareas) y "
        "vuelve a intentarlo.",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_console()
    args = build_parser().parse_args(argv)
    if args.unblock:
        return run_unblock()
    if args.check:
        return run_check()
    if args.list_apps:
        return run_list_apps()
    return run_limiter(args)
