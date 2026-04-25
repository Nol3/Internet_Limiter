# Limitador

Limitador de ancho de banda PC-wide para Windows. Interfaz minimalista.  
Bajo bufferbloat (AQM drop si latencia > 30 ms).

---

## Uso rápido (usuario final)

1. **Build el .exe** (solo una vez, ver abajo).
2. Doble-click `dist\Limitador.exe`.
3. Windows pide permiso UAC → **Sí**.
4. Introduce MB/s → **Iniciar**. Listo.

> No requiere terminal, no requiere venv, no requiere PowerShell admin.

---

## Build del ejecutable

Solo primera vez:

```powershell
cd "C:\Users\aleja\Desktop\claude\Limitador"
.\build.bat
```

Esto:
- crea venv,
- instala deps + PyInstaller,
- genera `dist\Limitador.exe` (onefile, windowed, UAC admin baked in).

El .exe resultante es **autónomo**: cópialo donde quieras, hace doble-click y funciona.

---

## Desarrollo (sin .exe)

```powershell
cd "C:\Users\aleja\Desktop\claude\Limitador"
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

> `main.py` auto-solicita UAC. No hace falta abrir PowerShell como admin.

---

## Cómo funciona

- **pydivert/WinDivert** intercepta todos los paquetes IP (in/out) a nivel kernel.
- **Token bucket** con burst corto (20 ms) limita bytes/segundo.
- **AQM**: si un paquete necesitaría > 30 ms de espera → **drop** (TCP reajusta rate).
  Esto evita bufferbloat: latencia extra < 30 ms garantizada.
- **LAN excluida**: 10.x, 172.16-31.x, 192.168.x y loopback no se limitan.

## Archivos

| Archivo            | Rol                                          |
|--------------------|----------------------------------------------|
| `main.py`          | GUI Tkinter + auto-elevación UAC             |
| `sniffer.py`       | WinDivert engine (threads IN/OUT)            |
| `limiter.py`       | Token bucket + AQM drop                      |
| `build.bat`        | Compila `dist\Limitador.exe` con PyInstaller |
| `fix-service.bat`  | Repara error 1058 (servicio deshabilitado)   |
| `requirements.txt` | pydivert                                     |

---

## Troubleshooting

### `OSError: [WinError 1058] No se puede iniciar el servicio`

Driver WinDivert deshabilitado (antivirus o instalación corrupta).

**Opción A — desde la app:** al pulsar Iniciar sale diálogo → click "Reparar automáticamente".

**Opción B — manual:** botón derecho en `fix-service.bat` → **Ejecutar como administrador**.

### Velocidad real < límite configurado

Normal. El overhead de protocolo (TCP/IP headers, retransmisiones) cuenta en bytes enviados pero no en payload útil. Pon límite un 5-10% más alto.

### Latencia alta al limitar

El AQM mantiene el delay máximo por paquete < 30 ms. Si aún notas lag:
- reduce `burst_ms` en `limiter.py` (ej: 10)
- reduce `max_delay_ms` en `limiter.py` (ej: 15)

### Antivirus bloquea WinDivert

Añade exclusión para `WinDivert.sys` / `WinDivert64.sys` (instalado por pydivert).

---

## Notas

- Cerrar la ventana detiene el limitador inmediatamente.
- El driver WinDivert se descarga al cerrar el proceso.
- IPv4 solo. IPv6 no está limitado (filter no incluye `ipv6`).
