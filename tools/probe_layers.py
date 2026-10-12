"""Sonda de capas de WinDivert: FLOW y SOCKET (necesita administrador).

Objetivo: averiguar si este equipo permite usar una capa que diga *directamente* a qué
proceso pertenece cada conexión. Hoy la atribución por aplicación usa la tabla de
conexiones (refresco de 1 s), así que un flujo recién abierto puede tardar hasta un
segundo en quedar bajo su límite. Si la capa FLOW funciona, eso desaparece.

Prueba cada combinación de capa y flags, y cuando consigue abrir un descriptor lee unos
segundos de eventos para ver qué campos traen (ProcessId, puerto local, protocolo).

Uso (pide UAC una vez):

    powershell -NoProfile -ExecutionPolicy Bypass -File tools\\run_elevated.ps1 ^
        -Script probe_layers.py -TimeoutSec 120
"""

from __future__ import annotations

import sys
import time
from contextlib import suppress
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pydivert import WinDivert
from pydivert.consts import Flag, Layer

from limitador import driver
from limitador.console import ensure_utf8_console

FLAG_COMBOS = (
    ("ninguno", Flag.DEFAULT),
    ("SNIFF", Flag.SNIFF),
    ("RECV_ONLY", Flag.RECV_ONLY),
    ("SNIFF|RECV_ONLY", Flag.SNIFF | Flag.RECV_ONLY),
)

FIELDS = ("ProcessId", "Protocol", "LocalPort", "RemotePort", "LocalAddr", "RemoteAddr")


def describe(packet) -> str:
    """Qué trae un evento: es lo que decide si sirve para atribuir tráfico a procesos."""
    parts = [f"event={getattr(packet, 'event', None)!r}"]
    for name in ("socket", "flow"):
        obj = getattr(packet, name, None)
        if obj is None:
            continue
        fields = [f"{f}={getattr(obj, f)}" for f in FIELDS if hasattr(obj, f)]
        parts.append(f"{name}({', '.join(fields)})")
    return " | ".join(parts)


def probe(layer: Layer, label: str, flags_name: str, flags) -> bool:
    try:
        handle = WinDivert("true", layer, 0, flags)
        handle.open()
    except Exception as exc:
        winerror = getattr(exc, "winerror", None)
        extra = f" (WinError {winerror})" if winerror else ""
        print(f"  {label:<7} flags={flags_name:<16} FALLA   {type(exc).__name__}: {exc}{extra}")
        return False

    print(f"  {label:<7} flags={flags_name:<16} ABIERTA")
    seen = 0
    deadline = time.monotonic() + 4.0
    try:
        while seen < 6 and time.monotonic() < deadline:
            try:
                packet = handle.recv(timeout=1.0)
            except Exception as exc:  # timeout o error de lectura
                if type(exc).__name__ not in ("TimeoutError", "OSError"):
                    print(f"      recv: {type(exc).__name__}: {exc}")
                continue
            if packet is None:
                continue
            print(f"      evento: {describe(packet)}")
            seen += 1
            # Los eventos de estas capas se devuelven para no bloquear la cola.
            with suppress(Exception):
                handle.send(packet)
        if seen == 0:
            print("      (sin eventos en 4 s: prueba a generar tráfico mientras corre)")
    finally:
        with suppress(Exception):
            handle.close()
    return True


def main() -> int:
    ensure_utf8_console()
    if not driver.is_admin():
        print(
            "Esta sonda necesita administrador. Ejecuta:\n"
            "  powershell -NoProfile -ExecutionPolicy Bypass -File tools\\run_elevated.ps1 "
            "-Script probe_layers.py -TimeoutSec 120",
            file=sys.stderr,
        )
        return 1

    driver.prepare()
    print("Sondeando capas de WinDivert en este equipo")
    print("(genera algo de tráfico mientras corre para ver eventos: p. ej. abre una web)\n")

    working = []
    for layer, label in ((Layer.FLOW, "FLOW"), (Layer.SOCKET, "SOCKET")):
        for flags_name, flags in FLAG_COMBOS:
            if probe(layer, label, flags_name, flags):
                working.append((label, flags_name))
        print()

    print("Resumen:")
    if working:
        for label, flags_name in working:
            print(f"  - capa {label} funciona con flags {flags_name}")
    else:
        print("  - ninguna capa de eventos se puede abrir: la atribución seguirá usando")
        print("    la tabla de conexiones (GetExtendedTcpTable/UdpTable)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
