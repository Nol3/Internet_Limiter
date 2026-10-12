"""port -> process identification.

Two sources are merged, because neither is complete on its own:

* **Layer events** (WinDivert): the ``FLOW`` layer reports ``BOUND``/``LISTEN``/
  ``ESTABLISHED``/``DELETED`` events carrying ``ProcessId``, ``LocalPort`` and
  ``Protocol``; the older ``SOCKET`` layer reports the equivalent ``BIND``/``LISTEN``/
  ``CONNECT``/``ACCEPT``/``CLOSE`` events. Exact and instant, but only for connections
  created (or closed) after we start.
* **``GetExtendedTcpTable`` / ``GetExtendedUdpTable``** (iphlpapi) enumerate every
  open endpoint with its owning PID. Catches connections that already existed and
  survives missed events, so it is polled once per second as the source of truth.

Keys are ``(protocol, local_port)``; values are PIDs. Local ports are per-protocol, so
TCP and UDP need separate keys.
"""

from __future__ import annotations

import ctypes
import logging
import socket
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

AF_INET = 2
AF_INET6 = 23
TCP_TABLE_OWNER_PID_ALL = 5
UDP_TABLE_OWNER_PID = 1
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_iphlpapi = ctypes.WinDLL("iphlpapi.dll")
_kernel32 = ctypes.WinDLL("kernel32.dll")


class MIB_TCPROW_OWNER_PID(ctypes.Structure):
    _fields_ = [
        ("dwState", wintypes.DWORD),
        ("dwLocalAddr", wintypes.DWORD),
        ("dwLocalPort", wintypes.DWORD),
        ("dwRemoteAddr", wintypes.DWORD),
        ("dwRemotePort", wintypes.DWORD),
        ("dwOwningPid", wintypes.DWORD),
    ]


class MIB_TCP6ROW_OWNER_PID(ctypes.Structure):
    _fields_ = [
        ("ucLocalAddr", ctypes.c_ubyte * 16),
        ("dwLocalScopeId", wintypes.DWORD),
        ("dwLocalPort", wintypes.DWORD),
        ("ucRemoteAddr", ctypes.c_ubyte * 16),
        ("dwRemoteScopeId", wintypes.DWORD),
        ("dwRemotePort", wintypes.DWORD),
        ("dwState", wintypes.DWORD),
        ("dwOwningPid", wintypes.DWORD),
    ]


class MIB_UDPROW_OWNER_PID(ctypes.Structure):
    _fields_ = [
        ("dwLocalAddr", wintypes.DWORD),
        ("dwLocalPort", wintypes.DWORD),
        ("dwOwningPid", wintypes.DWORD),
    ]


class MIB_UDP6ROW_OWNER_PID(ctypes.Structure):
    _fields_ = [
        ("ucLocalAddr", ctypes.c_ubyte * 16),
        ("dwLocalScopeId", wintypes.DWORD),
        ("dwLocalPort", wintypes.DWORD),
        ("dwOwningPid", wintypes.DWORD),
    ]


def _query_table(func, row_type, family: int, table_class: int):
    size = wintypes.DWORD(0)
    func(None, ctypes.byref(size), False, family, table_class, 0)
    if size.value == 0:
        return []
    buf = ctypes.create_string_buffer(size.value)
    ret = func(buf, ctypes.byref(size), False, family, table_class, 0)
    if ret != 0:
        raise OSError(ret, f"tabla de conexiones: error {ret}")
    count = ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD)).contents.value
    if count == 0:
        return []
    array = (row_type * count).from_buffer(buf, ctypes.sizeof(wintypes.DWORD))
    return list(array)


def _port(value: int) -> int:
    return socket.ntohs(value & 0xFFFF)


def enum_endpoints() -> dict[tuple[int, int], int]:
    """``{(protocol, local_port): pid}`` for every open TCP/UDP endpoint."""
    result: dict[tuple[int, int], int] = {}

    def add_tcp(rows):
        for row in rows:
            result[(6, _port(row.dwLocalPort))] = row.dwOwningPid

    def add_udp(rows):
        for row in rows:
            result[(17, _port(row.dwLocalPort))] = row.dwOwningPid

    try:
        add_tcp(
            _query_table(
                _iphlpapi.GetExtendedTcpTable,
                MIB_TCPROW_OWNER_PID,
                AF_INET,
                TCP_TABLE_OWNER_PID_ALL,
            )
        )
    except OSError as exc:  # pragma: no cover - defensive
        log.warning("TCP table (v4) failed: %s", exc)
    try:
        add_tcp(
            _query_table(
                _iphlpapi.GetExtendedTcpTable,
                MIB_TCP6ROW_OWNER_PID,
                AF_INET6,
                TCP_TABLE_OWNER_PID_ALL,
            )
        )
    except OSError as exc:  # pragma: no cover
        log.warning("TCP table (v6) failed: %s", exc)
    try:
        add_udp(
            _query_table(
                _iphlpapi.GetExtendedUdpTable, MIB_UDPROW_OWNER_PID, AF_INET, UDP_TABLE_OWNER_PID
            )
        )
    except OSError as exc:  # pragma: no cover
        log.warning("UDP table (v4) failed: %s", exc)
    try:
        add_udp(
            _query_table(
                _iphlpapi.GetExtendedUdpTable, MIB_UDP6ROW_OWNER_PID, AF_INET6, UDP_TABLE_OWNER_PID
            )
        )
    except OSError as exc:  # pragma: no cover
        log.warning("UDP table (v6) failed: %s", exc)
    return result


