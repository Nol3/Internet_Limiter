# Publicar una versión

Guía para sacar una release de Limitador.

## 1. Preparar el código

1. Comprobar que `limitador/rules.py` tiene la versión correcta (`APP_VERSION`), que
   `installer.iss` (`MyAppVersion`) y el `CHANGELOG.md` coinciden.
2. Pasar las pruebas y las comprobaciones que no necesitan permisos:

   ```bat
   .venv\Scripts\python.exe -m pytest tests -q
   .venv\Scripts\python.exe tools\pacing_check.py
   ```

3. Prueba de aceptación en red real (pide UAC, descarga unos cientos de MB):

   ```bat
   powershell -NoProfile -ExecutionPolicy Bypass -File tools\run_elevated.ps1 -Seconds 5
   ```

   Debe terminar con `RESULTADO: OK`. Si dice `MEDICIÓN NO VÁLIDA`, el espejo dejó de servir
   datos: repite la prueba más tarde (Cloudflare, por ejemplo, limita por IP si se abusa).

## 2. Compilar

```bat
build_installer.bat
```

Genera:

- `dist\Limitador.exe` — portable (~20 MB).
- `installer_output\Setup_Limitador_<versión>.exe` — instalador.

`build.bat` se niega a empaquetar si alguna prueba falla, y al final ejecuta
`dist\Limitador.exe --cli --check` para verificar el ejecutable congelado.

## 3. Probar el instalador

En una máquina (o usuario) limpia:

1. Instalar y comprobar que pide elevación, crea accesos directos y registra el driver.
2. `Limitador.exe --cli --check` debe dar todo en verde.
3. Limitar una descarga real y comprobar la tasa.
4. Desinstalar: no deben quedar ni la tarea programada `Limitador`, ni el servicio `WinDivert`,
   ni `%ProgramData%\Limitador\driver`.

## 4. Publicar

```bat
git tag -a v<versión> -m "Limitador <versión>"
git push origin main --tags
```

Crear la release en GitHub y adjuntar:

- `installer_output\Setup_Limitador_<versión>.exe` (recomendado para usuarios finales)
- `dist\Limitador.exe` (portable)

En las notas de la release, resumir el `CHANGELOG.md` y recordar:

- Se necesitan permisos de administrador (driver de kernel).
- Algunos antivirus marcan WinDivert como sospechoso: es un falso positivo.
- Cómo recuperar la conexión si algo va mal (`tools\unblock.cmd`, ver README).
