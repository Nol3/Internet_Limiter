@echo off
REM Lanza la prueba de aceptación con privilegios (se usa desde run_elevated.ps1).
setlocal
chcp 65001 >nul
cd /d "%~dp0.."
set OUT=%~dp0acceptance_output.txt
echo ==== prueba de aceptacion %DATE% %TIME% ==== > "%OUT%"
".venv\Scripts\python.exe" -u "tools\acceptance_test.py" %* >> "%OUT%" 2>&1
echo EXITCODE=%ERRORLEVEL% >> "%OUT%"
endlocal
