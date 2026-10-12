"""Capture engine: WinDivert handles wired to per-target shapers.

Design (v2):

* One capture thread per direction receives packets, decides whether the packet is
  subject to a limit at all, and either reinjects it immediately (zero added latency,
  the common case) or hands it to that direction's injector queue.
* One injector thread per direction owns the queue and the shapers. Because a single
  thread does all the pacing, packets of one flow keep their order and the rate is
  exact; the queue gives the shaper something to *delay* instead of dropping.
* ``port -> PID`` attribution comes from :mod:`limitador.procmap`: the local port of a
  packet tells us which application owns it, which is what turns a PC-wide limiter into
  a per-application one.
* Traffic that must not be touched (pure ACKs, DNS, ICMP, local networks) is identified
  in :mod:`limitador.classify` and never enters the queue.
* Stopping flushes the queue instead of discarding it: closing a WinDivert handle with
  packets still queued would drop them and cut live connections.

Fail-open rules (a limiter that cuts the connection is worse than no limiter)
--------------------------------------------------------------------------
While a handle is open, every packet that matches its filter is *held* by the driver
until we reinject it. So a crash in a capture thread while the handle stays open means
no internet at all. Therefore:

* the per-packet path cannot raise: parsing goes through ``classify.inspect_safe`` and
  the whole decision is wrapped, forwarding the packet untouched on any surprise;
* a supervisor thread watches the worker threads and, if any of them stops unexpectedly,
  **releases the capture** (closes the handles) so traffic flows normally again;
* the process only ever holds handles it knows about, so killing the process (or closing
  the handles) always restores the connection immediately;
* a self-test of the packet parser runs *before* the first handle is opened.
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from pydivert import WinDivert
from pydivert.consts import Flag, Layer

from . import classify, driver
from .classify import inspect_safe, should_pass_through
from .filters import (
    build_flow_filter,
    build_network_filter,
    build_socket_filter,
    validate_filter,
)
from .procmap import ProcessMap
from .rules import AppRule, Config
from .shaper import Shaper

log = logging.getLogger(__name__)

MB = 1024 * 1024
MIN_QUEUE_BYTES = 2 * MB
MAX_QUEUE_BYTES = 16 * MB
#: Cuántos paquetes saca el inyector de una vez (menos traspasos entre hilos = menos latencia).
MAX_INJECT_BURST = 64
MAX_CONSECUTIVE_RECV_ERRORS = 20
#: ERROR_ACCESS_DENIED de la API de Windows.
ERROR_ACCESS_DENIED = 5
#: Cuánto se cachea la regla de un PID antes de volver a resolverla.
RULE_CACHE_TTL_SECONDS = 5.0
#: Tope de entradas del caché de reglas por PID (válvula de seguridad).
MAX_CACHED_PIDS = 4096

#: Tipos de evento de las capas FLOW y SOCKET (``windivert.h``). Ojo: los valores 0-3 se
#: comparten entre capas pero **no significan lo mismo**; el 4 solo existe en la de sockets.
#: ``Packet.event`` entrega el número, no un nombre.
EVENT_FLOW_BOUND = 0
EVENT_FLOW_LISTEN = 1
EVENT_FLOW_ESTABLISHED = 2
EVENT_FLOW_DELETED = 3
EVENT_SOCKET_BIND = 0  # mismos números que las de flujo, distinto significado
EVENT_SOCKET_LISTEN = 1
EVENT_SOCKET_CONNECT = 2
EVENT_SOCKET_ACCEPT = 3
EVENT_SOCKET_CLOSE = 4

#: Qué código significa «este flujo se ha cerrado» en cada capa.
EVENT_CLOSED_CODES = {
    "flow": frozenset({EVENT_FLOW_DELETED}),
    "socket": frozenset({EVENT_SOCKET_CLOSE}),
}

#: Opción de diagnóstico: ``LIMITADOR_SPLIT_HANDLES=1`` abre un descriptor aparte para inyectar.
#: Por defecto va **desactivada**: se midió con `tools\ab_handles.py` que separar los descriptores
#: no mejora la velocidad (ambos modos dan lo mismo), así que se mantiene la arquitectura simple
#: y verificada. La opción queda para seguir investigando el techo de velocidad.
SPLIT_HANDLES_ENV = "LIMITADOR_SPLIT_HANDLES"

#: Capas de eventos de WinDivert, por orden de preferencia: la primera que abra, se usa.
#: FLOW es la ideal (entrega el ProcessId de cada flujo en cuanto se establece); SOCKET es
#: la alternativa clásica. Las capas de eventos no pueden devolver nada al driver, así que
#: hay que abrirlas en modo solo lectura: sin ``RECV_ONLY`` el driver responde error 87
#: (parámetro no válido). Comprobado con tools\probe_layers.py en Windows 11 25H2; las
#: variantes sin RECV_ONLY se dejan al final por si otra build las acepta.
EVENT_LAYER_CANDIDATES: tuple[tuple[str, Layer, Flag], ...] = (
    ("flow", Layer.FLOW, Flag.RECV_ONLY),
    ("flow", Layer.FLOW, Flag.SNIFF | Flag.RECV_ONLY),
    ("socket", Layer.SOCKET, Flag.RECV_ONLY),
    ("socket", Layer.SOCKET, Flag.SNIFF | Flag.RECV_ONLY),
    ("flow", Layer.FLOW, Flag.DEFAULT),
    ("socket", Layer.SOCKET, Flag.DEFAULT),
)


class LimiterError(RuntimeError):
    """Raised when the engine cannot start, with a message meant for the user."""


class CaptureReleased(Exception):
    """Internal: tells a worker thread that the handles were closed underneath it."""


class _ByteQueue:
    """FIFO bounded by *bytes* (not items), with a condition variable."""

    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        self._items: deque[tuple[object, int]] = deque()
        self._bytes = 0
        self._cv = threading.Condition()
        self._closed = False
        self.overflows = 0

    def set_max_bytes(self, max_bytes: int) -> None:
        with self._cv:
            self.max_bytes = max(int(max_bytes), 64 * 1024)

    def put(self, item: object, size: int) -> bool:
        with self._cv:
            if self._closed:
                return False
            if self._bytes + size > self.max_bytes:
                self.overflows += 1
                return False
            was_empty = not self._items
            self._items.append((item, size))
            self._bytes += size
            # Solo se despierta al inyector cuando la cola estaba vacía: avisar por paquete
            # costaba ~15.000 despertares por segundo con una descarga a 20 MB/s, y ese tira y
            # afloja del GIL es lo que retrasaba los paquetes de control (medido: +57 ms de ping
            # bajo carga). El inyector, una vez despierto, vacía todo lo que haya.
            if was_empty:
                self._cv.notify()
            return True

    def get_nowait(self) -> tuple[object, int] | None:
        """Saca un elemento sin esperar (``None`` si no hay nada)."""
        with self._cv:
            if not self._items:
                return None
            item, size = self._items.popleft()
            self._bytes -= size
            return item, size

    def get(self, timeout: float) -> tuple[object, int] | None:
        with self._cv:
            if not self._items:
                self._cv.wait(timeout)
            if not self._items:
                return None
            item, size = self._items.popleft()
            self._bytes -= size
            return item, size

    def drain(self) -> list[tuple[object, int]]:
        with self._cv:
            items = list(self._items)
            self._items.clear()
            self._bytes = 0
            return items

    def close(self) -> None:
        with self._cv:
            self._closed = True
            self._cv.notify_all()

    @property
    def bytes(self) -> int:
        with self._cv:
            return self._bytes

    @property
    def pending(self) -> int:
        with self._cv:
            return len(self._items)


@dataclass
class EngineEvent:
    level: str  # "info" | "warning" | "error"
    message: str


@dataclass
class Snapshot:
    """Everything the GUI/CLI needs, cheap to produce (no locks on the hot path)."""

    running: bool
    paused: bool
    bytes_down: int
    bytes_up: int
    forwarded_down: int
    forwarded_up: int
    dropped_down: int
    dropped_up: int
    dropped_packets: int
    queued_down: int
    queued_up: int
    queue_overflows: int
    released: bool = False
    per_pid: dict[int, tuple[int, int]] = field(default_factory=dict)
    shapers: dict[str, dict] = field(default_factory=dict)
    uptime: float = 0.0
    errors: list[str] = field(default_factory=list)
    #: Capa de eventos en uso ("" si ninguna): "flow", "socket" o "".
    event_layer: str = ""
    port_entries: int = 0


class Engine:
    """The limiter itself. Create, ``start()``, read ``snapshot()``, ``stop()``.

    ``handle_factory`` exists so the packet flow can be tested end to end with a fake
    handle (see ``tests/test_engine_flow.py``): the most dangerous bug in this program is
    a capture thread dying while packets are held, and that has to be testable without
    risking the machine's connection.
    """

    def __init__(
        self,
        config: Config,
        on_event: Callable[[EngineEvent], None] | None = None,
        handle_factory: Callable[..., object] | None = None,
    ) -> None:
        self.config = config
        self.on_event = on_event
        self.procmap = ProcessMap()
        self._handle_factory = handle_factory or WinDivert
        self._running = False
        self._paused = False
        self._starting = False
        self._stopping = False
        self._released = False
        self._started_at = 0.0
        self._errors: list[str] = []
        self._handles: list[object] = []
        #: Descriptores de solo escritura, uno por dirección, para inyectar (ver _open_handles).
        self._send_handles: dict[str, object] = {}
        self._capture_threads: dict[str, threading.Thread] = {}
        self._inject_threads: dict[str, threading.Thread] = {}
        self._event_thread: threading.Thread | None = None
        self._supervisor: threading.Thread | None = None
        self._queues: dict[str, _ByteQueue] = {}
        self._global_shapers: dict[str, Shaper] = {}
        self._app_shapers: dict[tuple[str, str], Shaper] = {}
        self._shape_lock = threading.RLock()  # reentrant: _sync_shapers -> _shaper_for
        self._pid_rule_cache: dict[int, tuple[float, AppRule | None]] = {}
        self._rule_env: tuple[str, str] = ("", "")
        self._bytes = {"in": 0, "out": 0}
        self._forwarded = {"in": 0, "out": 0}
        self._dropped = {"in": 0, "out": 0}
        self._dropped_packets = 0
        self._per_pid: dict[int, list[int]] = {}
        self._event_layer = ""
        self._timer_raised = False

    # ------------------------------------------------------------------ lifecycle
    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def capture_released(self) -> bool:
        """True when the engine gave up the capture to protect the connection."""
        return self._released

    def start(self) -> None:
        if self._running or self._starting:
            return
        self._starting = True
        try:
            if not driver.is_admin():
                raise LimiterError(
                    "Se necesitan privilegios de administrador para cargar el driver WinDivert.\n"
                    "Cierra la app y ábrela de nuevo aceptando el aviso de UAC."
                )
            ok, detail = classify.self_test()
            if not ok:
                raise LimiterError(
                    "Autocomprobación del análisis de paquetes fallida, no se captura nada:\n"
                    f"{detail}"
                )
            driver.prepare()
            self._validate_filters()
            self._open_handles()
            self._sync_shapers()
            self._queues = {
                "in": _ByteQueue(self._queue_budget()),
                "out": _ByteQueue(self._queue_budget()),
            }
            self._released = False
            self._raise_timer_resolution()
            self.procmap.start()
            # _running antes de arrancar el hilo de eventos: si su primera lectura fallara,
            # el bucle debe seguir vivo (solo sale cuando la parada es deliberada).
            self._running = True
            self._start_event_listener()
            self._paused = False
            self._started_at = time.monotonic()
            for direction in ("in", "out"):
                injector = threading.Thread(
                    target=self._inject_loop,
                    args=(direction,),
                    name=f"limitador-inject-{direction}",
                    daemon=True,
                )
                self._inject_threads[direction] = injector
                injector.start()
            for direction in ("in", "out"):
                capture = threading.Thread(
                    target=self._capture_loop,
                    args=(direction,),
                    name=f"limitador-capture-{direction}",
                    daemon=True,
                )
                self._capture_threads[direction] = capture
                capture.start()
            self._supervisor = threading.Thread(
                target=self._supervisor_loop, name="limitador-watchdog", daemon=True
            )
            self._supervisor.start()
            self._emit("info", "Limitador iniciado")
        except Exception:
            self._cleanup(silent=True)
            raise
        finally:
            self._starting = False

    def stop(self) -> None:
        """Stop cleanly: flush what is queued, then release the capture."""
        if self._stopping:
            return
        self._stopping = True
        try:
            if not self._running and not self._handles:
                return
            if not self._released:
                self._emit("info", "Deteniendo Limitador…")
            self._running = False
            for queue in self._queues.values():  # wake the injectors so they flush
                queue.close()
            for thread in self._inject_threads.values():
                thread.join(timeout=2.0)
            self._close_handles()  # unblocks recv() in the capture threads
            for thread in self._capture_threads.values():
                thread.join(timeout=2.0)
            self._cleanup(silent=False)
            if not self._released:
                self._emit("info", "Limitador detenido")
        finally:
            self._stopping = False

    def emergency_release(self, reason: str) -> None:
        """Give the capture back right now: the connection always wins over limiting."""
        if not self._handles:
            return
        self._released = True
        self._running = False
        self._close_handles()
        for queue in self._queues.values():
            queue.close()
        self._emit(
            "error",
            f"CAPTURA LIBERADA: {reason}. El tráfico de red vuelve a fluir sin límites; "
            "para volver a limitar, detén el limitador y vuelve a iniciarlo.",
        )

    def _cleanup(self, *, silent: bool) -> None:
        self._close_handles()
        self._capture_threads.clear()
        self._inject_threads.clear()
        if self._event_thread is not None:
            self._event_thread.join(timeout=1.0)
            self._event_thread = None
        with contextlib.suppress(Exception):  # pragma: no cover
            self.procmap.stop()
        with self._shape_lock:
            self._global_shapers.clear()
            self._app_shapers.clear()
        self._restore_timer_resolution()
        self._running = False
        self._paused = False
        if not silent:
            self._queues.clear()

    def _raise_timer_resolution(self) -> None:
        """Ask Windows for a 1 ms timer while limiting.

        Without this, ``time.sleep`` rounds up to the default ~15 ms tick on some systems,
        which would make any limit above a few MB/s unreachable.
        """
        if self._timer_raised:
            return
        with contextlib.suppress(Exception):  # pragma: no cover - sin winmm
            ctypes.windll.winmm.timeBeginPeriod(1)
            self._timer_raised = True

    def _restore_timer_resolution(self) -> None:
        if not self._timer_raised:
            return
        with contextlib.suppress(Exception):  # pragma: no cover
            ctypes.windll.winmm.timeEndPeriod(1)
        self._timer_raised = False

    def pause(self) -> None:
        """Keep capturing (so monitoring continues) but let everything through."""
        if self._running:
            self._paused = True
            self._emit("info", "Limitación en pausa (sin límites aplicados)")

    def resume(self) -> None:
        if self._running and self._paused:
            self._paused = False
            self._emit("info", "Limitación reanudada")

    def toggle_pause(self) -> bool:
        if self._paused:
            self.resume()
        else:
            self.pause()
        return self._paused

    # ------------------------------------------------------------------- handles
    def _validate_filters(self) -> None:
        for direction in ("in", "out"):
            real = "inbound" if direction == "in" else "outbound"
            filt = build_network_filter(real, exclude_lan=self.config.settings.exclude_lan)
            ok, message = validate_filter(filt)
            if not ok:
                raise LimiterError(f"El filtro de captura no compila: {message}")

    def _open_handles(self) -> None:
        settings = self.config.settings
        split_handles = os.environ.get(SPLIT_HANDLES_ENV) == "1"
        try:
            for direction, real in (("out", "outbound"), ("in", "inbound")):
                filt = build_network_filter(real, exclude_lan=settings.exclude_lan)
                send_handle = None
                if split_handles:
                    # Modo de diagnóstico: descriptor propio (solo escritura) para inyectar y uno
                    # de solo lectura para capturar. El filtro de captura excluye lo que inyectamos
                    # (marca *impostor*) para no recapturarlo en bucle.
                    filt = build_network_filter(
                        real, exclude_lan=settings.exclude_lan, exclude_own=True
                    )
                    try:
                        candidate = self._handle_factory(filt, Layer.NETWORK, 0, Flag.SEND_ONLY)
                        candidate.open()
                        send_handle = candidate
                    except Exception as exc:
                        log.warning("sin descriptor de inyección en %s (%s)", direction, exc)
                flags = Flag.DEFAULT if send_handle is None else Flag.RECV_ONLY
                handle = self._handle_factory(filt, Layer.NETWORK, 0, flags)
                handle.open()
                self._handles.append(handle)
                self._send_handles[direction] = send_handle or handle
        except OSError as exc:
            self._close_handles()
            winerror = getattr(exc, "winerror", None)
            if winerror == ERROR_ACCESS_DENIED:
                raise LimiterError(
                    "Windows rechazó el acceso al driver (error 5). Ábrelo como administrador."
                ) from exc
            if winerror in (1058, 1060, 1063, 2):
                raise LimiterError(
                    f"El driver WinDivert no está disponible (error {winerror}).\n\n"
                    "Pulsa 'Reparar driver' en la pestaña Avanzado, o ejecuta el diagnóstico."
                ) from exc
            raise LimiterError(f"No se pudo abrir la captura de paquetes: {exc}") from exc

    def _close_handles(self) -> None:
        for _direction, handle in list(self._send_handles.items()):
            if handle not in self._handles:  # no cerrar dos veces el mismo descriptor
                with contextlib.suppress(Exception):
                    handle.close()
        self._send_handles.clear()
        while self._handles:
            handle = self._handles.pop()
            with contextlib.suppress(Exception):
                handle.close()

    # -------------------------------------------------------------------- shaping
    def _rate_for(self, direction: str, rule: AppRule | None) -> float:
        if rule is not None:
            return rule.down_bps if direction == "in" else rule.up_bps
        settings = self.config.settings
        return settings.global_down_bps if direction == "in" else settings.global_up_bps

    def _shaper_for(self, direction: str, key: str | None, rate: float) -> Shaper:
        """Get or create the shaper for a target (``None`` key = global)."""
        with self._shape_lock:
            if key is None:
                table = self._global_shapers
                index: object = direction
            else:
                table = self._app_shapers
                index = (key, direction)
            shaper = table.get(index)
            if shaper is None:
                shaper = Shaper(rate, self.config.settings.effective_max_delay_ms)
                table[index] = shaper
            else:
                shaper.set_rate(rate)
                shaper.set_max_delay(self.config.settings.effective_max_delay_ms)
            return shaper

    def _sync_shapers(self) -> None:
        """Bring shapers in line with the current configuration (live updates)."""
        settings = self.config.settings
        with self._shape_lock:
            if settings.has_global_limit and not self._global_shapers:
                for direction, rate in (
                    ("in", settings.global_down_bps),
                    ("out", settings.global_up_bps),
                ):
                    if rate > 0:
                        self._global_shapers[direction] = Shaper(
                            rate, settings.effective_max_delay_ms
                        )
            for direction in ("in", "out"):
                shaper = self._global_shapers.get(direction)
                rate = self._rate_for(direction, None)
                if shaper and rate <= 0:
                    self._global_shapers.pop(direction, None)
                elif shaper and rate > 0:
                    shaper.set_rate(rate)
                    shaper.set_max_delay(settings.effective_max_delay_ms)
            active_keys = set()
            for rule in self.config.active_rules():
                for direction in ("in", "out"):
                    rate = self._rate_for(direction, rule)
                    if rate <= 0:
                        continue
                    key = self.config.normalise_exe(rule.exe)
                    active_keys.add((key, direction))
                    self._shaper_for(direction, key, rate)
            for key in list(self._app_shapers):
                if key not in active_keys:
                    self._app_shapers.pop(key, None)
        for queue in self._queues.values():
            queue.set_max_bytes(self._queue_budget())

    def _active_rate_sum(self) -> float:
        settings = self.config.settings
        total = max(settings.global_down_bps, settings.global_up_bps)
        for rule in self.config.active_rules():
            total += max(rule.down_bps, rule.up_bps)
        return total

    def _queue_budget(self, rate_sum: float | None = None) -> int:
        if rate_sum is None:
            rate_sum = self._active_rate_sum()
        budget = rate_sum * (self.config.settings.max_delay_ms / 1000.0) * 3
        return int(min(max(budget, MIN_QUEUE_BYTES), MAX_QUEUE_BYTES))

    def apply_config(self) -> None:
        """Called from the GUI after any setting changes."""
        self.config.settings.clamp()
        self._sync_shapers()

    # ------------------------------------------------------------------ rule lookup
    def _rule_signature(self) -> tuple[str, str]:
        return (
            "|".join(
                sorted(f"{r.exe}:{r.down_bps}:{r.up_bps}:{r.enabled}" for r in self.config.rules)
            ),
            f"{self.config.settings.global_down_bps}:{self.config.settings.global_up_bps}",
        )

    def _rule_for_pid(self, pid: int) -> AppRule | None:
        now = time.monotonic()
        signature = self._rule_signature()
        if signature != self._rule_env:
            self._rule_env = signature
            self._pid_rule_cache.clear()
        cached = self._pid_rule_cache.get(pid)
        if cached and now - cached[0] < RULE_CACHE_TTL_SECONDS:
            return cached[1]
        rule = self.config.rule_for_exe(self.procmap.path_for(pid))
        if rule is not None and not rule.has_limit and not rule.is_priority:
            # Sin tasas y sin prioridad: la regla existe pero no decide nada.
            rule = None
        self._pid_rule_cache[pid] = (now, rule)
        if len(self._pid_rule_cache) > MAX_CACHED_PIDS:
            self._pid_rule_cache.clear()
        return rule

    # --------------------------------------------------------------------- threads
    def _capture_loop(self, direction: str) -> None:
        """Receive, decide, reinject. Never dies holding a packet."""
        try:
            capture = self._handles[0 if direction == "out" else 1]
            handle = self._send_handles.get(direction) or capture
        except IndexError:  # pragma: no cover - defensive
            return
        outbound = direction == "out"
        consecutive_errors = 0
        while self._running:
            try:
                packet = capture.recv()
                consecutive_errors = 0
            except Exception as exc:
                if not self._running:
                    break
                consecutive_errors += 1
                self._note_error(f"captura {direction}: {exc}")
                if consecutive_errors >= MAX_CONSECUTIVE_RECV_ERRORS:
                    self.emergency_release(f"la captura {direction} no puede leer paquetes")
                    break
                time.sleep(0.05)
                continue
            # Every exit path below must end in a reinjection or a deliberate drop.
            try:
                self._process_packet(direction, outbound, handle, packet)
            except Exception as exc:
                self._note_error(f"error procesando un paquete en {direction}: {exc!r}")
                self._send(handle, packet)

    def _process_packet(self, direction: str, outbound: bool, handle, packet) -> None:
        settings = self.config.settings
        info = inspect_safe(packet.raw, outbound)
        if info is None:
            # Not something we can reason about: forward it untouched.
            self._forwarded[direction] += len(packet.raw)
            self._send(handle, packet)
            return

        size = info.size
        self._bytes[direction] += size
        pid = None
        if info.local_port > 0:
            pid = self.procmap.pid_for(info.proto, info.local_port)
            if pid:
                entry = self._per_pid.setdefault(pid, [0, 0])
                entry[0 if outbound else 1] += size

        if self._paused or should_pass_through(
            info,
            exempt_control=settings.exempt_control,
            limit_ipv6=settings.limit_ipv6,
        ):
            self._forwarded[direction] += size
            self._send(handle, packet)
            return

        shaper = self._select_shaper(direction, pid)
        if shaper is None:
            self._forwarded[direction] += size
            self._send(handle, packet)
            return

        # The queue stores (payload, size): the injector needs the size before it can pace.
        if not self._queues[direction].put((packet, shaper, info), size):
            self._dropped[direction] += size
            self._dropped_packets += 1

    def _select_shaper(self, direction: str, pid: int | None) -> Shaper | None:
        rule = self._rule_for_pid(pid) if pid else None
        if rule is not None and rule.is_priority:
            # Prioridad absoluta (el juego): su tráfico sale ya, sin cola y sin límite global.
            # Es lo que mantiene el ping bajo mientras otra aplicación llena el enlace.
            return None
        rate = self._rate_for(direction, rule)
        if rate <= 0:
            # No per-app limit: fall back to the PC-wide limit, if any.
            rate = self._rate_for(direction, None)
            rule = None
            if rate <= 0:
                return None
        key = self.config.normalise_exe(rule.exe) if rule else None
        return self._shaper_for(direction, key, rate)

    def _inject_loop(self, direction: str) -> None:
        try:
            handle = (
                self._send_handles.get(direction) or self._handles[0 if direction == "out" else 1]
            )
        except IndexError:  # pragma: no cover - defensive
            return
        queue = self._queues[direction]
        while True:
            item = queue.get(0.2)
            if item is None:
                if not self._running:
                    break
                continue
            # Se vacía en ráfaga lo que haya: despertar al hilo una vez por paquete era lo que
            # añadía latencia a los paquetes de control (que comparten CPU con este bucle).
            batch = [item]
            while len(batch) < MAX_INJECT_BURST:
                more = queue.get_nowait()
                if more is None:
                    break
                batch.append(more)
            # 'pending' son los bytes que van en esta ráfaga y aún no se han enviado: cuentan
            # como cola para el AQM, igual que contaban cuando se sacaba de uno en uno.
            pending = sum(item_size for _payload, item_size in batch)
            for payload, size in batch:
                packet, shaper, _info = payload
                try:
                    if self._paused or not self._running or self._released:
                        # Flush: never hold packets hostage while shutting down or paused.
                        pending -= size
                        self._send(handle, packet)
                        continue
                    allow, wait = shaper.plan(size, queue.bytes + pending)
                    pending -= size
                    if not allow:
                        shaper.note_dropped(size)
                        self._dropped[direction] += size
                        self._dropped_packets += 1
                        continue
                    if wait > 0:
                        shaper.wait(wait)
                    self._send(handle, packet)
                    shaper.note_sent(size, wait)
                except Exception as exc:
                    self._note_error(f"error inyectando en {direction}: {exc!r}")
                    self._send(handle, packet)

    def _send(self, handle, packet) -> None:
        """Reinject a packet, telling pydivert not to recompute its checksums.

        ``pydivert``'s ``send()`` recalculates IP+TCP/UDP checksums by default, and on
        Windows that path is implemented in *pure Python* (its native fast path is only
        used on other platforms) - roughly 60-150 us per packet, which capped the whole
        pipeline at ~9 MB/s regardless of the configured limit. We never modify packets, so
        their checksums are already valid and recomputing them is pure waste.
        """
        try:
            handle.send(packet, recalculate_checksum=False)
        except TypeError:  # pragma: no cover - a handle without that keyword
            try:
                handle.send(packet)
            except Exception as exc:
                if self._running:
                    self._note_error(f"reinyección: {exc}")
        except Exception as exc:
            if self._running:
                self._note_error(f"reinyección: {exc}")

    def _supervisor_loop(self) -> None:
        """Watchdog: if a worker stops unexpectedly, give the capture back."""
        while self._running:
            time.sleep(0.5)
            if not self._running:
                break
            dead = [
                f"captura {name}"
                for name, thread in self._capture_threads.items()
                if not thread.is_alive()
            ]
            dead += [
                f"inyección {name}"
                for name, thread in self._inject_threads.items()
                if not thread.is_alive()
            ]
            if dead:
                self.emergency_release("se han detenido hilos del motor (" + ", ".join(dead) + ")")
                break

    def _start_event_listener(self) -> None:
        """Atribución exacta puerto -> proceso usando las capas de eventos de WinDivert.

        Prueba FLOW primero (la capa pensada para esto: entrega el ``ProcessId`` de cada
        flujo en cuanto se establece) y luego SOCKET. Si ninguna abre, la atribución por
        aplicación sigue funcionando con la tabla de conexiones, que se refresca cada
        segundo: esto solo elimina el retardo en flujos recién abiertos.
        """
        for name, layer, flags in EVENT_LAYER_CANDIDATES:
            filter_string = build_flow_filter() if name == "flow" else build_socket_filter()
            try:
                handle = self._handle_factory(filter_string, layer, 0, flags)
                handle.open()
            except Exception as exc:
                log.debug("capa de eventos %s (flags=%s) no disponible: %s", name, flags, exc)
                continue
            self._handles.append(handle)
            self._event_layer = name
            self._emit("info", f"atribución por proceso: capa {name.upper()} (instantánea)")
            self._event_thread = threading.Thread(
                target=self._event_loop,
                args=(handle, name),
                name=f"limitador-{name}",
                daemon=True,
            )
            self._event_thread.start()
            return

        self._event_layer = ""
        self._emit(
            "warning",
            "ninguna capa de eventos de WinDivert se pudo abrir: se atribuye el tráfico con "
            "la tabla de conexiones (hasta 1 s de retardo en flujos nuevos)",
        )

    def _event_loop(self, handle: object, field: str) -> None:
        """Lee eventos de flujo/socket y alimenta el mapa de procesos. Nunca lanza."""
        while True:
            try:
                packet = handle.recv()
            except Exception:
                # El descriptor se cerró (parada normal) o la capa falló: en ambos casos
                # la tabla de conexiones sigue atribuyendo, así que salir es seguro.
                if self._stopping or not self._running:
                    break
                time.sleep(0.2)
                continue
            try:
                endpoint = getattr(packet, field, None)
                if endpoint is not None:
                    proto = int(getattr(endpoint, "Protocol", 0))
                    port = int(getattr(endpoint, "LocalPort", 0))
                    pid = int(getattr(endpoint, "ProcessId", 0))
                    code = int(getattr(packet, "event", -1))
                    if code in EVENT_CLOSED_CODES[field]:
                        self.procmap.forget_endpoint(proto, port)
                    else:
                        self.procmap.note_endpoint_event(proto, port, pid)
            except Exception:
                # Un evento raro no puede matar el hilo: la tabla de conexiones sigue ahí.
                pass
            # No se devuelve nada: el descriptor es de solo lectura y los eventos de estas
            # capas son informativos (no hay paquete que reinyectar).

    # -------------------------------------------------------------------- reporting
    def _emit(self, level: str, message: str) -> None:
        log.log(
            {"info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}.get(
                level, logging.INFO
            ),
            message,
        )
        if self.on_event:
            with contextlib.suppress(Exception):  # pragma: no cover
                self.on_event(EngineEvent(level, message))

    def _note_error(self, message: str) -> None:
        self._errors.append(message)
        del self._errors[:-20]
        self._emit("error", message)

    def snapshot(self) -> Snapshot:
        with self._shape_lock:
            shapers = {
                (f"app:{key}:{direction}" if key else f"global:{direction}"): shaper.snapshot()
                for (key, direction), shaper in self._app_shapers.items()
            }
            shapers.update(
                {
                    f"global:{direction}": shaper.snapshot()
                    for direction, shaper in self._global_shapers.items()
                }
            )
        return Snapshot(
            running=self._running,
            paused=self._paused,
            bytes_down=self._bytes["in"],
            bytes_up=self._bytes["out"],
            forwarded_down=self._forwarded["in"],
            forwarded_up=self._forwarded["out"],
            dropped_down=self._dropped["in"],
            dropped_up=self._dropped["out"],
            dropped_packets=self._dropped_packets,
            queued_down=self._queues["in"].bytes if "in" in self._queues else 0,
            queued_up=self._queues["out"].bytes if "out" in self._queues else 0,
            queue_overflows=sum(q.overflows for q in self._queues.values()),
            released=self._released,
            per_pid={pid: (counts[0], counts[1]) for pid, counts in self._per_pid.items()},
            shapers=shapers,
            uptime=(time.monotonic() - self._started_at) if self._running else 0.0,
            errors=list(self._errors),
            event_layer=self._event_layer,
            port_entries=self.procmap.size,
        )

    def reset_counters(self) -> None:
        self._bytes = {"in": 0, "out": 0}
        self._forwarded = {"in": 0, "out": 0}
        self._dropped = {"in": 0, "out": 0}
        self._dropped_packets = 0
        self._per_pid.clear()


def engine_available() -> tuple[bool, str]:
    """Can the engine start right now? Used by the preflight/diagnostics."""
    if not driver.is_admin():
        return False, "sin privilegios de administrador"
    ok, detail = classify.self_test()
    if not ok:
        return False, detail
    return driver.install()
