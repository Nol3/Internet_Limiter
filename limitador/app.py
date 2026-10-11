"""Application entry point: logging, single instance, UAC elevation, mode dispatch."""
from __future__ import annotations

import argparse
import ctypes
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import driver
from .rules import APP_VERSION, Config, log_path

WINDOW_TITLE = f"Limitador {APP_VERSION}"
MUTEX_NAME = "Local\\LimitadorSingleInstance"
_guard_handle = None
log = logging.getLogger("limitador")


# ------------------------------------------------------------------------ logging
def setup_logging(verbose: bool = False) -> None:
    root = logging.getLogger("limitador")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    if root.handlers:
        return
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=512 * 1024, backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        root.addHandler(handler)
    except OSError:  # pragma: no cover
        pass
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
    root.addHandler(stream)
    logging.getLogger("pydivert").setLevel(logging.WARNING)


# --------------------------------------------------------------------- elevation
def relaunch_as_admin() -> bool:
    """Relaunch this process elevated. Returns True when a new process was started."""
    if getattr(sys, "frozen", False):
        program = sys.executable
        arguments = " ".join(f'"{a}"' for a in sys.argv[1:])
    else:
        program = sys.executable
        entry = Path(sys.argv[0]).resolve()
        arguments = " ".join([f'"{entry}"', *(f'"{a}"' for a in sys.argv[1:])])
    try:
        result = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", program, arguments, str(Path.cwd()), 1
        )
    except Exception as exc:  # pragma: no cover
        log.error("no se pudo solicitar elevación: %s", exc)
        return False
    return int(result) > 32


def _message_box(title: str, text: str) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)
    except Exception:  # pragma: no cover
        print(f"{title}: {text}", file=sys.stderr)


# ---------------------------------------------------------------- single instance
def another_instance_running() -> bool:
    """Check without holding the mutex (used before elevation)."""
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    already = kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS
    if handle:
        kernel32.CloseHandle(handle)
    return bool(already)


def acquire_instance() -> bool:
    """Claim the single-instance mutex for this process."""
    global _guard_handle
    kernel32 = ctypes.windll.kernel32
    _guard_handle = kernel32.CreateMutexW(None, True, MUTEX_NAME)
    if kernel32.GetLastError() == 183:
        return False
    if not _guard_handle:  # pragma: no cover
        return True
    return True


def focus_existing_window(title: str = "Limitador") -> bool:
    user32 = ctypes.windll.user32
    handle = user32.FindWindowW(None, title)
    if not handle:
        for candidate in (WINDOW_TITLE, title):
            handle = user32.FindWindowW(None, candidate)
            if handle:
                break
    if not handle:
        return False
    user32.ShowWindow(handle, 9)      # SW_RESTORE
    user32.SetForegroundWindow(handle)
    return True


# ------------------------------------------------------------------------- modes
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="Limitador",
        description="Limitador de ancho de banda por aplicación y por equipo (Windows).",
    )
    parser.add_argument("--version", action="version", version=f"Limitador {APP_VERSION}")
    parser.add_argument("--diagnose", action="store_true", help="imprime el diagnóstico y sale")
    parser.add_argument("--repair-driver", action="store_true",
                        help="reinstala el driver WinDivert y sale")
    parser.add_argument("--minimized", action="store_true",
                        help="arranca en la bandeja del sistema")
    parser.add_argument("--no-elevate", action="store_true",
                        help="no pedir privilegios automáticamente (para depurar)")
    parser.add_argument("--verbose", action="store_true", help="log detallado")
    return parser.parse_known_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point. ``--cli`` and friends are forwarded to the console mode."""
    from .console import ensure_utf8_console

    ensure_utf8_console()
    raw = list(sys.argv[1:] if argv is None else argv)
    if any(token in raw for token in ("--cli", "--check", "--list-apps")):
        from . import cli

        setup_logging("--verbose" in raw)
        return cli.main(raw)

    args, _unknown = parse_args(raw)
    setup_logging(args.verbose)

    if args.diagnose:
        ok, detail = driver.prepare()
        print(detail)
        print(driver.diagnose())
        return 0 if driver.all_ok(driver.preflight()) else 1

    if args.repair_driver:
        if not driver.is_admin():
            print("Necesitas privilegios de administrador para reparar el driver.", file=sys.stderr)
            return 1
        ok, detail = driver.install(force_reinstall=True)
        print(f"{'Correcto' if ok else 'Fallo'}: {detail}")
        return 0 if ok else 1

    if os.name != "nt":  # pragma: no cover
        print("Limitador solo funciona en Windows.", file=sys.stderr)
        return 1

    if another_instance_running():
        if focus_existing_window(WINDOW_TITLE):
            log.info("ya hay una instancia activa: trayendo su ventana al frente")
        else:
            log.warning("ya hay una instancia de Limitador en ejecución")
        return 0

    if not driver.is_admin() and not args.no_elevate:
        log.info("sin privilegios: solicitando elevación por UAC")
        if relaunch_as_admin():
            return 0
        _message_box(
            "Limitador",
            "Limitador necesita privilegios de administrador para cargar el driver de "
            "captura de paquetes (WinDivert).\n\n"
            "Sin ellos no puede limitar el tráfico. Vuelve a abrirlo y acepta el aviso de "
            "Windows, o ejecútalo con el botón derecho > Ejecutar como administrador.",
        )
        return 1

    if not acquire_instance():  # pragma: no cover - race with another launch
        focus_existing_window(WINDOW_TITLE)
        return 0

    ok, detail = driver.prepare()
    log.debug("driver: %s", detail)
    for check in driver.preflight(check_limits=False):
        if not check.ok:
            log.warning("comprobación previa: %s -> %s", check.name, check.detail)

    from .gui import LimitadorApp

    config = Config.load()
    app = LimitadorApp(config, start_minimized=args.minimized)
    try:
        app.mainloop()
    except KeyboardInterrupt:  # pragma: no cover
        app._on_close(force=True)
    return 0