def process_path(pid: int) -> str | None:
    """Full image path of a PID (``None`` if it died or is protected)."""
    if pid <= 0:
        return None
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        if _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return None
    finally:
        _kernel32.CloseHandle(handle)


@dataclass
class _PidInfo:
    path: str | None
    name: str


class ProcessMap:
    """Live map of protocol+port -> PID, with names, refreshed in the background."""

    def __init__(self, poll_interval: float = 1.0, name_cache_ttl: float = 30.0) -> None:
        self.poll_interval = poll_interval
        self._lock = threading.Lock()
        self._ports: dict[tuple[int, int], int] = {}
        self._names: dict[int, _PidInfo] = {}
        self._name_time: dict[int, float] = {}
        self._event_ports: dict[tuple[int, int], int] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.name_cache_ttl = name_cache_ttl
        self.last_error: str | None = None

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self.refresh()
        self._thread = threading.Thread(target=self._loop, name="procmap", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_interval):
            self.refresh()

    def refresh(self) -> None:
        try:
            ports = enum_endpoints()
        except Exception as exc:  # pragma: no cover - defensive
            self.last_error = str(exc)
            return
        with self._lock:
            # Socket-layer events win for ports the poll does not know yet.
            self._ports = ports
            self._event_ports = {k: v for k, v in self._event_ports.items() if k not in ports}
            self.last_error = None

    # -------------------------------------------------------------- layer events
    def note_endpoint_event(self, proto: int, local_port: int, pid: int) -> None:
        """Feed one WinDivert flow/socket-layer event into the map.

        Solo rellena huecos: en cuanto la tabla de conexiones conoce el puerto, manda ella
        (es la fuente de verdad y ademas limpia lo que ya no existe).
        """
        if proto not in (6, 17) or local_port <= 0 or pid <= 0:
            return
        with self._lock:
            if (proto, local_port) not in self._ports:
                self._event_ports[(proto, local_port)] = pid

    def forget_endpoint(self, proto: int, local_port: int) -> None:
        """A flow closed: drop it so a reused port is not attributed to the old process."""
        if local_port <= 0:
            return
        with self._lock:
            self._event_ports.pop((proto, local_port), None)

    # -------------------------------------------------------------------- lookups
    def pid_for(self, proto: int, local_port: int) -> int | None:
        if local_port <= 0:
            return None
        key = (proto, local_port)
        with self._lock:
            pid = self._ports.get(key)
            if pid is None:
                pid = self._event_ports.get(key)
        return pid

    def ports_for_pid(self, pid: int) -> set[int]:
        """Puertos locales que la tabla atribuye a ese proceso.

        Se usa para decidir qué tráfico capturar: los puertos de una aplicación con prioridad
        absoluta se dejan fuera del filtro (su tráfico no entra en el motor), y con el alcance
        "limited" solo se capturan los de las aplicaciones que sí se limitan.
        """
        with self._lock:
            ports = {port for (_proto, port), owner in self._ports.items() if owner == pid}
            ports.update(
                port for (_proto, port), owner in self._event_ports.items() if owner == pid
            )
        return ports

    def path_for(self, pid: int) -> str | None:
        return self._info(pid).path

    def name_for(self, pid: int) -> str:
        return self._info(pid).name

    def _info(self, pid: int) -> _PidInfo:
        now = time.monotonic()
        with self._lock:
            cached = self._names.get(pid)
            fresh = now - self._name_time.get(pid, 0.0) < self.name_cache_ttl
        if cached and fresh:
            return cached
        path = process_path(pid)
        name = path.rsplit("\\", 1)[-1] if path else f"pid {pid}"
        info = _PidInfo(path, name)
        with self._lock:
            self._names[pid] = info
            self._name_time[pid] = now
        return info

    def port_count(self, pid: int) -> int:
        with self._lock:
            return sum(
                1
                for owner in list(self._ports.values()) + list(self._event_ports.values())
                if owner == pid
            )

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._ports) + len(self._event_ports)

    def pids(self) -> list[int]:
        with self._lock:
            return sorted(set(self._ports.values()) | set(self._event_ports.values()))


@dataclass
class AppTraffic:
    """Snapshot row for the applications list."""

    pid: int
    name: str
    path: str | None
    down_bps: float
    up_bps: float
    ports: int = 0
    limited: bool = False
    extra: dict = field(default_factory=dict)
