"""Windows autostart via Task Scheduler (evita UAC prompt en login).

Usa schtasks.exe para crear una tarea con 'Run with highest privileges' +
trigger 'At log on'. Esto lanza el exe como admin sin mostrar UAC.

Nota: autostart solo funciona con el ejecutable compilado (build.bat).
En modo dev (python main.py) la tarea no se puede registrar correctamente.
"""
from __future__ import annotations
import subprocess
import sys
from pathlib import Path

TASK_NAME = "Limitador"
CREATE_NO_WINDOW = 0x08000000


def _run(*args: str) -> tuple[int, str]:
    try:
        r = subprocess.run(
            ["schtasks.exe", *args],
            capture_output=True, text=True,
            creationflags=CREATE_NO_WINDOW,
        )
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return -1, str(e)


def current_exe_path() -> str | None:
    """Devuelve ruta del exe compilado, o None si corre en modo dev."""
    if getattr(sys, "frozen", False):
        return sys.executable
    return None


def is_enabled() -> bool:
    rc, _ = _run("/Query", "/TN", TASK_NAME)
    return rc == 0


def enable() -> tuple[bool, str]:
    """Crea tarea que arranca el exe al iniciar sesion, con privilegios altos."""
    exe = current_exe_path()
    if exe is None:
        return False, (
            "Autostart solo funciona con el ejecutable compilado.\n"
            "Ejecuta build.bat para generar dist\\Limitador.exe y luego úsalo."
        )
    if not Path(exe).exists():
        return False, f"Ruta no existe: {exe}"
    # /RL HIGHEST = run with highest privileges -> sin UAC prompt al login
    # /SC ONLOGON = trigger al iniciar sesion
    # /F = force overwrite si ya existe
    rc, out = _run(
        "/Create",
        "/TN", TASK_NAME,
        "/TR", f'"{exe}"',
        "/SC", "ONLOGON",
        "/RL", "HIGHEST",
        "/F",
    )
    return rc == 0, out


def disable() -> tuple[bool, str]:
    rc, out = _run("/Delete", "/TN", TASK_NAME, "/F")
    return rc == 0, out


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "off":
        ok, msg = disable()
    else:
        ok, msg = enable()
    print(f"{'[OK]' if ok else '[ERROR]'} {msg}")
    sys.exit(0 if ok else 1)
