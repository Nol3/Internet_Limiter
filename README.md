# Limitador

> **Bajar los ms y estabilizar el ping en juegos competitivos.** Ese es el motivo del proyecto.
>
> El limitador es la herramienta, no el objetivo. Tu tráfico de fondo (descargas, copias,
> streaming, actualizaciones) llena la cola de tu enlace y eso se siente como *bufferbloat*: el
> ping sube, hay tirones, el hit no registra. Limitando el fondo y dando **prioridad absoluta al
> juego**, tu tráfico sensible no espera en esa cola.
>
> Limitador de ancho de banda para Windows: **por aplicación** o para **todo el equipo**.
> Una alternativa simple, libre y gratuita a NetLimiter. Sin tocar el router, sin drivers
> propios firmados, sin cuentas ni telemetría.

![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-blue)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-Apache%202.0-green)
![Version](https://img.shields.io/badge/version-2.0.0-brightgreen)
![CI](https://github.com/Nol3/Internet_Limiter/actions/workflows/ci.yml/badge.svg)

**¿Quieres verlo funcionando?** → [DEMO.md](DEMO.md): guion de 3 minutos con mediciones reales.

---

## Qué hace

- **Límite por aplicación**: limita la bajada/subida de un programa concreto (Chrome, Steam,
  un backup…) y deja el resto del equipo sin tocar.
- **Límite global**: limita todo el tráfico del equipo de una vez.
- **Monitor en vivo**: qué aplicaciones están usando la red y a qué velocidad, en MB/s.
- **Modo juego**: presupuesto de retardo corto (20 ms) para que el ping no se dispare, más
  **prioridad absoluta** para las aplicaciones que marques: su tráfico no pasa por la cola.
- **Medición de latencia**: ping real (ICMP) a tu servidor de juego, con medio, pico y jitter,
  para comprobar el efecto en lugar de suponerlo.
- **Latencia controlada (AQM)**: no llena la cola del enlace; el retardo añadido está acotado
  por un presupuesto configurable (50 ms por defecto) en lugar de provocar *bufferbloat*.
- **Reglas persistentes**: las reglas por aplicación se guardan y se aplican solas.
- **Bandeja del sistema, inicio con Windows (sin UAC), modo consola, diagnóstico.**

## Para juegos: menos ms, más estables

El *bufferbloat* es el ping que sube cuando algo llena tu enlace. No se nota en la velocidad
(que sigue siendo alta) sino en los milisegundos, y en un juego competitivo se siente como
tirones o como que el hit no registra. La causa no es el router: es que **tu propio tráfico de
fondo** ocupa la cola del enlace y tu paquete del juego espera detrás.

El proyecto nació para eso, y lo atacan dos piezas:

1. **Modo juego** (pestaña *General*, o `--game-mode`): baja el presupuesto de retardo de 50 ms a
   20 ms. La cola del enlace no puede crecer: lo que sobra se descarta antes (AQM) en lugar de
   acumularse esperando. Menos cola = menos ping.
2. **Prioridad absoluta** (pestaña *Aplicaciones* → *Prioridad (no limitar)*, o `--exempt RUTA`):
   el juego marcado así **no pasa por el planificador ni por el límite global**. Su tráfico sale
   en cuanto llega, aunque otra aplicación esté descargando a tope.

### Compruébalo con tus números

```bat
:: apuntando a TU servidor de juego (no al de otro)
powershell -NoProfile -ExecutionPolicy Bypass -File tools\run_elevated.ps1 ^
    -Script latency_probe.py -ScriptArgs "--host 1.1.1.1 --seconds 6"
```

Mide el ping en cuatro situaciones con una descarga de fondo real: en reposo, con carga sin
limitador, con carga y modo juego, y con carga y descriptores separados. Te dice cuántos ms
añade o quita cada configuración, en lugar de pedirte que te lo creas.

### Lo que se midió aquí (y lo que obligó a corregir)

| situación | ping medio | respecto al reposo |
|---|---|---|
| en reposo | 17 ms | — |
| con carga, **sin** limitador | 17 ms | +0 ms |
| con carga, modo juego (un descriptor) | 73 ms | **+57 ms** |
| con carga, modo juego (descriptores separados) | 112 ms | **+95 ms** |

Dos conclusiones honestas de esta medición en esta línea:

- **Esta línea no tenía bufferbloat** con esa descarga (17 → 17 ms): el recorte del modo juego no
  era lo que hacía falta aquí, y sin embargo…
- **el limitador añadía 57-95 ms de ping bajo carga**. Es decir, hacía lo contrario de lo que
  promete el proyecto. La causa: el inyector se despertaba **una vez por paquete** (~15.000
  despertares/s con una descarga a 20 MB/s) y ese tira y afloja del GIL le robaba CPU al hilo que
  debe reenviar al instante tu tráfico sensible.

Corregido en esta versión: la cola avisa solo al pasar de vacía a tener algo y el inyector drena
en ráfaga (`MAX_INJECT_BURST`), contando el AQM los bytes en vuelo. La medida con descriptores
separados (peor: +95 ms) confirma además que esa vía no era el camino y quedó descartada.

### Verificado en hardware real

Prueba de aceptación automatizada (`tools/acceptance_test.py`), sobre una línea medida en dos
muestreos (arranque y sostenida) para que un espejo que estrangula no se confunda con un fallo:

| Fase | Resultado |
|------|-----------|
| Captura activa sin límites | petición HTTP 200 en 0,66 s, tráfico reinyectado al 100 % |
| Línea sin límite | 31,06 MB/s de arranque · 26,51 MB/s sostenida |
| Límite global de 8,84 MB/s | **7,65 MB/s (87 % del objetivo)**, 2,6 % de descartes propios |
| Límite solo a `curl.exe` de 8,84 MB/s | **6,09 MB/s medidos por el archivo, 9,28 MB/s por el motor** |
| Atribución por proceso | **capa FLOW** de WinDivert activa (instantánea) |
| Captura liberada por seguridad | no |

Precisión del planificador medida en tiempo real (`tools/pacing_check.py`):
30 MB/s → 100,0 %, 10 MB/s → 100,0 %, 3 MB/s → 100,0 %, 0,5 MB/s → 100,2 %.
El pipeline de Python sostiene 30 MB/s con el descriptor falso (100 %, ver `tests/`).

Para repetirlo en tu equipo: `tools\demo.cmd` (tres mediciones reales) o
`tools\run_elevated.ps1` (prueba completa, fases 0-3). `tools\rate_probe.py` mide la velocidad
real con varias tasas y presupuestos de retardo; `tools\probe_layers.py` dice qué capas de
WinDivert acepta tu equipo.

### La interfaz

Capturas generadas por la propia aplicación (`Limitador.exe --screenshot docs\img --demo`):

| General | Aplicaciones |
|---------|--------------|
| ![Pestaña General](docs/img/gui-general.png) | ![Pestaña Aplicaciones](docs/img/gui-aplicaciones.png) |
| Límite de todo el equipo, en caliente | Qué app consume qué **y cuánto lleva consumido**, con su límite propio |

| Avanzado | Registro |
|----------|----------|
| ![Pestaña Avanzado](docs/img/gui-avanzado.png) | ![Pestaña Registro](docs/img/gui-registro.png) |
| Retardo, tráfico de control, IPv6, bandeja, inicio | Todo lo que hace el motor, en texto |

*(En las capturas se usan límites de ejemplo en memoria para que se vea la interfaz en uso.)*

---

## Instalación (usuario final)

En la página de [Releases](../../releases/latest) hay dos opciones:

### Opción 1 · Instalador (recomendado)

1. Descarga `Setup_Limitador_2.0.0.exe`.
2. Doble clic y sigue el asistente (pide permisos de administrador: los necesita el driver).
3. Acceso directo en el escritorio y en el menú Inicio.
4. Opcional: marca «Iniciar Limitador automáticamente con Windows» (tarea programada con
   privilegios altos, sin aviso de UAC en cada arranque).

### Opción 2 · Portable

1. Descarga `Limitador.exe` (≈20 MB).
2. Cópialo donde quieras (USB, carpeta personal…).
3. Doble clic: la app pide elevación ella misma cuando la necesita.

No hace falta Python ni nada extra: el driver WinDivert viaja dentro del ejecutable y se
instala en `%ProgramData%\Limitador\driver` la primera vez.

> **Si Windows muestra «Windows protegió tu PC» (SmartScreen)**: el programa no está firmado
> digitalmente (un certificado de firma de código cuesta dinero). Pulsa **Más información →
> Ejecutar de todas formas**. Es el mismo aviso que dan todas las aplicaciones libres sin
> firmar; el código está completo en este repositorio y el driver es WinDivert, el mismo que
> usan Wireshark o GlassWire.
>
> Algunos antivirus marcan el ejecutable o el driver (falso positivo típico con WinDivert):
> añade una exclusión para `Limitador.exe` y `%ProgramData%\Limitador\driver`.

---

## Uso

1. Abre Limitador y acepta el aviso de UAC (solo la primera vez de cada ejecución).
2. Pulsa **Iniciar limitación**.
3. Pestaña **General**: define el límite de todo el equipo (bajada/subida). `0` = sin límite.
   Hay botones rápidos (1/5/10/25/50/100 MB/s).
4. Pestaña **Aplicaciones**: lista en vivo de las apps con conexiones. Selecciona una y pulsa
   **Limitar seleccionada…** para darle su propio límite de bajada/subida.
5. Pestaña **Avanzado**: presupuesto de retardo, no limitar tráfico de control (ACK/DNS/ICMP),
   excluir la red local, limitar IPv6, cerrar a la bandeja, inicio con Windows.
6. Cerrar la ventana (con la bandeja activada) deja el limitador funcionando en segundo plano.

Los límites se aplican **en caliente**: no hay que reiniciar la captura para cambiarlos.

### Modo consola

Útil para scripts, servidores o para comprobar que todo funciona:

```bat
Limitador.exe --cli --check                 :: diagnóstico completo (no limita nada)
Limitador.exe --cli --list-apps             :: qué apps usan la red ahora mismo
Limitador.exe --cli --down 5 --up 1 --duration 30
Limitador.exe --cli --app "C:\Program Files\App\app.exe=2:0.5" --duration 60
Limitador.exe --cli --down 5 --json informe.json     :: informe en JSON
Limitador.exe --repair-driver               :: reinstala el driver (necesita admin)
```

En modo consola imprime la velocidad conseguida frente al objetivo, la cola pendiente y los
bytes descartados, y termina con un resumen (media, pico, % del objetivo, descartes).

---

## Cómo funciona

| Componente | Papel |
|------------|-------|
| **WinDivert 2.2.2** (vía `pydivert` 4) | Intercepta paquetes IPv4/IPv6 a nivel de kernel |
| **Planificador por fecha de salida** | Calcula cuándo puede salir cada paquete (`depart = max(ahora, anterior) + tamaño/tasa`) |
| **AQM con presupuesto de retardo** | Descarta el sobrante cuando la cola supera `tasa × retardo_máx` (y **nunca** descarta en un enlace ocioso) |
| **Atribución puerto → proceso** | Capa de eventos **FLOW** de WinDivert (solo lectura) con la capa **SOCKET** y la tabla de conexiones TCP/UDP como respaldo |
| **Filtros validados** | El filtro se compila con el compilador de WinDivert antes de capturar nada |
| **Fail-open** | Si un hilo del motor muere, la captura se libera automáticamente para no dejar sin internet |

El diseño completo, con las decisiones y las trampas encontradas, está en
[docs/ARQUITECTURA.md](docs/ARQUITECTURA.md).

### Limitaciones conocidas (honestas)

- **Solo Windows 10/11 x64.** El driver y la API de atribución de procesos son de Windows.
- **El tráfico de la red local no se limita** (10.x, 172.16-31.x, 192.168.x, multicast,
  broadcast): es deliberado, para no romper la red de casa. Se puede desactivar en Avanzado.
- **Atribución por proceso**: el motor prueba primero la capa **FLOW** de WinDivert (entrega el
  proceso de cada flujo en cuanto se establece), después la capa **SOCKET** clásica y, si ninguna
  abre en tu equipo, la tabla de conexiones (refresco de 1 s). Solo en ese último caso un flujo
  recién abierto puede tardar hasta ~1 s en quedar bajo su límite por aplicación; el límite global
  no se ve afectado. La pestaña *Aplicaciones* indica qué fuente está en uso, y el registro dice
  cuál se consiguió abrir.
- **No inspecciona contenido** (no es un firewall ni un filtro de URLs): solo mide y regula tasa.
- **El techo de velocidad real depende de la línea.** Medido: con objetivos por debajo de un tercio
  de la línea, el tráfico limitado sale al 87-100 % del objetivo. Recortar una línea **muy rápida**
  (medido en una de ~75 MB/s) exige descartar el exceso, y TCP responde bajando su ritmo: ahí la
  velocidad final se queda alrededor de ~12 MB/s aunque se pida más. No es que el limitador deje de
  aplicar el límite (lo aplica: el tráfico no pasa a velocidad de línea y los descartes propios son
  pequeños), es el emisor el que se frena. Medido con `tools\rate_probe.py`.
- **El techo de velocidad no está ni en la lógica ni en el descriptor.** Medido: el pipeline de
  Python sostiene 30 MB/s con el descriptor falso (sin driver), y separar captura e inyección en
  dos descriptores **no cambia nada** (`tools\ab_handles.py`, misma sesión y mismo espejo: ambos
  modos dan 2-5 MB/s con una línea de 40 MB/s). La precisión del límite depende de cuánto más
  rápida sea la línea que el objetivo: con la línea a ~26 MB/s y un objetivo de ~9 (un tercio) el
  tráfico limitado sale al **87-105 %** del objetivo, mientras que con líneas de 40-75 MB/s y
  objetivos de 8-22 MB/s la velocidad final se queda en unos pocos MB/s porque el emisor se frena
  al perder los paquetes que hay que descartar. En el rango de uso habitual (recortar una conexión
  a unos pocos MB/s) el limitador es fiable; para líneas muy rápidas queda por probar el camino de
  los ACK (retrasarlos en vez de descartar datos), que es lo que hacen los limitadores comerciales.
  Medido con `tools\rate_probe.py` y `tools\ab_handles.py`.
- Un paquete TCP sin carga (ACK puro), DNS e ICMP no se limitan por defecto: retardarlos sube
  la latencia sin controlar nada. Se puede desactivar en Avanzado.
- No hay límites por «grupo de aplicaciones» ni por interfaz de red (todavía).

---

## Solución de problemas

### El antivirus marca el ejecutable o el driver

Falso positivo habitual con WinDivert. Añade una exclusión para `Limitador.exe` y para
`%ProgramData%\Limitador\driver`.

### «El driver WinDivert no está disponible (error 1058/1060)»

Pulsa **Avanzado → Reparar driver**, o ejecuta `Limitador.exe --repair-driver` como
administrador. Esto reinstala el registro del servicio apuntando a
`%ProgramData%\Limitador\driver`.

### **Me he quedado sin internet** (recuperación de emergencia)

Ocurre solo si el proceso del limitador muere *mientras* tiene paquetes retenidos. Al morir el
proceso, Windows cierra sus descriptores y la red vuelve sola; si aun así no vuelve:

1. **Cierra la ventana de Limitador** (o, si está en la bandeja, botón derecho → *Salir*). Al
   cerrarse, el motor detiene la captura y la red vuelve al instante.
2. Si la ventana no responde: Administrador de tareas **abierto como administrador** (Limitador
   corre elevado, así que un Administrador de tareas normal no puede terminarlo y dirá «Acceso
   denegado») → finalizar `Limitador.exe`.
3. Si nada de lo anterior funciona: `tools\unblock.cmd` con el botón derecho → *Ejecutar como
   administrador* (equivale a `Limitador.exe --cli --unblock`). Elimina el servicio del driver y
   la red vuelve de inmediato.

El motor incluye tres protecciones para que esto no pase: el camino por paquete no puede
lanzar excepciones, un vigilante libera la captura si un hilo se detiene, y la prueba de
aceptación se autodestruye si se pasa de tiempo.

### La velocidad real es menor que el límite configurado

Normal: cabeceras TCP/IP y retransmisiones cuentan como bytes pero no como datos útiles.
Configura el límite un 5-10 % por encima de lo que quieras conseguir. En las pruebas
automatizadas el tráfico limitado sale al 92-97 % del objetivo.

### Latencia alta al limitar

Baja el *presupuesto de retardo* en Avanzado (p. ej. 20-30 ms). Más bajo = menos bufferbloat,
pero también más descartes cuando la aplicación pide más de lo permitido.

### «Iniciar con Windows» no funciona

Comprueba en la pestaña Avanzado que la casilla está marcada y que la tarea existe:

```bat
schtasks /Query /TN Limitador
```

---

## Compilar desde el código

Requisitos: Windows 10/11, Python 3.10+ (o `uv`), y para el instalador Inno Setup 6.x.

```bat
build.bat             :: crea el venv, instala deps, pasa los tests y compila dist\Limitador.exe
build_installer.bat   :: lo anterior + installer_output\Setup_Limitador_2.0.0.exe
```

`build.bat` se niega a empaquetar si alguna prueba está en rojo.

### Desarrollo

```bat
uv venv .venv --python 3.13
uv pip install --python .venv\Scripts\python.exe -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest tests -q          :: 102 pruebas, sin driver ni admin
.venv\Scripts\python.exe -m ruff check .             :: análisis estático (0 avisos)
.venv\Scripts\python.exe -m ruff format --check .    :: formato
.venv\Scripts\python.exe main.py                     :: app en modo desarrollo (auto-eleva)
.venv\Scripts\python.exe main.py --screenshot docs\img --demo   :: capturas de la interfaz
```

El mismo control corre en cada push mediante GitHub Actions (`.github/workflows/ci.yml`):
ruff, las 102 pruebas, el ejecutable y el instalador.

### Herramientas de verificación

| Herramienta | Qué hace | Necesita admin |
|-------------|----------|----------------|
| `tools\demo.cmd` | Demostración guiada: 3 mediciones reales (referencia, global, por aplicación) | Pide UAC |
| `tools\pacing_check.py` | Precisión del planificador en tiempo real | No |
| `tools\acceptance_test.py` | Prueba de aceptación completa en la red real (fases 0-3) | Sí |
| `tools\run_elevated.ps1` | Lanza cualquier script de `tools\` con UAC y lo mata si se pasa de tiempo | Pide UAC |
| `tools\live_check.ps1` | Limitador en consola + tráfico real, para diagnóstico en vivo | Pide UAC |
| `tools\unblock.cmd` | Recuperación de emergencia de la red | Sí |

Y desde el propio ejecutable:

```bat
Limitador.exe --screenshot docs\img --demo   :: captura cada pestaña (documentación/verificación)
```

---

## Estructura del proyecto

```
Internet_Limiter/
├── limitador/
│   ├── engine.py       # captura WinDivert + hilos de inyección + vigilante
│   ├── shaper.py       # planificador de tasa (fecha de salida) + AQM + medidores
│   ├── classify.py     # lectura rápida de cabeceras, decisión por paquete, autotest
│   ├── procmap.py      # atribución puerto local -> proceso
│   ├── filters.py      # construcción y validación de filtros WinDivert
│   ├── driver.py       # instalación estable del driver, reparación, diagnóstico
│   ├── rules.py        # configuración y reglas por aplicación (JSON)
│   ├── gui.py          # interfaz Tkinter + bandeja del sistema
│   ├── cli.py          # modo consola con medición
│   ├── app.py          # arranque: logging, instancia única, elevación
│   ├── autostart.py    # tarea programada de inicio
│   └── units.py, console.py
├── tests/              # 102 pruebas (planificador, filtros, clasificación, motor con handle falso)
├── tools/              # demo guiada, verificación y recuperación
├── docs/               # arquitectura, auditoría de la v1, capturas de la interfaz (img/)
├── .github/workflows/  # integración continua: ruff + pruebas + ejecutable + instalador
├── DEMO.md             # guion de demostración (3 minutos)
├── main.py             # punto de entrada
├── Limitador.spec      # receta de PyInstaller
├── pyproject.toml      # configuración de ruff y pytest
├── build.bat / build_installer.bat / installer.iss
└── requirements.txt / requirements-dev.txt
```

---

## Historia

La versión 1.0.0 **no limitaba nada**: el filtro de captura no compilaba (usaba
`not (... )`, que el lenguaje de filtros de WinDivert no acepta) y, además, su lógica de
exclusión de LAN era incorrecta, de modo que habría descartado todo el tráfico. El fallo se
confundía con un «servicio deshabilitado». El análisis completo, con las pruebas que lo
demuestran, está en [docs/AUDITORIA_v1.md](docs/AUDITORIA_v1.md). La 2.0.0 es una reescritura
del motor, verificada en red real.

## Licencia

[Apache License 2.0](LICENSE). Terceros: ver [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)
(WinDivert es LGPL v3).

## Créditos

- [WinDivert](https://reqrypt.org/windivert.html) — driver de captura de paquetes.
- [pydivert](https://github.com/ffalcinelli/pydivert) — bindings de Python.
- [PyInstaller](https://pyinstaller.org/) — empaquetado.
- [Inno Setup](https://jrsoftware.org/isinfo.php) — instalador.
- [pystray](https://github.com/moses-palmer/pystray) + [Pillow](https://python-pillow.org/) —
  icono de bandeja.
