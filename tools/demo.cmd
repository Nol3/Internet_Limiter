@echo off
REM Demostracion guiada del limitador: tres mediciones reales con descargas de verdad.
REM Pide UAC una sola vez. El resultado queda en tools\demo_output.txt
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_elevated.ps1" ^
    -Script demo.py -ScriptArgs "--seconds 12" -TimeoutSec 420
if errorlevel 1 pause
