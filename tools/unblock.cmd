@echo off
REM ---------------------------------------------------------------------------
REM  Limitador - liberacion de emergencia
REM
REM  Si el limitador se quedo colgado y notas que no tienes internet, ejecuta
REM  este archivo con el boton derecho > "Ejecutar como administrador".
REM  Elimina el servicio del driver WinDivert, con lo que el trafico de red
REM  vuelve a fluir de inmediato (sin limites).
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0.."

echo Liberando la captura de red de Limitador...
echo.

if exist "dist\Limitador.exe" (
    "dist\Limitador.exe" --cli --unblock
) else if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" main.py --cli --unblock
) else (
    echo [ERROR] No encuentro ni dist\Limitador.exe ni .venv\Scripts\python.exe
    echo         Ejecuta este script desde la carpeta del proyecto.
)

echo.
pause
endlocal
