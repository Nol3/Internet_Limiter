# Auditoría del proyecto Internet_Limiter (Limitador v1.0.0)

Fecha: 2026-10-11 · Auditoría hecha ejecutando el código real contra el DLL de WinDivert 2.2.2
(pydivert 4.0.0) y la API de Windows de esta máquina (Windows 11 build 26200, x64,
**Integridad de memoria (HVCI) activada**).

## Resumen ejecutivo

El proyecto **no limita tráfico: nunca ha funcionado**. Los dos fallos críticos están en la
construcción del filtro de WinDivert (`sniffer.py`), y el síntoma se diagnosticó mal como
"servicio WinDivert deshabilitado" (de ahí `fix-service.bat`).

## Hallazgos críticos (bloquean la función principal)

### C1 — El filtro WinDivert no compila (error de sintaxis)

`sniffer.py:48-58` construye el filtro con la forma:

```
outbound and ip and not loopback and not (ip.SrcAddr >= 10.0.0.0 and ...)
```

Verificado contra `WinDivertHelperCompileFilter` (el propio compilador del driver):

```
FAIL | outbound and ip and not loopback and not (ip.SrcAddr >= 10.0.0.0 ... | pos=41 | Filter expression parse error
```

En el lenguaje de filtros de WinDivert 2.2.2 **`not` no puede preceder a un paréntesis**
(`not (x)` → error de parseo; `not x` y `(x)` por separado sí son válidos):

```
OK   | not ip
FAIL | not (ip)                      |
OK   | ip and (not tcp)              |   <- única forma válida de negar un grupo
```

Consecuencia: `WinDivertOpen` falla siempre, ninguna captura arranca, y `main.py:181` interpreta
el fallo como "servicio deshabilitado" y ofrece `sc delete` del driver. Es decir: el usuario
"repara" un servicio que no está roto y la app sigue sin limitar nada. El `>=`/`<=` sí es válido.

### C2 — La lógica de exclusión de LAN es de Morgan al revés: excluiría *todo* el tráfico

Aun con el filtro bien escrito, la condición del proyecto es:

```
not (ip.SrcAddr >= 10.0.0.0 and ip.SrcAddr <= 10.255.255.255)   # intención: "no es LAN"
```

La intención es "excluir si la dirección está en el rango", que equivale a
`(addr < lo) or (addr > hi)`, **no** a `not lo <= addr and not addr <= hi`. Con la forma
`and not ... and not ...` toda dirección posible falla una de las dos condiciones
(p. ej. `8.8.8.8`: `>= 10.0.0.0` es falso y `<= 10.255.255.255` es verdadero) → el filtro
**no coincidiría con ningún paquete**. Probado con `WinDivertHelperEvalFilter` sobre paquetes
sintéticos: 7 de 17 casos con la semántica esperada fallaban; el 100% del tráfico de internet
quedaba descartado.

### C3 — Para tráfico saliente se filtra el lado equivocado

El filtro de salida comprueba `ip.SrcAddr` (la IP local, que **siempre** es privada: 192.168.x,
10.x, 172.16-31.x) y el de entrada comprueba `ip.DstAddr` (la IP local, también privada).
En una app PC-wide lo que importa es la dirección de la **parte remota**:

- saliente → `ip.DstAddr` es el remoto.
- entrante → `ip.SrcAddr` es el remoto.

Filtrar el lado local descarta todo el tráfico propio de internet.

## Hallazgos altos (rompen la instalación en otros equipos)

### A1 — El driver vive en una carpeta temporal que se borra

pydivert 4.0.0 trae WinDivert 2.2.2 y carga el driver desde
`.venv/Lib/site-packages/pydivert/windivert_dll/WinDivert64.dll` + `WinDivert64.sys`
(`DLL_PATH` está fijado en `pydivert/windivert_dll/__init__.py:185`). WinDivert registra el
servicio `WinDivert` apuntando al `.sys` que está **junto al DLL**.

