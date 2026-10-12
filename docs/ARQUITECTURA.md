# Arquitectura de Limitador 2.0

Este documento explica **por qué** el motor está construido así. Cada decisión viene de una
medición hecha en hardware real (Windows 11 25H2, x64, «Integridad de memoria» activada), no de
una suposición.

---

## 1. El problema: limitar tráfico en Windows sin escribir un driver

Windows no ofrece ninguna API de usuario para regular la tasa de un proceso. Las opciones reales
son tres:

| Vía | Coste | Veredicto |
|-----|-------|-----------|
| QoS del router | No es por proceso, requiere tocar el router | Descartada (el objetivo es «doble clic y listo») |
| Driver WFP propio (lo que hace NetLimiter) | Driver kernel firmado (certificado EV, ~300 €/año) | Descartada para un proyecto libre |
| **WinDivert** (driver LGPL ya firmado) | Ninguno; intercepta paquetes y los devuelve desde el proceso | **Elegida** |

WinDivert funciona así: abres un descriptor con un filtro; el driver te **entrega** los paquetes
que coinciden y **espera** a que los reinyectes. Ese «espera» es la clave del diseño y también su
mayor peligro: mientras un paquete está en tus manos, no está en la red.

Consecuencia directa: **un fallo del proceso que no reinyecta equivale a quedarse sin internet**.
Todo lo que sigue está condicionado por esa realidad.

---

## 2. Filtros: el lenguaje que no es lo que parece

El filtro se compila en el driver y decide qué paquetes se interceptan. Dos cosas aprendidas a
golpes, verificadas con `WinDivertHelperCompileFilter` y `WinDivertHelperEvalFilter`:

1. **`not` no puede preceder a un paréntesis.** `not (ip)` es un error de sintaxis; `not ip` y
   `(expr)` por separado sí valen. Es decir, negar un grupo exige aplicar De Morgan a mano.
2. **No hay CIDR ni `!=` en direcciones.** Los rangos se escriben con `>=`/`<=` sobre la dirección
   completa.

Y una tercera, de lógica: en una app «de todo el equipo» solo importa la **parte remota** del
paquete. La dirección local de un paquete saliente es siempre privada (192.168.x, 10.x,
172.16-31.x), así que filtrarla descarta el 100 % del tráfico de internet. Por eso:

```
salida:  outbound and not loopback and (no <ip.DstAddr> en rangos locales)
entrada: inbound  and not loopback and (no <ip.SrcAddr> en rangos locales)
```

`limitador/filters.py` construye esos filtros y `validate_filter()` los compila **antes** de
abrir ningún descriptor: un filtro roto aborta el arranque con un mensaje claro en vez de dejar
la red a oscuras.

---

## 3. El planificador de tasa

### Lo que no funciona

- **Token bucket con sueño en el bucle de captura**: mientras duerme no se llama a `recv()`, la
  cola del driver se llena y la tasa oscila.
- **Dormir por paquete**: en Windows cada `time.sleep` cuesta ~0,5 ms *más* de lo pedido
  (medido: `sleep(0.05 ms)` → 0,50 ms; `sleep(2 ms)` → 2,50 ms; `sleep(10 ms)` → 10,2 ms). A
  26 MB/s cada paquete de 1500 B debe esperar 56 µs, así que dormir por paquete daba **10 MB/s
  reales** de 26 configurados.

### Lo que sí funciona

Un **planificador por fecha de salida** con reloj virtual:

```
salida = max(ahora, salida_anterior) + tamaño / tasa
espera = salida - ahora
```

Propiedades:

- la tasa a largo plazo es exacta, independientemente del patrón de llegada;
- si un sueño se pasa de largo, la deuda queda registrada y los paquetes siguientes salen antes:
  el error no se acumula;
- las esperas por debajo de `MIN_SLEEP_SECONDS` (10 ms) **no se duermen**: se acumulan en el reloj
  virtual y se pagan de una vez. Así el sobrecoste de ~0,5 ms se reparte entre ~175 paquetes en
  lugar de pagarse en cada uno;
- cada sueño pide `SLEEP_OVERHEAD_SECONDS` (0,5 ms) menos, compensando el sobrecoste medido.

Resultado medido con `tools/pacing_check.py` (tiempo real, 1,5 s por objetivo):

| Objetivo | Conseguido | Relación |
|----------|-----------|----------|
| 30 MB/s | 30,00 MB/s | 100,0 % |
| 10 MB/s | 10,00 MB/s | 100,0 % |
| 3 MB/s | 3,00 MB/s | 100,0 % |
| 0,5 MB/s | 0,50 MB/s | 100,2 % |

### El techo de 9 MB/s

`pydivert.send()` recalcula por defecto las sumas de comprobación de IP/TCP/UDP, y en Windows esa
implementación es **Python puro** (su ruta nativa solo se usa fuera de Windows): 60-150 µs por
paquete, lo que limitaba todo el pipeline a ~9 MB/s sin importar el límite configurado. Como los
paquetes no se modifican, sus sumas ya son válidas: se reinyectan con
`send(packet, recalculate_checksum=False)`.

