"""Ultra-simple bandwidth limiter GUI with UAC auto-elevation."""
from __future__ import annotations
import ctypes
import os
import subprocess
import sys
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

import autostart

MB = 1024 * 1024
SERVICE_NAMES = ("WinDivert", "WinDivert1.4")


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def elevate_and_exit() -> None:
    """Relaunch current script as admin via UAC prompt, then exit."""
    params = " ".join(f'"{a}"' for a in sys.argv)
    ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, params, None, 1
    )
    sys.exit(0)


def resource_path(rel: str) -> str:
    """Ruta a recurso, funciona en dev y dentro del .exe de PyInstaller."""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, rel)


def _sc(*args: str) -> tuple[int, str]:
    try:
        r = subprocess.run(
            ["sc.exe", *args], capture_output=True, text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return -1, str(e)


def repair_windivert_service() -> tuple[bool, str]:
    msgs = []
    for name in SERVICE_NAMES:
        _sc("stop", name)
        rc, _ = _sc("config", name, "start=", "demand")
        msgs.append(f"config {name}: rc={rc}")
        rc, _ = _sc("delete", name)
        msgs.append(f"delete {name}: rc={rc}")
    return True, " | ".join(msgs)


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Limitador")
        self.geometry("340x290")
        self.resizable(False, False)
        self.configure(bg="#1e1e1e")

        self._set_icon()

        self.limiter = None
        self._last_stats = (0, 0)
        self._last_tick = time.monotonic()

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(1000, self._tick)

    def _set_icon(self) -> None:
        ico = resource_path("Limitador.ico")
        if os.path.exists(ico):
            try:
                self.iconbitmap(ico)
            except Exception:
                pass

    def _build_ui(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TLabel", background="#1e1e1e", foreground="#eaeaea",
                        font=("Segoe UI", 10))
        style.configure("TFrame", background="#1e1e1e")
        style.configure("TEntry", fieldbackground="#2d2d2d", foreground="#eaeaea",
                        insertcolor="#eaeaea")
        style.configure("TButton", font=("Segoe UI", 10, "bold"), padding=6)
        style.configure("TCheckbutton", background="#1e1e1e", foreground="#eaeaea",
                        font=("Segoe UI", 9))
        style.map("TCheckbutton",
                  background=[("active", "#1e1e1e")],
                  foreground=[("active", "#eaeaea")])

        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="Descarga (MB/s)").grid(row=0, column=0, sticky="w", pady=4)
        self.down_var = tk.StringVar(value="550")
        ttk.Entry(frame, textvariable=self.down_var, width=10, justify="center").grid(
            row=0, column=1, sticky="e", pady=4)

        ttk.Label(frame, text="Subida (MB/s)").grid(row=1, column=0, sticky="w", pady=4)
        self.up_var = tk.StringVar(value="550")
        ttk.Entry(frame, textvariable=self.up_var, width=10, justify="center").grid(
            row=1, column=1, sticky="e", pady=4)

        self.btn = ttk.Button(frame, text="Iniciar", command=self._toggle)
        self.btn.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(12, 6))

        self.autostart_var = tk.BooleanVar(value=autostart.is_enabled())
        ttk.Checkbutton(
            frame, text="Iniciar con Windows",
            variable=self.autostart_var,
            command=self._toggle_autostart,
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 2))

        self.status = tk.StringVar(value="Detenido")
        ttk.Label(frame, textvariable=self.status, anchor="center",
                  font=("Segoe UI", 9, "italic")).grid(
            row=4, column=0, columnspan=2, sticky="ew", pady=2)

        self.rate = tk.StringVar(value="↓ 0.00 MB/s   ↑ 0.00 MB/s")
        ttk.Label(frame, textvariable=self.rate, anchor="center",
                  font=("Consolas", 10)).grid(
            row=5, column=0, columnspan=2, sticky="ew", pady=2)

    def _toggle_autostart(self) -> None:
        want = self.autostart_var.get()
        ok, msg = autostart.enable() if want else autostart.disable()
        if not ok:
            messagebox.showerror("Autostart",
                                 f"No se pudo {'activar' if want else 'desactivar'}:\n{msg}")
            self.autostart_var.set(not want)

    def _parse_rate(self, s: str) -> float:
        try:
            v = float(s.replace(",", "."))
            return max(0.0, v) * MB
        except ValueError:
            return 0.0

    def _toggle(self) -> None:
        if self.limiter and self.limiter._running:
            self._stop()
        else:
            self._start()

    def _start(self) -> None:
        down = self._parse_rate(self.down_var.get())
        up = self._parse_rate(self.up_var.get())
        if down == 0 and up == 0:
            messagebox.showwarning("Límite", "Define al menos un límite > 0.")
            return
        from sniffer import BandwidthLimiter
        try:
            self.limiter = BandwidthLimiter(down, up)
            self.limiter.start()
            time.sleep(0.5)
            # Surface errors captured inside the worker threads
            if self.limiter._thread_errors:
                err = next(iter(self.limiter._thread_errors.values()))
                raise OSError(str(err))
            # Verify both threads are still alive
            in_ok = self.limiter._in_thread and self.limiter._in_thread.is_alive()
            out_ok = self.limiter._out_thread and self.limiter._out_thread.is_alive()
            if not in_ok or not out_ok:
                raise OSError("WinDivert no pudo iniciar (servicio deshabilitado?)")
        except OSError as e:
            msg = str(e)
            if "1058" in msg or "deshabilitado" in msg.lower() or "disabled" in msg.lower():
                if messagebox.askyesno(
                    "Servicio WinDivert deshabilitado",
                    "El driver WinDivert está deshabilitado.\n\n"
                    "¿Intentar reparar automáticamente?"
                ):
                    _, info = repair_windivert_service()
                    messagebox.showinfo("Reparación",
                                        "Listo. Pulsa Iniciar de nuevo.\n\n" + info)
            else:
                messagebox.showerror("Error", f"No se pudo iniciar:\n{e}")
            self.limiter = None
            return
        except Exception as e:
            messagebox.showerror("Error", f"No se pudo iniciar:\n{e}")
            self.limiter = None
            return
        self.btn.config(text="Detener")
        self.status.set(f"Limitando ↓{down/MB:.1f} ↑{up/MB:.1f} MB/s")
        self._last_stats = (0, 0)
        self._last_tick = time.monotonic()

    def _stop(self) -> None:
        if self.limiter:
            self.limiter.stop()
            self.limiter = None
        self.btn.config(text="Iniciar")
        self.status.set("Detenido")
        self.rate.set("↓ 0.00 MB/s   ↑ 0.00 MB/s")

    def _tick(self) -> None:
        if self.limiter and self.limiter._running:
            now = time.monotonic()
            down, up = self.limiter.get_stats()
            last_down, last_up = self._last_stats
            dt = max(now - self._last_tick, 0.001)
            d_rate = (down - last_down) / dt / MB
            u_rate = (up - last_up) / dt / MB
            self.rate.set(f"↓ {d_rate:5.2f} MB/s   ↑ {u_rate:5.2f} MB/s")
            self._last_stats = (down, up)
            self._last_tick = now
        self.after(1000, self._tick)

    def _on_close(self) -> None:
        if self.limiter:
            self.limiter.stop()
        self.destroy()


def main() -> int:
    if not is_admin():
        elevate_and_exit()
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
