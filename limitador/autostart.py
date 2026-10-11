"""Windows autostart through Task Scheduler.

A scheduled task with ``/RL HIGHEST`` starts the app elevated at logon *without* a UAC
prompt, which is the only clean way to auto-start a program that needs the WinDivert
driver.

v1 refused to work unless the app was frozen; here the launch command is built from
either the compiled ``.exe`` or ``pythonw.exe + main.py``, so autostart also works while
developing.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TASK_NAME = "Limitador"
CREATE_NO_WINDOW = 0x08000000


def _run(*args: str) -> tuple[int, str]:
    try:
        result = subprocess.run(
            ["schtasks.exe", *args],
            capture_output=True,
            text=True,
            creationflags=CREATE_NO_WINDOW,
        )
        return result.returncode, (result.stdout or "") + (result.stderr or "")
    except OSError as exc:  # pragma: no cover
        return -1, str(exc)


def launch_command() -> tuple[str, str]:
    """``(program, arguments)`` that starts this app as it is running right now."""
    if getattr(sys, "frozen", False):
        return sys.executable, ""
    interpreter = Path(sys.executable)
    pythonw = interpreter.with_name("pythonw.exe")
    program = str(pythonw if pythonw.exists() else interpreter)
    entry = Path(__file__).resolve().parent.parent / "main.py"
    return program, f'"{entry}" --minimized'


def is_enabled() -> bool:
    rc, _ = _run("/Query", "/TN", TASK_NAME)
    return rc == 0


def current_target() -> str | None:
    """'Task To Run' of the existing task, or None if there is no task."""
    rc, out = _run("/Query", "/TN", TASK_NAME, "/FO", "LIST", "/V")
    if rc != 0:
        return None
    for line in out.splitlines():
        if ":" in line and "task to run" in line.lower():
            return line.split(":", 1)[1].strip()
    return None


def matches_current_install() -> bool:
    """True if the existing task points at the program we would register now."""
    target = current_target()
    if not target:
        return False
    program, _ = launch_command()
    return program.lower() in target.lower()


def enable() -> tuple[bool, str]:
    program, arguments = launch_command()
    if not Path(program).exists():
        return False, f"no existe el programa a registrar: {program}"
    command = f'"{program}"' + (f" {arguments}" if arguments else "")
    rc, out = _run(
        "/Create",
        "/TN",
        TASK_NAME,
        "/TR",
        command,
        "/SC",
        "ONLOGON",
        "/RL",
        "HIGHEST",
        "/F",
    )
    if rc == 0:
        return True, f"tarea '{TASK_NAME}' creada: {command}"
    if "access is denied" in out.lower() or "acceso denegado" in out.lower():
        return False, "acceso denegado: abre Limitador como administrador y reinténtalo"
    return False, out.strip() or f"schtasks devolvió {rc}"


def disable() -> tuple[bool, str]:
    rc, out = _run("/Delete", "/TN", TASK_NAME, "/F")
    if rc == 0:
        return True, f"tarea '{TASK_NAME}' eliminada"
    if "no se encuentra" in out.lower() or "cannot find" in out.lower():
        return True, "no había ninguna tarea registrada"
    return False, out.strip() or f"schtasks devolvió {rc}"


def sync(enabled: bool) -> tuple[bool, str]:
    """Make the registry state match ``enabled``, doing nothing when already right."""
    if enabled:
        if is_enabled() and matches_current_install():
            return True, "inicio automático ya configurado"
        return enable()
    if not is_enabled():
        return True, "inicio automático desactivado"
    return disable()


def status_text() -> str:
    if not is_enabled():
        return "desactivado"
    if not matches_current_install():
        return "activado (apunta a otra instalación)"
    return "activado"


if __name__ == "__main__":  # pragma: no cover - manual helper
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    if action == "on":
        ok, message = enable()
    elif action == "off":
        ok, message = disable()
    else:
        print(status_text())
        print("target:", current_target())
        sys.exit(0)
    print(f"{'[OK]' if ok else '[ERROR]'} {message}")
    sys.exit(0 if ok else 1)
