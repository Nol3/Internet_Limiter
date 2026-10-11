# Demostración

Guion para enseñar el limitador funcionando en unos 3 minutos. **Todo lo que aparece aquí son
mediciones reales**: el programa descarga de verdad, mide y compara con el objetivo. No hay
datos inventados ni capturas retocadas (las capturas de la interfaz se generan con
`--screenshot`, ver más abajo).

---

## Preparación (una sola vez)

- Windows 10/11 x64.
- El instalador (`Setup_Limitador_2.0.0.exe`) o el ejecutable portable (`Limitador.exe`).
- Conexión a internet: la demostración descarga unos 150 MB de un servidor público de pruebas.
- Nada más. No hace falta Python, ni el driver aparte, ni configurar nada.

> En un equipo nuevo, Windows puede mostrar el aviso de **SmartScreen** («Windows protegió tu
> PC») porque el programa no está firmado digitalmente: *Más información → Ejecutar de todas
> formas*. Y el antivirus puede marcar el driver WinDivert como falso positivo.

---

## Guion

### 1 · Instalación (30 s)

1. Doble clic en `Setup_Limitador_2.0.0.exe` → asistente → **Instalar** (pide permisos de
   administrador: los necesita el driver de captura).
2. Deja marcado *«Iniciar Limitador automáticamente con Windows»* si quieres enseñar que
   arranca solo, sin aviso de UAC.
3. Abre Limitador. Acepta el aviso de UAC de la primera ejecución.

> Si prefieres no instalar nada: `Limitador.exe` portable hace exactamente lo mismo desde
> cualquier carpeta o un USB.

### 2 · Demostración automática (90 s) ← *la parte que impresiona*

En una consola (o doble clic en el Explorador):

```bat
tools\demo.cmd
```

Pide UAC una vez y hace tres mediciones seguidas, en directo:

1. **Referencia**: descarga sin limitador → la velocidad real de la línea.
2. **Límite global** de 5 MB/s → la descarga se frena a ~5 MB/s.
3. **Límite solo a `curl.exe`** de 2 MB/s → se frena a ~2 MB/s *sin tocar el resto del equipo*.

Al terminar imprime una tabla como esta (números de la prueba de aceptación, en la misma
máquina y con la misma línea):

```
==============================================================
RESULTADO DE LA DEMOSTRACIÓN
==============================================================
Referencia (sin limitador)                  27.36 MB/s
Límite global (9.12 MB/s)                    8.84 MB/s    97% del objetivo  OK
    (lo que aplica el planificador)          9.12 MB/s
Límite por aplicación (9.12 MB/s)            8.40 MB/s    92% del objetivo  OK
    (lo que aplica el planificador)          9.12 MB/s
==============================================================
RESULTADO: OK
```

Puntos que conviene decir en voz alta mientras corre:

- Mientras descarga, la línea `el limitador ve X MB/s` es lo que el propio motor está contando
  paquete a paquete.
- La tercera fase demuestra la diferencia con NetLimiter de verdad: **el límite es por
  aplicación**, no para todo el equipo.

### 3 · A mano, en la interfaz (60 s)

| Paso | Qué hacer | Qué se ve |
|------|-----------|-----------|
| 1 | Pestaña **General** → límite de bajada `5` MB/s → **Aplicar límites globales** → **Iniciar limitación** | El gráfico empieza a dibujar y la velocidad baja |
| 2 | Abre el navegador y descarga algo grande | En **Aplicaciones**, `chrome.exe`/`msedge.exe` sube en la lista con su velocidad |
| 3 | Selecciona el navegador → **Limitar seleccionada…** → `2` MB/s | La fila se marca como *limitada* y el navegador se frena solo |
| 4 | **Pausar** y **Detener** | La red vuelve al instante a su velocidad normal |

Las capturas de cada pestaña están en [`docs/img/`](docs/img) y en el
[README](README.md#la-interfaz).

### 4 · La prueba de aceptación (opcional, para quien dude)

```bat
tools\run_elevated.ps1     :: fases 0-3, incluye una fase de control sin límites
```

Mide además el *forwarding* puro (captura activa sin limitar), que debe dar exactamente la
misma velocidad que sin limitador: es la prueba de que el motor no pierde paquetes.

---

## Qué mirar y qué puede fallar

| Síntoma | Explicación |
|---------|-------------|
| La referencia sale más lenta que el límite | Tu conexión va más despacio que el límite configurado: usa `--limit 1` para que se note la diferencia. |
| «Ningún espejo de pruebas responde» | Los servidores públicos de *speed test* fallan o te limitan por IP. Espera un rato o usa `--url <otro-espejo>`. |
| El límite por aplicación tarda ~1 s en notarse | La atribución por proceso usa la tabla de conexiones (se refresca cada segundo). El límite global no tiene ese retardo. |
| La velocidad real es un 3-8 % menor que el objetivo | Normal: cabeceras TCP/IP y retransmisiones cuentan como bytes pero no como datos útiles. |
| Se queda sin internet | No debería: al morir el proceso se liberan los descriptores y la red vuelve. Si aun así pasa: `tools\unblock.cmd` (como administrador). Ver el README. |

### Parámetros útiles de la demo

```bat
.venv\Scripts\python.exe tools\demo.py --seconds 20 --limit 10 --app-limit 1
.venv\Scripts\python.exe tools\demo.py --only base      :: solo mide la línea (no necesita admin)
.venv\Scripts\python.exe tools\demo.py --json informe.json
```

---

## Generar las capturas de la interfaz

```bat
Limitador.exe --screenshot docs\img --demo
```

Abre la interfaz, guarda una imagen de cada pestaña en `docs\img\` y sale. Con `--demo` usa
límites de ejemplo **solo en memoria** (no guarda nada en tu configuración) para que las
imágenes muestren la interfaz con límites puestos. No arranca el motor: no toca el tráfico.

Sirve además como verificación: si la interfaz se construye entera y las capturas salen bien
(no en negro), la app empaquetada funciona.

---

## Última ejecución de referencia

<!-- Medición real, máquina del autor (Windows 11 25H2, línea de ~27 MB/s). -->

| Fase | Objetivo | Medido | % del objetivo |
|------|----------|--------|----------------|
| Referencia sin limitador | — | 27,36 MB/s | — |
| Límite global | 9,12 MB/s | 8,84 MB/s | 97 % |
| Límite por aplicación (`curl.exe`) | 9,12 MB/s | 8,40 MB/s | 92 % |
| Captura activa sin límites (control) | — | HTTP 200 en 0,68 s | sin pérdidas |

Precisión del planificador medida aparte (`tools\pacing_check.py`): 30 MB/s → 100,0 %,
10 MB/s → 100,0 %, 3 MB/s → 100,0 %, 0,5 MB/s → 100,2 %.

Estos números salen de `tools\acceptance_test.py` (fases 0-3). `tools\demo.cmd` mide lo mismo
pero en versión corta y con límites más bajos por defecto (5 y 2 MB/s).