En el `.exe` onefile de PyInstaller el paquete se extrae a
`%TEMP%\_MEIxxxx\pydivert\windivert_dll\` y esa carpeta **se borra al cerrar la app**. Resultado:
el servicio `WinDivert` queda apuntando a una ruta inexistente y la siguiente ejecución falla
con 1058 / "no se puede encontrar el archivo". Es exactamente el fallo que `fix-service.bat`
intenta parchear a mano. Solución: copiar DLL+SYS a una ruta estable
(`%ProgramData%\Limitador\driver`) y apuntar `DLL_PATH` ahí antes de abrir el primer handle.

### A2 — Dependencias sin fijar y API cambiada

`requirements.txt` dice `pydivert>=2.1.0`; hoy resuelve **4.0.0** (driver 2.2.2, API nueva).
pydivert 2.1 empaqueta WinDivert **1.4**, que no carga con Integridad de memoria (HVCI)
activada — activada en este equipo (`...HypervisorEnforcedCodeIntegrity\Enabled = 1`).
Instalaciones reproducibles requieren pin, y el mensaje de error debe decir la verdad cuando
el driver no arranca.

### A3 — El "autostart" se niega a funcionar en modo dev y no se autodesinstala del todo

`autostart.enable()` devuelve error en modo dev (`sys.frozen` falso); con `pythonw.exe + main.py`
funcionaría igual. Y `is_enabled()` da por bueno cualquier tarea llamada `Limitador` aunque
apunte a una ruta antigua.

### A4 — Sin instancia única, sin bandeja, y el cierre puede perder paquetes

- Abrir dos instancias crea dos capturas WinDivert del mismo tráfico (doble limitación impredecible).
- No hay icono de bandeja: cerrar la ventana mata el limitador sin avisar.
- `BandwidthLimiter.stop()` cierra los handles sin `join()`; los paquetes en cola del driver se
  descartan en el cierre (corte breve de tráfico).

## Hallazgos medios (calidad y precisión)

### M1 — El "token bucket" no limita la tasa real de forma correcta

`limiter.py` descuenta tokens y devuelve `wait = deficit/rate`, durmiendo ese tiempo
*bloqueando el bucle de captura*. Mientras duerme no llama a `recv()`, así que la cola del driver
(llena con la ráfaga que llega en ese tiempo) desborda y la tasa efectiva oscila. Además los
tokens pueden quedar negativos sin acotar la deuda, de modo que el "burst" real no es el
configurado. Verificado en la suite nueva: sustituido por un planificador de fecha de salida
(*deadline scheduler*) con presupuesto de retardo duro.

### M2 — Se tira (drop) tráfico entrante como método principal de limitación

Descartar paquetes entrantes hace que el emisor retransmita: se desperdicia ancho de banda y
sube la latencia. Un shaper debe **retardar** con una cola acotada y descartar solo como último
recurso (AQM). Implementado con hilo inyector separado + presupuesto de retardo.

### M3 — No hay límites por aplicación

El objetivo declarado es "una versión simple y gratuita de NetLimiter", y NetLimiter es
*por aplicación*. WinDivert 2.2 sí lo permite: capa `SOCKET` entrega eventos con `processId`,
`localPort` y `protocolo` (verificado que compila: `event == CONNECT`, `BIND`, `LISTEN`,
`ACCEPT`, `CLOSE` y `processId == N`), y `GetExtendedTcpTable`/`GetExtendedUdpTable` completan el
mapa puerto→PID. Con eso se puede limitar y monitorizar por proceso.

### M4 — IPv6 ignorado y excepciones incompletas

El README lo admite ("IPv4 únicamente"), pero además el filtro no excluía broadcast
(255.255.255.255 → DHCP), ni link-local (169.254/16), ni multicast (224/4), y no había
comprobación de que el filtro compilase antes de arrancar (un error de sintaxis se reportaba
como "driver deshabilitado").

### M5 — Errores del hilo no visibles en la GUI y `time.sleep(0.5)` en el hilo de Tk

`main.py:169` duerme medio segundo en el hilo de la interfaz para "ver errores", y la detección
de fallo se basa en leer atributos privados (`_thread_errors`, `_in_thread`).

### M6 — Precio de no limitar lo que no se debe: la app no excluye tráfico de control

DNS, ICMP y los ACK puros (paquetes sin payload) no deberían pasar por el shaper: retardarlos
multiplica la latencia de establecimiento de conexión sin aportar nada al control de tasa.

## Lo que está bien (se mantiene)

- Idea general: WinDivert como motor de interceptación (es la vía correcta sin driver propio firmado).
- Auto-elevación UAC al arrancar (`ShellExecuteW runas`).
- Uso de Task Scheduler con `/RL HIGHEST` para no pedir UAC en cada login.
- Empaquetado onefile/windowed + Inno Setup y generación de icono multi-resolución.
- Tema oscuro ttk y estructura de textos en español.

## Veredicto sobre "cambiar de lenguaje"

No hace falta. El fallo no era de Python: era un filtro inválido y una idea equivocada de cómo
funciona WinDivert. Una reescritura en C#/WPF daría una GUI más vistosa (Tkinter es lo que es),
pero no más capacidad, y perdería la facilidad de leer/modificar el código que este proyecto
tiene como objetivo de aprendizaje. Se mantiene Python + PyInstaller + Inno Setup, con el motor
rediseñado y verificado por tests.
