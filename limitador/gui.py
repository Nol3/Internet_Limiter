"""Tkinter interface: global limits, per-application limits, live monitoring.

Threading rules followed here:

* the engine lives in background threads; the GUI never blocks on it;
* engine callbacks land in a ``queue.Queue`` that the Tk loop drains, because Tk is not
  thread safe;
* every rate is computed in the UI thread from two snapshots of monotonically
  increasing byte counters, which keeps the packet path free of locks.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
import tkinter as tk
from collections import deque
from pathlib import Path
from tkinter import messagebox, ttk

from . import autostart, driver
from .engine import Engine, EngineEvent, LimiterError, Snapshot
from .procmap import ProcessMap
from .rules import APP_VERSION, AppRule, Config, log_path
from .units import MB, UNITS, format_bytes, format_duration, format_rate, parse_rate, to_display

BG = "#1e1e1e"
BG_ALT = "#252526"
FG = "#eaeaea"
ACCENT = "#4ec9b0"
ACCENT_WARM = "#ce9178"
MUTED = "#9d9d9d"


class _LogHandler(logging.Handler):
    """Feeds the engine's log records into the GUI log tab."""

    def __init__(self, sink: queue.Queue[str]) -> None:
        super().__init__()
        self.sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.sink.put_nowait(f"{time.strftime('%H:%M:%S')} {record.levelname:<7} {record.getMessage()}")
        except queue.Full:  # pragma: no cover
            pass