Coste actual del camino por paquete, medido: análisis + decisión = **8,5 µs** (≈162 MB/s por
núcleo), construcción del objeto `Packet` = 1,55 µs. El cuello de botella vuelve a ser el driver,
no el código.

---

## 4. AQM: descartar sin destrozar

El presupuesto de retardo (`retardo_máx`, 50 ms por defecto) define cuánto puede crecer la cola:
`presupuesto = max(tasa × retardo_máx, 2 MTU)`.

Dos reglas, y el orden importa:

1. **Nunca descartar en un enlace ocioso.** Un paquete más grande que todo el presupuesto (posible
   con límites muy bajos) se retrasa, no se descarta: descartarlo bloquearía la conexión para
   siempre en vez de hacerla lenta. El mínimo de 2 MTU evita que un límite bajo descarte casi todo.
2. **Descartar solo cuando hay cola delante**, comparando `bytes_en_cola + tamaño` con el
   presupuesto, y **en el punto de encolado** (drop-tail), no al desencolar. Descartar al
   desencolar es un error sutil: el paquete ya salió de la cola, así que la cola se vacía
   descartando en vez de enviando y el flujo se queda a cero.

En la prueba de aceptación real, limitando a 9,12 MB/s desde una línea de 27 MB/s, el AQM
descartó 5,16 MB en 5 s y el tráfico limitado salió al 92-97 % del objetivo.

---

## 5. Por aplicación: puerto local → proceso

Windows no dice «este paquete es de Chrome». Se deduce:

- Cada paquete tiene un puerto local (origen en salida, destino en entrada).
- `GetExtendedTcpTable` / `GetExtendedUdpTable` (iphlpapi) enumeran todos los extremos abiertos
  con su PID propietario. Se refresca cada segundo en un hilo aparte.
- Las capas de **eventos** de WinDivert dan la atribución exacta y en tiempo real:
  - `FLOW` entrega los eventos `BOUND`/`LISTEN`/`ESTABLISHED`/`DELETED` con `ProcessId`,
    `LocalPort` y `Protocol`. Es la vía pensada para esto, así que se intenta primero.
  - `SOCKET` es la alternativa clásica (`BIND`/`CONNECT`/`ACCEPT`/`CLOSE`).
  - **Hay que abrirlas en modo solo lectura (`WINDIVERT_FLAG_RECV_ONLY`)**: estas capas no
    pueden devolver nada al driver, y sin ese flag `WinDivertOpen` responde error 87
    («parámetro no válido»). Ese error fue durante meses la razón de creer que la capa no
    existía en este equipo; `tools\probe_layers.py` lo aclaró probando capa x flags.
  - Se prueban en cascada (FLOW antes que SOCKET, con y sin `SNIFF`) y el registro anota
    cuál se consiguió abrir; la pestaña *Aplicaciones* lo muestra.
  - Si ninguna abre, la atribución cae en la tabla de conexiones, que se refresca cada segundo
    (hasta ~1 s de retraso para flujos nuevos). El límite global no se ve afectado en ningún caso.
  - Los eventos solo rellenan huecos: en cuanto la tabla conoce el puerto, manda ella. Un evento
    `DELETED`/`CLOSE` borra la entrada, para no atribuir un puerto reutilizado al proceso anterior.

La clave del planificador es el **nombre del ejecutable**, no el PID: un PID cambia en cada
arranque, así que las reglas se guardan por ruta normalizada (`os.path.normcase`) y se resuelven a
planificadores independientes por dirección. Así el límite de Steam sobrevive a reiniciar Steam.

---

## 6. Fail-open: que nunca te dejen sin internet

Cinco capas, de dentro hacia fuera:

1. **El camino por paquete no puede lanzar excepciones.** `classify.inspect_safe()` devuelve `None`
   ante cualquier sorpresa y el paquete se reinyecta tal cual. Un `try/except` envuelve además la
   decisión completa: si algo falla, el paquete se reenvía igual.
2. **Autocomprobación antes de capturar.** `classify.self_test()` analiza paquetes sintéticos IPv4
   e IPv6 como `memoryview`, `bytes` y `bytearray` (que es el tipo que entrega pydivert) y verifica
   puertos y direcciones. Si falla, no se abre ningún descriptor.
3. **Vigilante.** Un hilo comprueba cada 0,5 s que los hilos de captura e inyección siguen vivos;
   si alguno murió, **libera la captura** (cierra los descriptores) y lo avisa.
4. **Parada que vacía la cola.** Al detener se reinyecta todo lo que estaba en cola antes de cerrar
   los descriptores; cerrarlos con paquetes dentro los descartaría y cortaría conexiones vivas.
5. **Recuperación manual.** Si el proceso muere, Windows cierra sus descriptores y la red vuelve
   sola. Si no vuelve: Administrador de tareas → cerrar `Limitador.exe`, o `tools\unblock.cmd`
   (que elimina el servicio del driver).

