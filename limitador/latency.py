"""Medición de latencia (RTT) con ICMP, sin privilegios de administrador.

Es la pieza que hace visible el motivo del proyecto: el *bufferbloat* no se ve en la velocidad,
se ve en los milisegundos. Mientras algo llena la cola del enlace (una descarga, una copia de
seguridad, el streaming), tu ping se dispara aunque la velocidad siga siendo alta. Aquí se mide
ese efecto con ICMP echo (``IcmpSendEcho`` de iphlpapi), que **no necesita administrador** a
diferencia de un socket crudo.

Para juegos, apunta ``host`` a la IP del servidor al que te conectas: es la única medida que
importa de verdad (a un servidor lejano el RTT es mayor, y lo que se persigue es que deje de
*subir* cuando hay tráfico de fondo).
"""

from __future__ import annotations

import ctypes
import itertools
import socket
import struct
import threading
import time
from collections import deque
from ctypes import wintypes
from dataclasses import dataclass, field

#: Payload del eco: suficiente para que no lo traten como un ping vacío.
PAYLOAD = b"limitador-latencia" * 4

#: Tamaño del búfer de respuesta: sizeof(ICMP_ECHO_REPLY) + payload + margen.
REPLY_BUFFER = 256

#: Desplazamientos dentro de ICMP_ECHO_REPLY (Address, Status, RoundTripTime son DWORD).
OFFSET_STATUS = 4
OFFSET_RTT = 8

_iphlpapi = ctypes.WinDLL("iphlpapi.dll")
_iphlpapi.IcmpCreateFile.restype = wintypes.HANDLE
_iphlpapi.IcmpSendEcho.restype = wintypes.DWORD
_iphlpapi.IcmpSendEcho.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.WORD,
    ctypes.c_void_p,
    ctypes.c_void_p,
    wintypes.DWORD,
    wintypes.DWORD,
]
_iphlpapi.IcmpCloseHandle.argtypes = [wintypes.HANDLE]

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

#: Muestras mínimas para atreverse a decir que el enlace es estable.
MIN_SAMPLES_FOR_STABILITY = 3


def resolve(host: str) -> int | None:
    """Dirección IPv4 de ``host`` en el formato que espera ``IcmpSendEcho``."""
    try:
        return struct.unpack("<I", socket.inet_aton(socket.gethostbyname(host)))[0]
    except OSError:
        return None


def ping(host: str, timeout_ms: int = 1000) -> float | None:
    """RTT en milisegundos, o ``None`` si no hubo respuesta.

    No necesita administrador: usa la API ICMP de Windows, no un socket crudo.
    """
    address = resolve(host)
    if address is None:
        return None
    handle = _iphlpapi.IcmpCreateFile()
    if not handle or handle == INVALID_HANDLE_VALUE:
        return None
    try:
        reply = ctypes.create_string_buffer(REPLY_BUFFER)
        sent = _iphlpapi.IcmpSendEcho(
            handle,
            address,
            PAYLOAD,
            len(PAYLOAD),
            None,
            reply,
            REPLY_BUFFER,
            int(timeout_ms),
        )
        if not sent:
            return None
        if struct.unpack_from("<I", reply.raw, OFFSET_STATUS)[0] != 0:
            return None
        return float(struct.unpack_from("<I", reply.raw, OFFSET_RTT)[0])
    finally:
        _iphlpapi.IcmpCloseHandle(handle)


@dataclass
class LatencyStats:
    """Resumen de una ventana de medidas. Todo en milisegundos."""

    host: str = ""
    samples: int = 0
    lost: int = 0
    last_ms: float | None = None
    avg_ms: float = 0.0
    min_ms: float = 0.0
    max_ms: float = 0.0
    jitter_ms: float = 0.0

    @property
    def loss_percent(self) -> float:
        total = self.samples + self.lost
        return (self.lost / total * 100.0) if total else 0.0

    @property
    def stable(self) -> bool:
        """Un enlace se siente estable si el pico no se dispara sobre la media."""
        return self.samples >= MIN_SAMPLES_FOR_STABILITY and self.max_ms <= max(
            self.avg_ms * 2.0, self.avg_ms + 30.0
        )

    def line(self) -> str:
        if not self.samples:
            return f"{self.host}: sin respuesta"
        base = (
            f"{self.host}: {self.last_ms:.0f} ms (medio {self.avg_ms:.0f}, "
            f"pico {self.max_ms:.0f}, jitter {self.jitter_ms:.0f}"
        )
        if self.loss_percent:
            base += f", pérdida {self.loss_percent:.0f}%"
        return base + ")"


@dataclass
class LatencyMonitor:
    """Mide RTT a un objetivo en un hilo, con ventana deslizante.

    Pensado para ir con el motor: si el ping sube cuando hay tráfico de fondo, el límite está
    haciendo su trabajo; si se queda plano, la línea no se estaba saturando.
    """

    host: str = "1.1.1.1"
    interval: float = 1.0
    window: int = 60
    timeout_ms: int = 1000
    _samples: deque[float | None] = field(default_factory=lambda: deque(maxlen=60), repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _thread: threading.Thread | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self._samples = deque(maxlen=self.window)

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="limitador-latencia", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.measure_once()
            self._stop.wait(self.interval)

    def measure_once(self) -> float | None:
        value = ping(self.host, self.timeout_ms)
        self._samples.append(value)
        return value

    # --------------------------------------------------------------------- stats
    def stats(self) -> LatencyStats:
        values = [s for s in list(self._samples) if s is not None]
        lost = sum(1 for s in list(self._samples) if s is None)
        if not values:
            return LatencyStats(host=self.host, lost=lost)
        diffs = [abs(b - a) for a, b in itertools.pairwise(values)]
        return LatencyStats(
            host=self.host,
            samples=len(values),
            lost=lost,
            last_ms=values[-1],
            avg_ms=sum(values) / len(values),
            min_ms=min(values),
            max_ms=max(values),
            jitter_ms=(sum(diffs) / len(diffs)) if diffs else 0.0,
        )

    def reset(self) -> None:
        self._samples.clear()


def measure(
    host: str, count: int = 10, interval: float = 0.2, timeout_ms: int = 1000
) -> LatencyStats:
    """Mide ``count`` veces sin hilo ni estado: útil para pruebas y para el modo consola."""
    monitor = LatencyMonitor(host=host, interval=interval, window=count, timeout_ms=timeout_ms)
    for _ in range(count):
        monitor.measure_once()
        time.sleep(interval)
    return monitor.stats()
