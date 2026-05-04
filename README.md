# Limitador

> Limitador de ancho de banda **PC-wide** para Windows. Sin configurar el router, sin tocar el OS. Doble click y listo.

![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-blue)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-Apache%202.0-green)
![Release](https://img.shields.io/badge/release-v1.0.0-brightgreen)

---

## Características

- **PC-wide**: limita TODO el tráfico del equipo (no por aplicación).
- **Bajo bufferbloat**: AQM con drop si la latencia supera 30 ms.
- **Cero configuración**: GUI minimalista, dos campos (descarga/subida en MB/s).
- **Auto-elevación UAC**: no requiere abrir terminal como admin.
- **Inicio con Windows** (opcional): vía Task Scheduler, sin prompt UAC al login.
- **Driver kernel**: usa WinDivert para interceptar paquetes IP a nivel kernel.
- **LAN excluida**: tráfico a `10.x`, `172.16-31.x`, `192.168.x` y loopback no se limita.

---

## Descarga (usuario final)

Tienes dos opciones en la página de [Releases](../../releases/latest):

### Opción 1 · Instalador wizard (recomendado)

1. Descarga `Setup_Limitador_1.0.0.exe`.
2. Doble click → sigue el asistente.
3. Acceso directo en el escritorio + Menú Inicio.
4. (Opcional) Marca "Iniciar con Windows" durante la instalación.

### Opción 2 · Portable

1. Descarga `Limitador.exe`.
2. Cópialo donde quieras (USB, carpeta personal, etc.).
3. Doble click → UAC → Iniciar.

> No requiere Python, no requiere instalar nada extra. El driver WinDivert se carga al arrancar la app y se descarga al cerrarla.

---

## Uso

1. Doble click en `Limitador.exe` (o el acceso directo si usaste el instalador).
2. Acepta el prompt UAC (necesario para cargar el driver kernel).
3. Define los límites:
   - **Descarga (MB/s)**: tráfico entrante.
   - **Subida (MB/s)**: tráfico saliente.
   - Pon `0` para no limitar esa dirección.
4. Click **Iniciar**. El indicador inferior muestra el rate real en tiempo real.
5. Click **Detener** para liberar el ancho de banda completo.

Cerrar la ventana detiene automáticamente el limitador.

---

## Compilar desde código (developers)

Requisitos:
- Windows 10 / 11
- Python 3.10 o superior — [python.org](https://www.python.org/downloads/)
- *(Opcional para instalador)* Inno Setup 6.x — [jrsoftware.org](https://jrsoftware.org/isdl.php)

### Build standalone `.exe`

```powershell
.\build.bat
```

Esto:
- Crea `.venv/`
- Instala dependencias (`pydivert`, `Pillow`, `pyinstaller`)
- Genera el icono desde `Limiter_icon.png`
- Compila `dist\Limitador.exe` (onefile, windowed, UAC admin embebido)
- Crea acceso directo `Limitador.lnk` en la raíz

### Build instalador wizard

```powershell
.\build_installer.bat
```

Compila el `.exe` (paso anterior) **y** genera `installer_output\Setup_Limitador_1.0.0.exe`. Requiere Inno Setup 6.x.

### Modo desarrollo (sin compilar)

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

`main.py` auto-solicita UAC; no hace falta abrir PowerShell como admin.

---

## Cómo funciona

| Componente | Rol |
|------------|-----|
| **WinDivert** (vía pydivert) | Intercepta paquetes IPv4 in/out a nivel kernel |
| **Token bucket** | Rate limit con burst corto (20 ms) |
| **AQM (drop)** | Si un paquete necesitaría > 30 ms de espera → drop. TCP reajusta automáticamente. |
| **Filtro LAN** | Tráfico privado y loopback se deja pasar sin throttle |

**Resultado**: latencia extra garantizada < 30 ms incluso con el límite saturado, evitando bufferbloat severo.

---

## Estructura del proyecto

```
Internet_Limiter/
├── main.py              # GUI Tkinter + auto-elevación UAC
├── sniffer.py           # Engine WinDivert (threads IN/OUT)
├── limiter.py           # Token bucket + AQM drop
├── autostart.py         # Registro en Task Scheduler
├── make_icon.py         # Genera Limitador.ico desde PNG
├── build.bat            # Compila dist\Limitador.exe
├── build_installer.bat  # Compila + Setup_Limitador.exe
├── installer.iss        # Script Inno Setup
├── fix-service.bat      # Repair manual del driver WinDivert
├── requirements.txt     # pydivert, Pillow
├── Limiter_icon.png     # Icono fuente (transparente)
└── Limitador.ico        # Icono multi-resolución (generado)
```

---

## Troubleshooting

### `OSError: [WinError 1058] No se puede iniciar el servicio`

El driver WinDivert está deshabilitado (suele pasar tras intervención de antivirus).

- **Desde la app**: al pulsar "Iniciar" aparece un diálogo → click "Reparar automáticamente".
- **Manual**: click derecho en `fix-service.bat` → "Ejecutar como administrador".

### Velocidad real menor que el límite configurado

Es normal. Los headers TCP/IP y retransmisiones cuentan en bytes enviados pero no en payload útil. Configura el límite un 5–10% más alto que el rate deseado.

### Latencia alta al limitar

El AQM mantiene el delay máximo por paquete < 30 ms. Si aún notas lag, edita `limiter.py`:
- Reduce `burst_ms` (por defecto 20 → prueba 10)
- Reduce `max_delay_ms` (por defecto 30 → prueba 15)

### Antivirus bloquea WinDivert

Falso positivo común. Añade exclusión para `WinDivert.sys` / `WinDivert64.sys` (instalado por pydivert).

### "Iniciar con Windows" no funciona en modo dev

Correcto. Autostart solo funciona con el ejecutable compilado. En modo `python main.py` la opción está deshabilitada — primero ejecuta `build.bat`.

---

## Notas técnicas

- IPv4 únicamente (IPv6 no se filtra).
- El driver WinDivert se descarga al cerrar la app (libera el servicio).
- Cerrar la ventana detiene el limitador inmediatamente.
- La tarea de autostart se llama `Limitador` en Task Scheduler.

---

## Licencia

[Apache License 2.0](LICENSE) — uso libre, incluye uso comercial.

WinDivert se distribuye bajo [LGPL v3](https://reqrypt.org/windivert.html).

---

## Créditos

- [WinDivert](https://reqrypt.org/windivert.html) — driver de captura de paquetes.
- [pydivert](https://github.com/ffalcinelli/pydivert) — bindings Python.
- [PyInstaller](https://pyinstaller.org/) — empaquetado.
- [Inno Setup](https://jrsoftware.org/isinfo.php) — instalador wizard.
