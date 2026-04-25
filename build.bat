@echo off
REM Build standalone Limitador.exe (UAC admin baked in, no terminal)
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo ============================================
echo   Limitador - Build script
echo ============================================
echo.

REM --- Check Python ---
where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python no encontrado en PATH.
    echo         Instala Python 3.10+ desde https://www.python.org/
    goto :end
)

python --version
echo.

REM --- Create venv if missing ---
if not exist ".venv\Scripts\activate.bat" (
    echo [*] Creando entorno virtual...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Fallo creando venv.
        goto :end
    )
)

REM --- Activate venv ---
call ".venv\Scripts\activate.bat"
if errorlevel 1 (
    echo [ERROR] No se pudo activar venv.
    goto :end
)

echo [*] Actualizando pip...
python -m pip install --upgrade pip
echo.

echo [*] Instalando dependencias...
pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Fallo instalando requirements.txt
    goto :end
)
echo.

echo [*] Instalando PyInstaller...
pip install pyinstaller
if errorlevel 1 (
    echo [ERROR] Fallo instalando PyInstaller.
    goto :end
)
echo.

REM --- Clean previous build ---
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"
if exist "Limitador.spec" del /q "Limitador.spec"

REM --- Generate Limitador.ico from PNG (needs Pillow) ---
if exist "Limiter_icon.png" (
    echo [*] Generando Limitador.ico...
    python make_icon.py
    if errorlevel 1 (
        echo [WARN] make_icon.py fallo. Build continua sin icono custom.
    )
)

echo [*] Compilando ejecutable (puede tardar 1-2 min)...
echo.
set ICON_ARGS=
if exist "Limitador.ico" (
    set ICON_ARGS=--icon Limitador.ico --add-data "Limitador.ico;."
)
pyinstaller --onefile --windowed --uac-admin --collect-all pydivert %ICON_ARGS% --name Limitador --clean --noconfirm main.py

echo.
if exist "dist\Limitador.exe" (
    echo [*] Creando acceso directo en raiz del proyecto...
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
      "$s = (New-Object -ComObject WScript.Shell).CreateShortcut('%~dp0Limitador.lnk');" ^
      "$s.TargetPath = '%~dp0dist\Limitador.exe';" ^
      "$s.WorkingDirectory = '%~dp0dist';" ^
      "$s.IconLocation = '%~dp0dist\Limitador.exe,0';" ^
      "$s.Description = 'Limitador de ancho de banda PC-wide';" ^
      "$s.Save()"

    if exist "Limitador.lnk" (
        echo [OK] Acceso directo: %cd%\Limitador.lnk
    ) else (
        echo [WARN] No se pudo crear acceso directo ^(puedes usar dist\Limitador.exe^).
    )

    echo.
    echo ============================================
    echo   [OK] Build completo
    echo ============================================
    echo   EXE:       %cd%\dist\Limitador.exe
    echo   Shortcut:  %cd%\Limitador.lnk
    echo   Doble-click en el shortcut. UAC se solicita automaticamente.
    echo ============================================
) else (
    echo [ERROR] Build fallo. Revisa el log de PyInstaller arriba.
)

:end
echo.
pause
endlocal
