@echo off
REM Diagnóstico en vivo: ejecuta el limitador por consola durante N segundos y guarda
REM la salida. Se lanza con privilegios desde tools\live_check.ps1.
setlocal
chcp 65001 >nul
cd /d "%~dp0.."
set OUT=%~dp0live_output.txt
echo ==== live check %DATE% %TIME% ==== > "%OUT%"
".venv\Scripts\python.exe" -u "main.py" --cli --down 26 --up 26 --duration 40 --interval 1 >> "%OUT%" 2>&1
echo EXITCODE=%ERRORLEVEL% >> "%OUT%"
endlocal
