# Avisos de terceros

Limitador es Apache License 2.0 (ver [LICENSE](../LICENSE)), pero **distribuye y carga** software
de terceros. Este archivo cumple con la obligación de atribución.

## WinDivert (LGPL v3)

- **Qué es**: el driver de kernel que intercepta y reinyecta paquetes de red.
- **Autor**: Basil (reqrypt.org).
- **Licencia**: GNU Lesser General Public License v3 (dual con GPL v2).
- **Cómo se usa**: Limitador **no modifica** WinDivert. Se distribuyen los binarios sin cambios
  (`WinDivert64.dll` y `WinDivert64.sys`, versión 2.2.2) tal y como los empaqueta `pydivert`, se
  copian a `%ProgramData%\Limitador\driver` en la primera ejecución y se cargan como servicio
  bajo demanda.
- **Fuentes**: <https://reqrypt.org/windivert.html> (descarga `WinDivert-2.2.2-A.zip`, que incluye
  fuentes y el texto completo de la licencia).
- **Texto de la licencia**: <https://www.gnu.org/licenses/lgpl-3.0.html>

Cualquier redistribución de Limitador debe mantener este aviso y ofrecer el mismo acceso a las
fuentes de WinDivert indicado arriba.

## pydivert (LGPL v3)

- **Qué es**: bindings de Python sobre WinDivert.
- **Autor**: Fabio Falcinelli y Maximilian Hils.
- **Licencia**: LGPL-3.0-or-later o GPL-2.0-or-later.
- **Fuentes**: <https://github.com/ffalcinelli/pydivert>

## Pillow (MIT-CMU)

- **Qué es**: manipulación de imágenes; genera el icono multi-resolución y el icono de bandeja.
- **Licencia**: MIT-CMU. **Fuentes**: <https://python-pillow.org/>

## pystray (LGPL v3)

- **Qué es**: icono de la bandeja del sistema.
- **Autor**: Moses Palmér.
- **Licencia**: LGPL v3. **Fuentes**: <https://github.com/moses-palmer/pystray>

## PyInstaller (GPL v2 con excepción de arranque)

- **Qué es**: empaqueta la aplicación en un ejecutable.
- **Nota**: la excepción de arranque permite distribuir el ejecutable generado bajo la licencia
  que elijas (Apache 2.0 en este caso).
- **Fuentes**: <https://pyinstaller.org/>

## Inno Setup (licencia propia permisiva)

- **Qué es**: genera el instalador. **Fuentes**: <https://jrsoftware.org/isinfo.php>

---

El instalador y el ejecutable incluyen los binarios de WinDivert y pydivert; el código fuente de
ambos está disponible en los enlaces de arriba, sin modificaciones por parte de este proyecto.
