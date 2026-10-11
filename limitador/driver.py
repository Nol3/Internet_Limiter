"""WinDivert driver lifecycle: stable install path, preflight checks and repair.

Why this module exists (this is the fix for the "WinError 1058" plague of v1):
``pydivert`` bundles ``WinDivert64.dll`` + ``WinDivert64.sys`` and loads the driver from
the DLL's own directory. Inside a PyInstaller *onefile* build that directory is
``%TEMP%\\_MEIxxxx\\pydivert\\windivert_dll``, and it is deleted when the app exits, so
the ``WinDivert`` service ends up pointing at a file that no longer exists. Every launch
after the first therefore failed to load the driver.

We copy both files to a stable per-machine directory and point pydivert at that copy
*before* the first handle is opened, and we remove stale service registrations left over
from an older temp directory.
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import shutil
import subprocess
import sys
import winreg
from dataclasses import dataclass
from pathlib import Path

SERVICE_NAME = "WinDivert"
DRIVER_FILES = ("WinDivert64.dll", "WinDivert64.sys")
CREATE_NO_WINDOW = 0x08000000
SERVICE_KEY = rf"SYSTEM\CurrentControlSet\Services\{SERVICE_NAME}"

_prepared = False
_driver_dir: Path | None = None


# --------------------------------------------------------------------------- admin
def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # pragma: no cover
        return False


# ------------------------------------------------------------------------- locations
def _pydivert_driver_dir() -> Path:
    from pydivert import windivert_dll

    return Path(windivert_dll.__file__).parent


def stable_driver_dir() -> Path:
    """Where the driver is kept permanently (survives temp cleanup/reboots)."""
    global _driver_dir
    if _driver_dir is not None:
        return _driver_dir
    candidates = []
    program_data = os.environ.get("ProgramData")
    if program_data:
        candidates.append(Path(program_data) / "Limitador" / "driver")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "Limitador" / "driver")
    candidates.append(_pydivert_driver_dir())
    _driver_dir = candidates[0]
    return _driver_dir


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()[:16]


def bundled_driver_version() -> str:
    from pydivert import __version__ as pydivert_version

    return f"pydivert {pydivert_version}"


# --------------------------------------------------------------------------- install
def patch_dll_path(directory: Path) -> bool:
    """Make pydivert load WinDivert.dll from ``directory``.

    Must run before the first WinDivert call, because pydivert caches the DLL handle
    on first use.
    """
    from pydivert import windivert_dll

    dll = directory / "WinDivert64.dll"
    if not dll.exists():
        return False
    windivert_dll.DLL_PATH = str(dll)
    return True


def ensure_driver_files() -> tuple[bool, str]:
    """Copy driver DLL+SYS to the stable directory (best effort, idempotent)."""
    target = stable_driver_dir()
    source = _pydivert_driver_dir()
    try:
        if target.resolve() == source.resolve():
            return True, f"usando el driver incluido en {source}"
        target.mkdir(parents=True, exist_ok=True)
        copied = []
        for name in DRIVER_FILES:
            src = source / name
            if not src.exists():
                return False, f"falta {name} en el paquete ({src})"
            dst = target / name
            if not dst.exists() or _sha256(dst) != _sha256(src):
                shutil.copy2(src, dst)
                copied.append(name)
        patch_dll_path(target)
        detail = f"driver en {target}"
        if copied:
            detail += f" (actualizado: {', '.join(copied)})"
        return True, detail
    except OSError as exc:
        # Not fatal: fall back to the bundled copy (works, just not stable across runs).
        return False, f"no se pudo copiar el driver a {target}: {exc}"


def _secure_driver_dir(directory: Path) -> tuple[bool, str]:
    """Restrict write access to Administrators/SYSTEM.

    The driver is a kernel module loaded by an elevated process: if a standard user
    could replace ``WinDivert64.sys`` in that directory, they would get their own code
    into the kernel the next time the app runs elevated. Well-known SIDs are used
    instead of group names so it works on any Windows language.
    """
    if not is_admin():
        return False, "sin privilegios: no se pudo restringir el directorio del driver"
    command = [
        "icacls.exe", str(directory), "/inheritance:r",
        "/grant:r", "*S-1-5-18:(OI)(CI)F",      # SYSTEM
        "/grant:r", "*S-1-5-32-544:(OI)(CI)F",  # Administrators
        "/grant:r", "*S-1-5-32-545:(OI)(CI)RX", # Users: read/execute only
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                creationflags=CREATE_NO_WINDOW)
    except OSError as exc:  # pragma: no cover
        return False, f"icacls no disponible: {exc}"
    if result.returncode != 0:
        return False, (result.stdout or result.stderr or "").strip()[:200]
    return True, "permisos del directorio del driver restringidos a administradores"


def prepare() -> tuple[bool, str]:
    """Idempotent startup step: stable driver files + DLL_PATH patched."""
    global _prepared
    if _prepared:
        return True, "ya preparado"
    ok, detail = ensure_driver_files()
    if not ok:
        patch_dll_path(_pydivert_driver_dir())
    elif is_admin():
        secured, message = _secure_driver_dir(stable_driver_dir())
        detail += f" · {message}"
    _prepared = True
    return ok, detail


# ------------------------------------------------------------------- service state
def service_image_path() -> str | None:
    """ImagePath of the registered WinDivert service, if any."""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, SERVICE_KEY) as key:
            value, _ = winreg.QueryValueEx(key, "ImagePath")
    except OSError:
        return None
    if not isinstance(value, str):
        return None
    expanded = os.path.expandvars(value.replace("%SystemRoot%", os.environ.get("SystemRoot", r"C:\Windows")))
    for prefix in ("\\??\\", "\\SystemRoot\\"):
        if expanded.startswith(prefix):
            expanded = expanded[len(prefix):]
    return expanded


def service_exists() -> bool:
    return service_image_path() is not None


def service_state() -> str:
    """'running', 'stopped' or 'missing'."""
    try:
        result = subprocess.run(
            ["sc.exe", "query", SERVICE_NAME],
            capture_output=True, text=True, creationflags=CREATE_NO_WINDOW,
        )
    except OSError:  # pragma: no cover
        return "missing"
    if result.returncode != 0:
        return "missing"
    out = (result.stdout or "").upper()
    if "RUNNING" in out:
        return "running"
    if "STOPPED" in out:
        return "stopped"
    return "unknown"


def stop_and_delete_service() -> tuple[bool, str]:
    """Remove the service registration so WinDivert can install it cleanly again."""
    messages = []
    for args in (["stop", SERVICE_NAME], ["delete", SERVICE_NAME]):
        try:
            result = subprocess.run(
                ["sc.exe", *args], capture_output=True, text=True,
                creationflags=CREATE_NO_WINDOW,
            )
            messages.append(f"sc {' '.join(args)}: rc={result.returncode}")
        except OSError as exc:  # pragma: no cover
            messages.append(f"sc {' '.join(args)}: {exc}")
    return True, " | ".join(messages)


def is_stale_service() -> bool:
    """True if the registered service points at a file that no longer exists."""
    path = service_image_path()
    if not path:
        return False
    return not Path(path).exists()


def load_test() -> tuple[bool, str]:
    """Try to open a throwaway handle: this is what actually installs the driver."""
    from pydivert import WinDivert

    try:
        with WinDivert("false"):
            pass
        return True, "driver cargado correctamente"
    except OSError as exc:
        return False, f"{exc} (winerror={getattr(exc, 'winerror', '?')})"


def install(force_reinstall: bool = False) -> tuple[bool, str]:
    """Ensure the driver is installed and loadable, repairing if needed."""
    if not is_admin():
        return False, "se necesitan privilegios de administrador para instalar el driver"
    prepare()
    if force_reinstall:
        stop_and_delete_service()
    else:
        path = service_image_path()
        if path and Path(path).exists():
            candidate = Path(path)
            if not (stable_driver_dir() / candidate.name).exists():
                # Service points somewhere else that still exists (old install/temp).
                stop_and_delete_service()
    ok, detail = load_test()
    if not ok:
        # One repair attempt: drop the registration and let WinDivert recreate it.
        stop_and_delete_service()
        ok, detail = load_test()
    return ok, detail


# ---------------------------------------------------------------------- preflight
@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    hint: str = ""
    critical: bool = True

    @property
    def mark(self) -> str:
        if self.ok:
            return "[ok]  "
        return "[FALLA]" if self.critical else "[aviso]"


def _hvci_enabled() -> bool | None:
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios\HypervisorEnforcedCodeIntegrity",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "Enabled")
        return bool(value)
    except OSError:
        return None


def windows_version() -> str:
    """Friendly Windows version (the raw build number alone is misleading)."""
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion",
        ) as key:
            def value(name: str) -> str:
                try:
                    return str(winreg.QueryValueEx(key, name)[0])
                except OSError:
                    return "?"

            product = value("ProductName")
            build = value("CurrentBuild")
            ubr = value("UBR")
            display = value("DisplayVersion")
        return f"{product} {display} (build {build}.{ubr})"
    except OSError:  # pragma: no cover
        return f"build {sys.getwindowsversion().build}"


def preflight(*, check_limits: bool = True) -> list[Check]:
    """Everything that must hold before the engine can limit traffic."""
    from .filters import build_network_filter, validate_filter

    checks: list[Check] = []

    admin = is_admin()
    checks.append(Check("Privilegios de administrador", admin,
                        "elevado" if admin else "proceso sin elevar",
                        "El limitador necesita el driver kernel: ejecútalo como administrador."))

    driver_dir = stable_driver_dir()
    have_files = all((driver_dir / name).exists() for name in DRIVER_FILES)
    checks.append(Check("Archivos del driver", have_files,
                        f"{driver_dir}",
                        "Ejecuta la reparación: copia WinDivert64.dll y WinDivert64.sys "
                        "a la carpeta del driver."))

    if admin:
        state = service_state()
        image = service_image_path() or "(no registrado)"
        stale = is_stale_service()
        checks.append(Check(
            "Servicio del driver", not stale, f"servicio {SERVICE_NAME}: {state} · {image}",
            "El servicio apunta a una ruta que ya no existe: pulsa 'Reparar driver'.",
        ))
    else:
        checks.append(Check("Servicio del driver", True, "no comprobado (sin privilegios)",
                            "", critical=False))

    filters_ok = True
    detail_parts = []
    for direction in ("inbound", "outbound"):
        filt = build_network_filter(direction, exclude_lan=True)
        ok, msg = validate_filter(filt)
        filters_ok = filters_ok and ok
        detail_parts.append(f"{direction}: {'ok' if ok else msg}")
    checks.append(Check("Filtros de captura", filters_ok, " · ".join(detail_parts),
                        "Fallo de programación: revisa limitador/filters.py"))

    hvci = _hvci_enabled()
    if hvci:
        checks.append(Check(
            "Integridad de memoria (HVCI)", True,
            "activada (WinDivert 2.2.2 es compatible)",
            "", critical=False,
        ))

    if check_limits:
        from .engine import engine_available

        ok, detail = engine_available()
        checks.append(Check("Motor de captura", ok, detail,
                            "Instala/repara el driver o reinicia el equipo."))
    return checks


def all_ok(checks: list[Check]) -> bool:
    return all(c.ok for c in checks if c.critical)


def diagnose() -> str:
    """Plain-text report to paste into a bug report."""
    from .rules import APP_VERSION

    lines = [
        f"Limitador {APP_VERSION} - diagnóstico",
        f"Windows: {windows_version()} ({sys.getwindowsversion().platform_str if hasattr(sys.getwindowsversion(), 'platform_str') else ''})",
        f"Python: {sys.version.split()[0]} · frozen={getattr(sys, 'frozen', False)}",
        f"{bundled_driver_version()}",
        f"Administrador: {is_admin()}",
        f"HVCI (integridad de memoria): {_hvci_enabled()}",
        f"Carpeta del driver: {stable_driver_dir()}",
    ]
    for name in DRIVER_FILES:
        path = stable_driver_dir() / name
        lines.append(f"  {name}: {'sí' if path.exists() else 'NO'}"
                     + (f" sha256:{_sha256(path)}" if path.exists() else ""))
    lines.append(f"Servicio: estado={service_state()} imagen={service_image_path()}")
    lines.append(f"Servicio obsoleto: {is_stale_service()}")
    lines.append("")
    for check in preflight():
        lines.append(f"{check.mark} {check.name}: {check.detail}")
        if not check.ok and check.hint:
            lines.append(f"        -> {check.hint}")
    return "\n".join(lines)
