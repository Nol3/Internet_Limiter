"""Application entry point: logging, single instance, UAC elevation, mode dispatch."""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import driver
from .rules import APP_VERSION, AppRule, Config, Settings, log_path

if TYPE_CHECKING:  # solo para anotaciones: no cargar la interfaz al arrancar
    import tkinter as tk

WINDOW_TITLE = f"Limitador {APP_VERSION}"
MUTEX_NAME = "Local\\LimitadorSingleInstance"

#: ShellExecuteW devuelve un valor mayor que 32 cuando tuvo éxito (documentado por Microsoft).
SHELLEXECUTE_MIN_SUCCESS = 32
#: ERROR_ALREADY_EXISTS de la API de Windows.
ERROR_ALREADY_EXISTS = 183

#: Una captura se considera vacía si tiene como mucho estos colores y son casi negros.
BLANK_MAX_COLORS = 2
BLANK_MAX_BRIGHTNESS = 40

#: Handle del mutex de instancia única, en un dict para no necesitar ``global``.
_instance_guard: dict[str, object] = {"handle": None}

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
    return int(result) > SHELLEXECUTE_MIN_SUCCESS


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
    already = kernel32.GetLastError() == ERROR_ALREADY_EXISTS
    if handle:
        kernel32.CloseHandle(handle)
    return bool(already)


def acquire_instance() -> bool:
    """Claim the single-instance mutex for this process."""
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, True, MUTEX_NAME)
    # Se mantiene vivo mientras el proceso exista (el mutex se libera al morir).
    _instance_guard["handle"] = handle
    return kernel32.GetLastError() != ERROR_ALREADY_EXISTS


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
    user32.ShowWindow(handle, 9)  # SW_RESTORE
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
    parser.add_argument(
        "--repair-driver", action="store_true", help="reinstala el driver WinDivert y sale"
    )
    parser.add_argument(
        "--minimized", action="store_true", help="arranca en la bandeja del sistema"
    )
    parser.add_argument(
        "--no-elevate",
        action="store_true",
        help="no pedir privilegios automáticamente (para depurar)",
    )
    parser.add_argument("--verbose", action="store_true", help="log detallado")
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
    return parser.parse_known_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point. ``--cli`` and friends are forwarded to the console mode."""
    # Import perezoso a propósito: no cargar la interfaz ni el modo consola si no toca.
    from .console import ensure_utf8_console  # noqa: PLC0415

    ensure_utf8_console()
    raw = list(sys.argv[1:] if argv is None else argv)
    if any(token in raw for token in ("--cli", "--check", "--list-apps")):
        from . import cli  # noqa: PLC0415

        setup_logging("--verbose" in raw)
        return cli.main(raw)

    args, _unknown = parse_args(raw)
    setup_logging(args.verbose)

    if args.screenshot:
        return run_screenshot(Path(args.screenshot), demo=args.demo)

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

    from .gui import LimitadorApp  # noqa: PLC0415

    config = Config.load()
    app = LimitadorApp(config, start_minimized=args.minimized)
    try:
        app.mainloop()
    except KeyboardInterrupt:  # pragma: no cover
        app._on_close(force=True)
    return 0


# ------------------------------------------------------------------------ capturas
def demo_config() -> Config:
    """Configuración de ejemplo (solo en memoria) para las capturas de documentación.

    No se guarda en disco: se usa únicamente para que las imágenes muestren la interfaz
    con límites puestos.
    """
    config = Config(
        settings=Settings(
            global_down_bps=5 * 1024 * 1024,
            global_up_bps=1 * 1024 * 1024,
            max_delay_ms=50.0,
        )
    )
    office = Path(r"C:\Program Files\Microsoft Office") / "root" / "Office16" / "OUTLOOK.EXE"
    examples = (
        (r"C:\Program Files\Google\Chrome\Application\chrome.exe", 2 * 1024 * 1024, 512 * 1024),
        (str(office), 1 * 1024 * 1024, 0),
    )
    for exe, down, up in examples:
        config.set_rule(AppRule(exe=exe, down_bps=down, up_bps=up))
    return config


def _grab_window(app: tk.Misc, ImageGrab) -> Any:
    """Captura la ventana de la aplicación.

    Se captura la ventana por su handle (PrintWindow) y no la pantalla: así la imagen sale
    bien aunque la ventana esté tapada o la pantalla no se haya repintado. Si el resultado
    sale en negro se reintenta, y como último recurso se captura la región de pantalla.
    """
    handles = []
    with contextlib.suppress(Exception):
        handles.append(app.winfo_id())
        parent = ctypes.windll.user32.GetParent(app.winfo_id())
        if parent:
            handles.append(parent)

    image = None
    for _attempt in range(6):
        for hwnd in handles:
            with contextlib.suppress(Exception):
                image = ImageGrab.grab(window=hwnd)
                if not _looks_blank(image):
                    return image
        app.lift()
        app.update()
        time.sleep(0.6)

    x, y = app.winfo_rootx(), app.winfo_rooty()
    width, height = app.winfo_width(), app.winfo_height()
    if image is None or _looks_blank(image):
        image = ImageGrab.grab(bbox=(x, y, x + width, y + height))
    return image


def _looks_blank(image: Any) -> bool:
    """True si la captura es un color plano casi negro (ventana sin repintar)."""
    colors = image.convert("RGB").getcolors(maxcolors=4)
    if colors is None:
        return False
    return (
        len(colors) <= BLANK_MAX_COLORS
        and max(sum(color) for _count, color in colors) < BLANK_MAX_BRIGHTNESS
    )


def run_screenshot(out_dir: Path, demo: bool = False) -> int:
    """Abre la interfaz y guarda una imagen de cada pestaña.

    Cumple dos papeles: material para la documentación y verificación de que la interfaz
    se construye entera (incluidas las dependencias opcionales de la bandeja) en el
    ejecutable empaquetado. No arranca el motor: no toca el tráfico.
    """
    from .gui import LimitadorApp  # noqa: PLC0415

    try:
        from PIL import ImageGrab  # noqa: PLC0415
    except ImportError:  # pragma: no cover - Pillow es dependencia de la bandeja
        print("Hace falta Pillow para capturar la interfaz.", file=sys.stderr)
        return 2

    config = demo_config() if demo else Config.load()
    # Nunca arrancar el motor durante una captura: no queremos alterar el tráfico.
    config.settings.start_on_launch = False
    config.settings.minimize_to_tray = False

    out_dir.mkdir(parents=True, exist_ok=True)
    app = LimitadorApp(config)
    app.attributes("-topmost", True)
    app.deiconify()
    app.lift()
    app.update()
    time.sleep(1.5)

    names = ("general", "aplicaciones", "avanzado", "registro")
    problems = 0
    for index, tab_id in enumerate(app.notebook.tabs()):
        app.notebook.select(tab_id)
        app.update()
        time.sleep(0.9)
        image = _grab_window(app, ImageGrab)
        blank = _looks_blank(image)
        problems += int(blank)
        name = names[index] if index < len(names) else str(index)
        path = out_dir / f"gui-{name}.png"
        image.save(path)
        state = "VACIA (revisar)" if blank else "ok"
        print(f"captura guardada: {path}  {image.size[0]}x{image.size[1]}  [{state}]")

    app._on_close(force=True)
    if problems:
        print(
            f"aviso: {problems} captura(s) salieron vacias; vuelve a intentarlo.", file=sys.stderr
        )
        return 1
    return 0