La prueba de flujo con descriptor falso (`tests/test_engine_flow.py`) reproduce el escenario
peligroso sin arriesgar la conexión: simula un analizador que revienta, una decisión que revienta y
un hilo de captura muerto, y verifica en los tres casos que ningún paquete se queda atrapado.

---

## 7. Hilos y estado

```
captura (1 por dirección)   recv -> clasificar -> reinyectar ya, o encolar
inyección (1 por dirección) desencolar -> planificar -> dormir -> send
eventos (opcional, 1)       eventos de las capas FLOW/SOCKET -> mapa puerto->PID
procmap (1)                 refresca la tabla de conexiones cada 1 s
vigilante (1)               comprueba que los hilos siguen vivos
```

- Un solo hilo inyecta por dirección: así el orden por flujo se conserva y no hay contención.
- Los contadores son enteros que escribe **un único** hilo; la interfaz calcula tasas por
  diferencias entre dos instantáneas, sin candados en el camino caliente.
- Los planificadores se tocan desde el hilo de inyección (planificar) y desde la interfaz
  (cambiar tasa): un `RLock` por planificador. El candado global de planificadores es reentrante
  a propósito (`_sync_shapers` llama a `_shaper_for` con el candado tomado; con un `Lock` normal
  el programa se colgaba).
- El reloj del sistema se sube a 1 ms (`timeBeginPeriod(1)`) mientras se limita, para que los
  sueños de milisegundos sean fiables.

### Rendimiento medido (y su techo)

| Medición | Resultado |
|----------|-----------|
| Reenvío sin límites (mismo hilo, sin cola) | ~82 MB/s |
| Pipeline con el descriptor falso (cola + hilos, sin driver) | 30 MB/s al 100 % del objetivo |
| Reenvío **con límite activo** (dos hilos sobre el mismo descriptor) | ~12 MB/s |

El pipeline de Python no es el cuello (sostiene 30 MB/s sin driver), así que la diferencia está
en hablar con el driver a la vez desde el hilo de captura (`WinDivertRecv`) y el de inyección
(`WinDivertSend`) sobre el **mismo descriptor**: el driver serializa ambas operaciones. El
siguiente paso identificado es abrir un descriptor propio para inyectar (marcando los paquetes
propios como *impostor* en el filtro de captura para que no se recapturen).

Aparte, cuando el objetivo está muy por debajo de la línea, recortar exige descartar el exceso y
TCP reacciona bajando su ritmo: la velocidad final puede quedar por debajo del objetivo sin que
el limitador deje de aplicar el límite (se ve en `tools\rate_probe.py` y lo reporta como nota la
prueba de aceptación).

---

## 8. Empaquetado e instalación

- **PyInstaller onefile** (`Limitador.spec`): sin consola, icono, `pystray`/PIL incluidos y el
  driver de pydivert como **datos** (`collect_data_files("pydivert", includes=["windivert_dll/*"])`).
- **Sin manifiesto `requireAdministrator`**: así `--cli --check`, `--list-apps` y `--version`
  funcionan sin UAC; la app pide elevación ella misma al abrir la interfaz (y avisa si se cancela).
- **Driver estable**: al arrancar se copian `WinDivert64.dll` y `WinDivert64.sys` a
  `%ProgramData%\Limitador\driver`, se apunta `pydivert.windivert_dll.DLL_PATH` ahí **antes** de
  abrir el primer descriptor, y se restringen los permisos con `icacls` usando SIDs
  (`*S-1-5-18`, `*S-1-5-32-544`, `*S-1-5-32-545`). Cargar un driver kernel desde una carpeta
  escribible por cualquier usuario sería una escalada de privilegios.
- **Inno Setup**: crea esa carpeta con permisos, registra la tarea programada con `/RL HIGHEST` si
  el usuario lo pide, y al desinstalar borra tarea, servicio del driver y datos.
- **Integridad de memoria (HVCI)**: activada en este equipo. El driver de WinDivert 1.4 (que traen
  los pydivert antiguos) no carga con HVCI; el 2.2.2 de `pydivert` 4 sí. De ahí el pin de versión.

---

## 9. Verificación

| Nivel | Herramienta | Necesita admin |
|-------|-------------|----------------|
| Unidad | `pytest tests` (102 pruebas: planificador, filtros, clasificación, reglas, procmap, motor con descriptor falso) | No |
| Filtros reales | `tests/test_filters.py` compila y **evalúa** los filtros con las funciones del propio driver | No |
| Precisión | `tools/pacing_check.py` | No |
| Extremo a extremo | `tools/acceptance_test.py` (fases 0-3 en red real) | Sí |
| Producto congelado | `Limitador.exe --cli --check` / `--list-apps` | No |

Las fases de la prueba de aceptación van en orden de riesgo creciente: primero comprobar que
capturar **no** rompe la conexión, luego medir la línea, y solo después aplicar límites. Además
lleva un vigilante propio y el lanzador mata el árbol de procesos si se pasa de tiempo: morir
cierra los descriptores y la red vuelve.
