# Changelog

Todas las versiones notables de este proyecto se documentan en este archivo.

El formato sigue [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/),
y este proyecto adhiere a [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [2.0.0] — 2026-10-12

Reescritura del motor. La 1.0.0 no limitaba nada: su filtro de captura no compilaba y su
lógica de exclusión de LAN era incorrecta (ver [docs/AUDITORIA_v1.md](docs/AUDITORIA_v1.md)).
Esta versión se ha verificado contra la red real.

### Corregido

- **Filtro de captura inválido**: `not (...)` no existe en el lenguaje de filtros de WinDivert;
  ahora la negación de rangos se construye con la ley de De Morgan aplicada a mano
  (`(not x >= lo or not x <= hi)`) y **el filtro se compila antes de capturar nada**.
- **Exclusión de LAN invertida**: se filtraba la dirección local (siempre privada), lo que
  descartaba todo el tráfico de internet; ahora solo se comprueba la parte **remota**
  (destino en salida, origen en entrada).
- **Driver en carpeta temporal**: el `.exe` onefile cargaba el driver desde `%TEMP%\_MEIxxxx`,
  que se borra al cerrar; el servicio quedaba apuntando a un archivo inexistente (error 1058).
  Ahora el driver se copia a `%ProgramData%\Limitador\driver`, con permisos restringidos a
  administradores, y las instalaciones obsoletas se limpian solas.
- **Precisión de la tasa**: el planificador dormía por paquete, y en Windows cada `time.sleep`
  cuesta ~0,5 ms más de lo pedido: 26 MB/s configurados daban 10 MB/s reales. Ahora los
  retardos pequeños se acumulan en el reloj virtual y se pagan en un solo sueño, y se compensa
  el sobrecoste medido (30 MB/s → 100,0 %).
- **Techo de ~9 MB/s**: `pydivert.send()` recalcula las sumas de comprobación **en Python puro**
  en Windows (~60-150 µs por paquete). Al no modificar los paquetes, ahora se reinyectan sin
  recalcularlas.
- **Tasa exacta**: sustituido el token bucket con deuda ilimitada por un planificador por fecha
  de salida; la tasa no se supera nunca y el retardo añadido queda acotado.
- **Sin descartes en enlace ocioso**: un paquete más grande que el presupuesto se retrasa en vez
  de descartarse (descartarlo bloquearía la conexión con límites muy bajos).

### Añadido

- **Límites por aplicación** (lo que hace útil a un NetLimiter): atribución puerto local → proceso
  con la tabla de conexiones TCP/UDP, capa de sockets de WinDivert cuando está disponible, y
  planificador independiente por aplicación.
- **Monitor por aplicación** en vivo (bajada/subida por proceso) y lista de apps con conexiones.
- **Fail-open garantizado**: el camino por paquete no puede lanzar excepciones; un hilo vigilante
  libera la captura si un hilo del motor muere; la cola se vacía al detener en lugar de descartarse.
- **Autocomprobación del analizador** antes de abrir el primer descriptor: un analizador roto
  aborta el arranque en vez de dejar sin internet.
- **Modo consola** con medición (`--cli --down/--up/--app/--duration/--json`, `--check`,
  `--list-apps`) y **recuperación de emergencia** (`--unblock`, `tools\unblock.cmd`).
- **Bandeja del sistema** (mostrar, pausar/reanudar, salir), **instancia única**, arranque en
  segundo plano (`--minimized`).
- **Reglas persistentes** en `%APPDATA%\Limitador\config.json` y aplicación en caliente.
- **Diagnóstico** copiable al portapapeles y `--repair-driver`.
- **102 pruebas** que no necesitan driver ni privilegios, incluida una prueba de flujo
  extremo a extremo con un descriptor falso que reproduce el escenario de «hilo muerto con
  paquetes retenidos».
- **Prueba de aceptación** en red real (`tools\acceptance_test.py`), comprobación de precisión
  (`tools\pacing_check.py`) y vigilante que mata la prueba si se pasa de tiempo.
- **Demostración guiada** (`tools\demo.cmd`): tres mediciones reales (referencia sin límite,
  límite global y límite por aplicación) con tabla comparativa y veredicto. Guion en
  [DEMO.md](DEMO.md).
- **Capturas de la interfaz desde la propia aplicación** (`Limitador.exe --screenshot CARPETA
  [--demo]`): recorre las pestañas, guarda una imagen de cada una y sale. Sirve además como
  verificación de que la interfaz se construye entera en el ejecutable empaquetado. Las
  imágenes del README se generan así.
- **Integración continua** (`.github/workflows/ci.yml`): análisis estático, las 102 pruebas y
  la compilación del ejecutable y del instalador en cada push.
- **Análisis estático con ruff** (`pyproject.toml`) en cero avisos, y formateo con `ruff format`.

### Cambiado

- Inicio con Windows también en modo desarrollo (`pythonw.exe main.py`), no solo con el `.exe`.
- El ejecutable ya no lleva un manifiesto `requireAdministrator`: la app pide elevación ella
  misma cuando la necesita, así que `--cli --check` y `--list-apps` funcionan sin UAC.
- Empaquetado por `Limitador.spec` (reproducible) en lugar de una línea de `pyinstaller`.
- Dependencias fijadas (`pydivert==4.0.0`, que trae WinDivert 2.2.2, compatible con la
  «Integridad de memoria» de Windows 11).
- El instalador limpia la tarea programada, el registro del driver y la carpeta del driver al
  desinstalar. Los ajustes del usuario (`%APPDATA%\Limitador\config.json`) se conservan a
  propósito.

### Eliminado

- `sniffer.py`, `limiter.py` y `autostart.py` de la raíz (sustituidos por `limitador/engine.py`,
  `limitador/shaper.py` y `limitador/autostart.py`).
- `fix-service.bat` (sustituido por `Limitador.exe --repair-driver` y `tools\unblock.cmd`).

### Limitaciones conocidas

- La capa de sockets de WinDivert devuelve error 87 en Windows 11 25H2 con este driver, así que
  la atribución por proceso usa la tabla de conexiones (refresco de 1 s).
- El tráfico de la red local no se limita por defecto.
- Solo Windows x64.

---

## [1.0.0] — 2026-05-04

Primera release pública. **No funcionaba**: el filtro de captura no compilaba
(`not (...)`), de modo que el driver no llegaba a capturar nada, y el fallo se
interpretaba como «servicio WinDivert deshabilitado». Ver
[docs/AUDITORIA_v1.md](docs/AUDITORIA_v1.md).

### Añadido

- GUI Tkinter con tema oscuro y auto-elevación UAC.
- Limitador PC-wide con WinDivert y token bucket con descarte (AQM) si el retardo superaba 30 ms.
- Opción «Iniciar con Windows» vía Task Scheduler y script `fix-service.bat`.
- Build con PyInstaller onefile e instalador Inno Setup 6.x (español e inglés).
- Generación de `Limitador.ico` multi-resolución desde `Limiter_icon.png`.

[2.0.0]: https://github.com/Nol3/Internet_Limiter/releases/tag/v2.0.0
[1.0.0]: https://github.com/Nol3/Internet_Limiter/releases/tag/v1.0.0