class LimitadorApp(tk.Tk):
    def __init__(self, config: Config, start_minimized: bool = False) -> None:
        super().__init__()
        self.config_data = config
        self.events: queue.Queue[EngineEvent] = queue.Queue()
        self.log_lines: queue.Queue[str] = queue.Queue(maxsize=500)
        self.engine = Engine(self.config_data, on_event=self.events.put)
        self.pmap = ProcessMap(poll_interval=2.0)
        self._snapshot: Snapshot | None = None
        self._previous: Snapshot | None = None
        self._previous_time = time.monotonic()
        self._history = {"down": deque(maxlen=60), "up": deque(maxlen=60)}
        self._app_rows: dict[str, int | None] = {}
        self._rate_cache: dict[int, tuple[float, float]] = {}
        self._selected_pid: int | None = None
        self._busy = False
        self._closing = False
        self._tray = None

        self.title(f"Limitador {APP_VERSION}")
        self.geometry("620x560")
        self.minsize(560, 500)
        self.configure(bg=BG)
        self._set_icon()
        self._setup_style()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        logging.getLogger("limitador").addHandler(_LogHandler(self.log_lines))
        self.pmap.refresh()
        self._sync_widgets_from_config()
        self._start_tray()
        self.after(250, self._drain_events)
        self.after(1000, self._refresh_loop)
        if self.config_data.settings.start_on_launch:
            self.after(400, self._start)
        if start_minimized:
            self.after(200, self._hide_to_tray)

    # --------------------------------------------------------------------- chrome
    def _set_icon(self) -> None:
        for candidate in (Path(__file__).resolve().parent.parent / "Limitador.ico",
                          Path(getattr(__import__("sys"), "_MEIPASS", "")) / "Limitador.ico"):
            if candidate and candidate.exists():
                try:
                    self.iconbitmap(str(candidate))
                    return
                except Exception:
                    continue

    def _setup_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=FG, font=("Segoe UI", 9))
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=BG_ALT, relief="flat")
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Card.TLabel", background=BG_ALT, foreground=FG)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 8))
        style.configure("Title.TLabel", background=BG, foreground=FG, font=("Segoe UI", 15, "bold"))
        style.configure("Status.TLabel", background=BG, foreground=ACCENT, font=("Segoe UI", 10, "bold"))
        style.configure("Rate.TLabel", background=BG, foreground=FG, font=("Consolas", 11))
        style.configure("TEntry", fieldbackground="#2d2d2d", foreground=FG, insertcolor=FG)
        style.configure("TCombobox", fieldbackground="#2d2d2d", background="#2d2d2d", foreground=FG)
        style.configure("TButton", font=("Segoe UI", 9, "bold"), padding=5)
        style.configure("TCheckbutton", background=BG, foreground=FG)
        style.map("TCheckbutton", background=[("active", BG)], foreground=[("active", FG)])
        style.configure("Treeview", background=BG_ALT, fieldbackground=BG_ALT, foreground=FG,
                        rowheight=21, borderwidth=0)
        style.configure("Treeview.Heading", background="#333", foreground=FG,
                        font=("Segoe UI", 8, "bold"))
        style.map("Treeview", background=[("selected", "#0e639c")])
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=BG_ALT, foreground=MUTED, padding=(12, 5))
        style.map("TNotebook.Tab", background=[("selected", BG)], foreground=[("selected", ACCENT)])

    def _build_ui(self) -> None:
        header = ttk.Frame(self, padding=(14, 12, 14, 6))
        header.pack(fill="x")
        ttk.Label(header, text=f"Limitador {APP_VERSION}", style="Title.TLabel").pack(side="left")
        self.status_var = tk.StringVar(value="Detenido")
        ttk.Label(header, textvariable=self.status_var, style="Status.TLabel").pack(side="left", padx=12)

        buttons = ttk.Frame(self, padding=(14, 0, 14, 8))
        buttons.pack(fill="x")
        self.toggle_btn = ttk.Button(buttons, text="Iniciar limitación", command=self._toggle_engine)
        self.toggle_btn.pack(side="left")
        self.pause_btn = ttk.Button(buttons, text="Pausar", command=self._toggle_pause, state="disabled")
        self.pause_btn.pack(side="left", padx=6)
        self.uptime_var = tk.StringVar(value="")
        ttk.Label(buttons, textvariable=self.uptime_var, style="Muted.TLabel").pack(side="right")

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=14, pady=4)
        self._build_general_tab(notebook)
        self._build_apps_tab(notebook)
        self._build_advanced_tab(notebook)
        self._build_log_tab(notebook)

        graph_frame = ttk.Frame(self, padding=(14, 6, 14, 4))
        graph_frame.pack(fill="x")
        self.canvas = tk.Canvas(graph_frame, height=78, bg=BG_ALT, highlightthickness=0)
        self.canvas.pack(fill="x")
        self.total_var = tk.StringVar(value="")
        ttk.Label(graph_frame, textvariable=self.total_var, style="Muted.TLabel").pack(anchor="w", pady=(3, 0))

    # ---------------------------------------------------------------------- tabs
    def _build_general_tab(self, notebook: ttk.Notebook) -> None:
        tab = ttk.Frame(notebook, padding=14)
        notebook.add(tab, text="General")

        ttk.Label(tab, text="Límite global (todo el equipo, sin configurar el router)",
                  style="Card.TLabel").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))

        ttk.Label(tab, text="Descarga").grid(row=1, column=0, sticky="w", pady=4)
        self.down_var = tk.StringVar()
        down_entry = ttk.Entry(tab, textvariable=self.down_var, width=10, justify="right")
        down_entry.grid(row=1, column=1, sticky="w", pady=4)
        ttk.Label(tab, text="Subida").grid(row=2, column=0, sticky="w", pady=4)
        self.up_var = tk.StringVar()
        up_entry = ttk.Entry(tab, textvariable=self.up_var, width=10, justify="right")
        up_entry.grid(row=2, column=1, sticky="w", pady=4)

        self.unit_var = tk.StringVar(value="MB/s")
        unit_box = ttk.Combobox(tab, textvariable=self.unit_var, values=list(UNITS),
                                width=7, state="readonly")
        unit_box.grid(row=1, column=2, rowspan=2, padx=8)
        unit_box.bind("<<ComboboxSelected>>", lambda _e: self._sync_widgets_from_config())

        ttk.Label(tab, text="0 = sin límite", style="Muted.TLabel").grid(row=3, column=0,
                                                                        columnspan=4, sticky="w")

        presets = ttk.Frame(tab)
        presets.grid(row=4, column=0, columnspan=4, sticky="w", pady=(10, 4))
        ttk.Label(presets, text="Rápido:").pack(side="left", padx=(0, 6))
        for value in (1, 5, 10, 25, 50, 100):
            ttk.Button(presets, text=f"{value} MB/s", width=8,
                       command=lambda v=value: self._apply_preset(v)).pack(side="left", padx=2)

        apply_row = ttk.Frame(tab)
        apply_row.grid(row=5, column=0, columnspan=4, sticky="w", pady=(10, 0))
        ttk.Button(apply_row, text="Aplicar límites globales",
                   command=self._apply_global).pack(side="left")
        self.global_hint = tk.StringVar(value="")
        ttk.Label(apply_row, textvariable=self.global_hint, style="Muted.TLabel").pack(side="left", padx=8)

        for entry in (down_entry, up_entry):
            entry.bind("<Return>", lambda _e: self._apply_global())
            entry.bind("<FocusOut>", lambda _e: self._apply_global())
        ttk.Label(tab, text="Los límites se aplican en caliente: no hace falta reiniciar la captura.",
                  style="Muted.TLabel").grid(row=6, column=0, columnspan=4, sticky="w", pady=(10, 0))

    def _build_apps_tab(self, notebook: ttk.Notebook) -> None:
        tab = ttk.Frame(notebook, padding=10)
        notebook.add(tab, text="Aplicaciones")

        columns = ("app", "pid", "down", "up", "limit_down", "limit_up", "state")
        self.apps_tree = ttk.Treeview(tab, columns=columns, show="headings", height=11)
        headings = {
            "app": ("Aplicación", 190), "pid": ("PID", 55), "down": ("↓ ahora", 80),
            "up": ("↑ ahora", 80), "limit_down": ("Límite ↓", 80),
            "limit_up": ("Límite ↑", 80), "state": ("Estado", 90),
        }
        for key, (text, width) in headings.items():
            self.apps_tree.heading(key, text=text)
            self.apps_tree.column(key, width=width, anchor="w" if key == "app" else "center")
        self.apps_tree.pack(fill="both", expand=True)
        self.apps_tree.bind("<<TreeviewSelect>>", self._on_app_selected)

        row = ttk.Frame(tab, padding=(0, 8, 0, 0))
        row.pack(fill="x")
        self.limit_btn = ttk.Button(row, text="Limitar seleccionada…", command=self._limit_selected)
        self.limit_btn.pack(side="left")
        ttk.Button(row, text="Quitar límite", command=self._remove_limit).pack(side="left", padx=6)
        ttk.Button(row, text="Refrescar ahora", command=self._refresh_apps).pack(side="left")

        ttk.Label(tab, text="La atribución usa los puertos locales de cada proceso "
                            "(capa de sockets de WinDivert + tabla de conexiones).",
                  style="Muted.TLabel").pack(anchor="w", pady=(6, 0))

    def _build_advanced_tab(self, notebook: ttk.Notebook) -> None:
        tab = ttk.Frame(notebook, padding=14)
        notebook.add(tab, text="Avanzado")

        ttk.Label(tab, text="Presupuesto de retardo (ms)").grid(row=0, column=0, sticky="w", pady=4)
        self.delay_var = tk.StringVar()
        delay = ttk.Spinbox(tab, from_=5, to=500, increment=5, width=6,
                            textvariable=self.delay_var, command=self._apply_advanced)
        delay.grid(row=0, column=1, sticky="w")
        delay.bind("<FocusOut>", lambda _e: self._apply_advanced())
        ttk.Label(tab, text="Cuánto puede retener un paquete como máximo. Define también la ráfaga\n"
                            "(burst = tasa × retardo). Más bajo = menos bufferbloat, más descartes.",
                  style="Muted.TLabel", justify="left").grid(row=1, column=0, columnspan=3,
                                                             sticky="w", pady=(0, 10))

        self.control_var = tk.BooleanVar()
        self.lan_var = tk.BooleanVar()
        self.ipv6_var = tk.BooleanVar()
        self.tray_var = tk.BooleanVar()
        self.autostart_var = tk.BooleanVar()
        self.autostart_on_launch_var = tk.BooleanVar()
        options = [
            (self.control_var, "No limitar tráfico de control (ACK, DNS, ICMP)",
             "Recomendado: retardar los ACK multiplica la latencia sin controlar la tasa."),
            (self.lan_var, "Excluir la red local del límite",
             "Tráfico a 10.x / 172.16-31.x / 192.168.x, multicast y broadcast no se limita."),
            (self.ipv6_var, "Limitar también IPv6",
             "Si lo desactivas, solo se limita IPv4."),
            (self.tray_var, "Cerrar a la bandeja del sistema",
             "Cerrar la ventana deja el limitador activo, con icono en la bandeja."),
            (self.autostart_var, "Iniciar con Windows (tarea programada, sin UAC al arrancar)",
             f"Estado actual: {autostart.status_text()}"),
            (self.autostart_on_launch_var, "Empezar a limitar al abrir la aplicación", ""),
        ]
        row = 2
        for var, text, hint in options:
            ttk.Checkbutton(tab, text=text, variable=var,
                            command=self._apply_advanced).grid(row=row, column=0, columnspan=3,
                                                              sticky="w", pady=(4, 0))
            row += 1
            if hint:
                ttk.Label(tab, text=hint, style="Muted.TLabel", justify="left").grid(
                    row=row, column=0, columnspan=3, sticky="w", padx=(22, 0))
                row += 1

        ttk.Separator(tab).grid(row=row, column=0, columnspan=3, sticky="ew", pady=10)
        row += 1
        self.stats_var = tk.StringVar(value="")
        ttk.Label(tab, textvariable=self.stats_var, justify="left",
                  style="Muted.TLabel").grid(row=row, column=0, columnspan=3, sticky="w")
        row += 1
        actions = ttk.Frame(tab)
        actions.grid(row=row, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Button(actions, text="Reiniciar contadores", command=self._reset_counters).pack(side="left")
        ttk.Button(actions, text="Diagnóstico", command=self._show_diagnostics).pack(side="left", padx=6)
        ttk.Button(actions, text="Reparar driver", command=self._repair_driver).pack(side="left")

    def _build_log_tab(self, notebook: ttk.Notebook) -> None:
        tab = ttk.Frame(notebook, padding=10)
        notebook.add(tab, text="Registro")
        self.log_text = tk.Text(tab, height=14, bg=BG_ALT, fg=FG, insertbackground=FG,
                                relief="flat", font=("Consolas", 8), wrap="none")
        self.log_text.pack(fill="both", expand=True)
        self.log_text.configure(state="disabled")
        row = ttk.Frame(tab, padding=(0, 6, 0, 0))
        row.pack(fill="x")
        ttk.Button(row, text="Abrir carpeta de configuración",
                   command=self._open_config_dir).pack(side="left")
        ttk.Label(row, text=f"Log: {log_path()}", style="Muted.TLabel").pack(side="left", padx=8)

    # --------------------------------------------------------------------- state
    def _sync_widgets_from_config(self) -> None:
        settings = self.config_data.settings
        unit = self.unit_var.get()
        self.down_var.set(to_display(settings.global_down_bps, unit))
        self.up_var.set(to_display(settings.global_up_bps, unit))
        self.delay_var.set(f"{settings.max_delay_ms:g}")
        self.control_var.set(settings.exempt_control)
        self.lan_var.set(settings.exclude_lan)
        self.ipv6_var.set(settings.limit_ipv6)
        self.tray_var.set(settings.minimize_to_tray)
        self.autostart_var.set(autostart.is_enabled())
        self.autostart_on_launch_var.set(settings.start_on_launch)

    def _apply_global(self) -> None:
        try:
            self.config_data.settings.global_down_bps = parse_rate(self.down_var.get(), self.unit_var.get())
            self.config_data.settings.global_up_bps = parse_rate(self.up_var.get(), self.unit_var.get())
        except ValueError as exc:
            messagebox.showerror("Límite inválido", str(exc), parent=self)
            self._sync_widgets_from_config()
            return
        self._commit_settings()
        settings = self.config_data.settings
        if settings.has_global_limit:
            self.global_hint.set(f"↓ {format_rate(settings.global_down_bps)} · "
                                 f"↑ {format_rate(settings.global_up_bps)}")
        else:
            self.global_hint.set("sin límite global")

    def _apply_preset(self, value: int) -> None:
        self.unit_var.set("MB/s")
        self.down_var.set(str(value))
        self.up_var.set(str(max(value // 4, 1)))
        self._apply_global()

    def _apply_advanced(self) -> None:
        settings = self.config_data.settings
        try:
            settings.max_delay_ms = float(self.delay_var.get().replace(",", "."))
        except ValueError:
            settings.max_delay_ms = 50.0
        settings.exempt_control = self.control_var.get()
        settings.exclude_lan = self.lan_var.get()
        settings.limit_ipv6 = self.ipv6_var.get()
        settings.minimize_to_tray = self.tray_var.get()
        settings.start_on_launch = self.autostart_on_launch_var.get()
        self._commit_settings()
        self._sync_autostart()

    def _sync_autostart(self) -> None:
        want = self.autostart_var.get()
        if want == autostart.is_enabled():
            return
        ok, message = autostart.sync(want)
        if not ok:
            messagebox.showerror("Inicio con Windows", message, parent=self)
            self.autostart_var.set(not want)
        logging.getLogger("limitador").info("autostart: %s", message)

    def _commit_settings(self) -> None:
        self.config_data.settings.clamp()
        self.engine.apply_config()
        try:
            self.config_data.save()
        except OSError as exc:
            logging.getLogger("limitador").warning("no se pudo guardar la configuración: %s", exc)
        self._sync_widgets_from_config()

    # -------------------------------------------------------------------- engine
    def _toggle_engine(self) -> None:
        if self.engine.is_running:
            self._stop()
        else:
            self._start()

    def _start(self) -> None:
        if self._busy:
            return
        self._apply_global()
        self._busy = True
        self.toggle_btn.config(state="disabled", text="Iniciando…")
        self.update_idletasks()
        try:
            self.engine.start()
        except LimiterError as exc:
            self._handle_start_error(str(exc))
        except Exception as exc:  # pragma: no cover - unexpected
            self._handle_start_error(f"{exc!r}")
        finally:
            self._busy = False
            self.toggle_btn.config(state="normal")
            self._refresh_state()

    def _handle_start_error(self, message: str) -> None:
        self.status_var.set("Error")
        detail = driver.diagnose()
        if messagebox.askyesno(
            "No se pudo iniciar",
            f"{message}\n\n¿Quieres ver el diagnóstico completo?",
            parent=self,
        ):
            self._show_text_window("Diagnóstico", detail)
        logging.getLogger("limitador").error(message)

    def _stop(self) -> None:
        self.toggle_btn.config(state="disabled", text="Deteniendo…")
        self.update_idletasks()
        self.engine.stop()
        self.toggle_btn.config(state="normal")
        self._refresh_state()

    def _toggle_pause(self) -> None:
        self.engine.toggle_pause()
        self._refresh_state()

    def _refresh_state(self) -> None:
        running = self.engine.is_running
        paused = self.engine.is_paused
        self.toggle_btn.config(text="Detener" if running else "Iniciar limitación")
        self.pause_btn.config(state="normal" if running else "disabled",
                              text="Reanudar" if paused else "Pausar")
        if not running:
            self.status_var.set("Detenido")
        elif paused:
            self.status_var.set("En pausa (sin límites)")
        else:
            settings = self.config_data.settings
            active = settings.has_global_limit or bool(self.config_data.active_rules())
            self.status_var.set("Limitando" if active else "Monitorizando (sin límites)")

    # ---------------------------------------------------------------------- loop
    def _drain_events(self) -> None:
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            # The engine already wrote this to the log file; here it only needs to show up
            # in the log tab (re-logging it would duplicate every line).
            if event.level == "error":
                self.status_var.set("Aviso: revisa el registro")
        while True:
            try:
                line = self.log_lines.get_nowait()
            except queue.Empty:
                break
            self.log_text.configure(state="normal")
            self.log_text.insert("end", line + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self._refresh_state()
        if not self._closing:
            self.after(250, self._drain_events)

    def _refresh_loop(self) -> None:
        now = time.monotonic()
        snapshot = self.engine.snapshot()
        span = max(now - self._previous_time, 1e-6)
        if self._previous is not None:
            down = (snapshot.bytes_down - self._previous.bytes_down) / span
            up = (snapshot.bytes_up - self._previous.bytes_up) / span
            self._history["down"].append(down)
            self._history["up"].append(up)
            self._update_app_rates(snapshot, span)
        self._previous, self._previous_time = snapshot, now
        self._snapshot = snapshot
        self._draw_graph()
        settings = self.config_data.settings
        self.total_var.set(
            f"↓ {format_bytes(snapshot.bytes_down)} · ↑ {format_bytes(snapshot.bytes_up)} · "
            f"descartado {format_bytes(snapshot.dropped_down + snapshot.dropped_up)} "
            f"({snapshot.dropped_packets} paq.)"
            + (f" · cola {format_bytes(snapshot.queued_down + snapshot.queued_up)}"
               if snapshot.queued_down + snapshot.queued_up else "")
        )
        self.uptime_var.set(f"activo {format_duration(snapshot.uptime)}" if snapshot.running else "")
        self.stats_var.set(
            f"Puertos identificados: {snapshot.port_entries} · "
            f"capa de sockets: {'activa' if snapshot.socket_events_ok else 'no disponible'}\n"
            f"Retardo máx. configurado: {settings.max_delay_ms:.0f} ms · "
            f"desbordes de cola: {snapshot.queue_overflows}\n"
            f"Reglas activas: {len(self.config_data.active_rules())}"
        )
        self._refresh_apps()
        self._refresh_state()
        if not self._closing:
            self.after(1000, self._refresh_loop)

    def _update_app_rates(self, snapshot: Snapshot, span: float) -> None:
        if self._previous is None:
            return
        rates: dict[int, tuple[float, float]] = {}
        for pid, (down_total, up_total) in snapshot.per_pid.items():
            previous = self._previous.per_pid.get(pid)
            if previous is None:
                continue
            rates[pid] = ((down_total - previous[0]) / span, (up_total - previous[1]) / span)
        self._rate_cache.update(rates)

    # ----------------------------------------------------------------- apps list
    def _refresh_apps(self) -> None:
        snapshot = self._snapshot
        now = time.monotonic()
        if now - getattr(self, "_last_pmap", 0.0) > 2.0:
            self.pmap.refresh()
            self._last_pmap = now
        selected = self._selected_pid
        self.apps_tree.delete(*self.apps_tree.get_children())
        self._app_rows.clear()

        pids = set(snapshot.per_pid) if snapshot else set()
        pids |= set(self.pmap.pids())
        rules_by_exe = {self.config_data.normalise_exe(r.exe): r for r in self.config_data.rules}
        matched_rules: set[str] = set()

        def add_row(pid: int | None, name: str, exe: str | None, down: float, up: float) -> None:
            rule = rules_by_exe.get(self.config_data.normalise_exe(exe or ""))
            if rule:
                matched_rules.add(self.config_data.normalise_exe(rule.exe))
            state = "limitada" if (rule and rule.has_limit) else ("regla inactiva" if rule else "")
            item = self.apps_tree.insert("", "end", values=(
                name[:44], pid if pid else "—",
                format_rate(down) if down else "—",
                format_rate(up) if up else "—",
                format_rate(rule.down_bps) if rule and rule.down_bps else "—",
                format_rate(rule.up_bps) if rule and rule.up_bps else "—",
                state,
            ), tags=("limited",) if state == "limitada" else ())
            self._app_rows[item] = pid

        for pid in sorted(pids, key=lambda p: -(self._rate_cache.get(p, (0, 0))[0])):
            exe = self.pmap.path_for(pid)
            down, up = self._rate_cache.get(pid, (0.0, 0.0))
            add_row(pid, self.pmap.name_for(pid), exe, down, up)

        for rule in self.config_data.rules:      # rules for apps that are not running now
            key = self.config_data.normalise_exe(rule.exe)
            if key not in matched_rules:
                add_row(None, f"{rule.display_name} (sin conexiones)", rule.exe, 0.0, 0.0)

        self.apps_tree.tag_configure("limited", foreground=ACCENT)
        if selected is not None:
            for item, pid in self._app_rows.items():
                if pid == selected:
                    self.apps_tree.selection_set(item)
                    break

    def _on_app_selected(self, _event=None) -> None:
        selection = self.apps_tree.selection()
        self._selected_pid = self._app_rows.get(selection[0]) if selection else None

    def _limit_selected(self) -> None:
        selection = self.apps_tree.selection()
        if not selection:
            messagebox.showinfo("Aplicaciones", "Selecciona primero una aplicación.", parent=self)
            return
        pid = self._app_rows.get(selection[0])
        exe = self.pmap.path_for(pid) if pid else None
        if not exe:
            values = self.apps_tree.item(selection[0], "values")
            name = str(values[0]).replace(" (sin conexiones)", "")
            rule = next((r for r in self.config_data.rules if r.display_name == name), None)
            exe = rule.exe if rule else None
        if not exe:
            messagebox.showinfo("Aplicaciones",
                                "No se pudo determinar el ejecutable de esta aplicación.",
                                parent=self)
            return
        name = self.pmap.name_for(pid) if pid else Path(exe).name
        existing = self.config_data.rule_for_exe(exe)
        self._limit_dialog(exe, name, existing)

    def _limit_dialog(self, exe: str, name: str, existing: AppRule | None) -> None:
        dialog = tk.Toplevel(self)
        dialog.title(f"Límite para {name}")
        dialog.configure(bg=BG)
        dialog.transient(self)
        dialog.grab_set()
        dialog.geometry("380x230")

        ttk.Label(dialog, text=name, style="Title.TLabel").pack(anchor="w", padx=14, pady=(12, 0))
        ttk.Label(dialog, text=exe, style="Muted.TLabel", wraplength=340).pack(anchor="w", padx=14)

        form = ttk.Frame(dialog, padding=14)
        form.pack(fill="x")
        unit = "MB/s"
        ttk.Label(form, text="Descarga").grid(row=0, column=0, sticky="w", pady=4)
        down_var = tk.StringVar(value=to_display(existing.down_bps, unit) if existing else "")
        ttk.Entry(form, textvariable=down_var, width=10, justify="right").grid(row=0, column=1, sticky="w")
        ttk.Label(form, text="Subida").grid(row=1, column=0, sticky="w", pady=4)
        up_var = tk.StringVar(value=to_display(existing.up_bps, unit) if existing else "")
        ttk.Entry(form, textvariable=up_var, width=10, justify="right").grid(row=1, column=1, sticky="w")
        unit_var = tk.StringVar(value=unit)
        ttk.Combobox(form, textvariable=unit_var, values=list(UNITS), width=7,
                     state="readonly").grid(row=0, column=2, rowspan=2, padx=8)
        ttk.Label(form, text="0 o vacío = sin límite en esa dirección",
                  style="Muted.TLabel").grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))

        def save() -> None:
            try:
                down = parse_rate(down_var.get(), unit_var.get())
                up = parse_rate(up_var.get(), unit_var.get())
            except ValueError as exc:
                messagebox.showerror("Límite inválido", str(exc), parent=dialog)
                return
            if down == 0 and up == 0:
                self.config_data.remove_rule(exe)
            else:
                self.config_data.set_rule(AppRule(exe=exe, name=name, down_bps=down, up_bps=up))
            self._commit_settings()
            self.engine.apply_config()
            logging.getLogger("limitador").info(
                "límite por aplicación: %s ↓%s ↑%s", name,
                format_rate(down), format_rate(up))
            dialog.destroy()

        actions = ttk.Frame(dialog, padding=(14, 0, 14, 12))
        actions.pack(fill="x")
        ttk.Button(actions, text="Guardar", command=save).pack(side="right")
        if existing:
            def remove() -> None:
                self.config_data.remove_rule(exe)
                self._commit_settings()
                logging.getLogger("limitador").info("límite eliminado: %s", name)
                dialog.destroy()
            ttk.Button(actions, text="Quitar límite", command=remove).pack(side="right", padx=6)
        ttk.Button(actions, text="Cancelar", command=dialog.destroy).pack(side="right")

    def _remove_limit(self) -> None:
        selection = self.apps_tree.selection()
        if not selection:
            return
        pid = self._app_rows.get(selection[0])
        exe = self.pmap.path_for(pid) if pid else None
        if exe and self.config_data.remove_rule(exe):
            self._commit_settings()
            logging.getLogger("limitador").info("límite eliminado: %s", Path(exe).name)
            return
        messagebox.showinfo("Aplicaciones", "Esa fila no tiene un límite guardado.", parent=self)

    # -------------------------------------------------------------------- graph
    def _draw_graph(self) -> None:
        canvas = self.canvas
        canvas.delete("all")
        width = max(canvas.winfo_width(), 1)
        height = max(canvas.winfo_height(), 1)
        settings = self.config_data.settings
        history = self._history
        peak = max(
            [max(history["down"], default=0.0), max(history["up"], default=0.0),
             settings.global_down_bps, settings.global_up_bps, 1024.0]
        )
        for fraction in (0.5, 1.0):
            y = height - fraction * height
            canvas.create_line(0, y, width, y, fill="#333333")
        canvas.create_text(4, 8, anchor="w", fill=ACCENT,
                           text=f"↓ {format_rate(history['down'][-1] if history['down'] else 0)}",
                           font=("Consolas", 8))
        canvas.create_text(4, 20, anchor="w", fill=ACCENT_WARM,
                           text=f"↑ {format_rate(history['up'][-1] if history['up'] else 0)}",
                           font=("Consolas", 8))
        for key, color in (("down", ACCENT), ("up", ACCENT_WARM)):
            values = list(history[key])
            if len(values) < 2:
                continue
            step = width / max(len(values) - 1, 1)
            points = []
            for index, value in enumerate(values):
                y = height - (value / peak) * (height - 6) - 3
                points.extend((index * step, y))
            canvas.create_line(*points, fill=color, width=1.4)
        if settings.has_global_limit:
            for rate, color in ((settings.global_down_bps, ACCENT), (settings.global_up_bps, ACCENT_WARM)):
                if rate > 0:
                    y = height - (rate / peak) * (height - 6) - 3
                    canvas.create_line(0, y, width, y, fill=color, dash=(3, 3))

    # ------------------------------------------------------------------ actions
    def _reset_counters(self) -> None:
        self.engine.reset_counters()
        self._rate_cache.clear()
        self._history = {"down": deque(maxlen=60), "up": deque(maxlen=60)}

    def _show_diagnostics(self) -> None:
        self._show_text_window("Diagnóstico", driver.diagnose())

    def _show_text_window(self, title: str, content: str) -> None:
        window = tk.Toplevel(self)
        window.title(title)
        window.configure(bg=BG)
        window.geometry("640x460")
        text = tk.Text(window, bg=BG_ALT, fg=FG, insertbackground=FG, wrap="word",
                       font=("Consolas", 8), relief="flat")
        text.pack(fill="both", expand=True, padx=10, pady=(10, 4))
        text.insert("1.0", content)
        row = ttk.Frame(window, padding=(10, 0, 10, 10))
        row.pack(fill="x")
        ttk.Button(row, text="Copiar al portapapeles",
                   command=lambda: (self.clipboard_clear(), self.clipboard_append(content))).pack(side="right")
        ttk.Button(row, text="Cerrar", command=window.destroy).pack(side="right", padx=6)

    def _repair_driver(self) -> None:
        if not driver.is_admin():
            messagebox.showwarning("Reparar driver",
                                   "Necesitas ejecutar Limitador como administrador.", parent=self)
            return
        self.status_var.set("Reparando driver…")
        self.update_idletasks()

        def work() -> None:
            ok, detail = driver.install(force_reinstall=True)
            self.events.put(EngineEvent(
                "info" if ok else "error",
                f"Reparación del driver: {detail}"))
            self.after(0, lambda: messagebox.showinfo(
                "Reparar driver", f"{'Correcto' if ok else 'Fallo'}: {detail}", parent=self))
            self.after(0, self._refresh_state)

        threading.Thread(target=work, name="repair-driver", daemon=True).start()

    def _open_config_dir(self) -> None:
        target = log_path().parent
        target.mkdir(parents=True, exist_ok=True)
        import os
        os.startfile(str(target))  # noqa: S606 - opening our own folder

    # --------------------------------------------------------------------- tray
    def _start_tray(self) -> None:
        if not self.config_data.settings.minimize_to_tray:
            return
        try:
            import pystray
            from PIL import Image
        except ImportError:
            logging.getLogger("limitador").info("pystray/Pillow no instalados: sin icono de bandeja")
            return
        icon_path = Path(__file__).resolve().parent.parent / "Limitador.ico"
        try:
            image = Image.open(icon_path) if icon_path.exists() else Image.new("RGBA", (64, 64), (30, 30, 30, 255))
        except Exception:
            image = Image.new("RGBA", (64, 64), (30, 30, 30, 255))

        def show(_icon=None, _item=None) -> None:
            self.after(0, self._show_window)

        def toggle(_icon=None, _item=None) -> None:
            self.after(0, self._toggle_pause)

        def quit_app(_icon=None, _item=None) -> None:
            self.after(0, lambda: self._on_close(force=True))

        menu = pystray.Menu(
            pystray.MenuItem("Mostrar Limitador", show, default=True),
            pystray.MenuItem("Pausar/Reanudar", toggle),
            pystray.MenuItem("Salir", quit_app),
        )
        self._tray = pystray.Icon("Limitador", image, "Limitador", menu)
        threading.Thread(target=self._tray.run, name="tray", daemon=True).start()

    def _hide_to_tray(self) -> None:
        if self._tray is None:
            return
        self.withdraw()
        self.status_var.set("En segundo plano (bandeja del sistema)")

    def _show_window(self) -> None:
        self.deiconify()
        self.lift()
        self.focus_force()

    # -------------------------------------------------------------------- close
    def _on_close(self, force: bool = False) -> None:
        if self._closing:
            return
        if not force and self.config_data.settings.minimize_to_tray and self._tray is not None:
            self._hide_to_tray()
            return
        self._closing = True
        try:
            self.engine.stop()
        finally:
            if self._tray is not None:
                try:
                    self._tray.stop()
                except Exception:
                    pass
            try:
                self.config_data.save()
            except OSError:
                pass
            self.destroy()
