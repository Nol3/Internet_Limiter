# Changelog

Todas las versiones notables de este proyecto se documentan en este archivo.

El formato sigue [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/),
y este proyecto adhiere a [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.0.0] — 2026-05-04

Primera release pública estable.

### Añadido
- GUI Tkinter minimalista con tema dark (340x290).
- Limitador de ancho de banda **PC-wide** vía WinDivert (kernel-level).
- Token bucket con burst configurable (20 ms por defecto).
- AQM (Active Queue Management): drop si un paquete necesitaría > 30 ms de espera, evitando bufferbloat severo.
- Auto-elevación UAC al lanzar el ejecutable.
- Auto-detección y reparación del servicio WinDivert (error 1058).
- Opción "Iniciar con Windows" vía Task Scheduler (sin prompt UAC al login).
- Indicador en tiempo real del rate de descarga/subida (MB/s).
- Filtrado automático de tráfico LAN (`10.x`, `172.16-31.x`, `192.168.x`) y loopback.
- Build script `build.bat` que crea `dist\Limitador.exe` autónomo (onefile, windowed).
- Build script `build_installer.bat` que genera el wizard `Setup_Limitador_1.0.0.exe` con Inno Setup.
- Script `fix-service.bat` para reparación manual del driver.
- Generación automática de `Limitador.ico` multi-resolución desde `Limiter_icon.png`.

### Distribución
- Instalador wizard con Inno Setup 6.x (idiomas Español + English).
- Ejecutable portable standalone (`Limitador.exe`).

### Soporte
- Windows 10 / 11 (64-bit).
- IPv4 únicamente.

[1.0.0]: https://github.com/Nol3/Internet_Limiter/releases/tag/v1.0.0
