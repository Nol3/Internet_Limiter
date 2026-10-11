@echo off
REM ===========================================================================
REM  Limitador - compila dist\Limitador.exe
REM
REM  Usa Limitador.spec (onefile, sin consola, manifiesto UAC admin, driver
REM  WinDivert incluido como datos). Necesita Python 3.10+ (o uv).
REM ===========================================================================
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo ============================================
echo   Limitador - compilar ejecutable
echo ============================================
echo.

set PYEXE=.venv\Scripts\python.exe

REM --- 1. Entorno virtual -----------------------------------------------------
if not exist "%PYEXE%" (
    where uv >nul 2>&1
    if not errorlevel 1 (
        echo [*] Creando entorno virtual con uv...
        uv venv .venv --python 3.13
    ) else (
        where python >nul 2>&1
        if errorlevel 1 (
            echo [ERROR] No encuentro ni uv ni python en el PATH.
            echo         Instala Python 3.10+ desde https://www.python.org/
            goto :end
        )
        echo [*] Creando entorno virtual con python -m venv...
        python -m venv .venv
    )
    if not exist "%PYEXE%" (
        echo [ERROR] No se pudo crear el entorno virtual.
        goto :end
    )
)

"%PYEXE%" --version
echo.

REM --- 2. Dependencias --------------------------------------------------------
echo [*] Instalando dependencias (requirements-dev.txt)...
where uv >nul 2>&1
if not errorlevel 1 (
    uv pip install --python "%PYEXE%" -r requirements-dev.txt
) else (
    "%PYEXE%" -m pip install --upgrade pip
    "%PYEXE%" -m pip install -r requirements-dev.txt
)
if errorlevel 1 (
    echo [ERROR] Fallo instalando dependencias.
    goto :end
)
echo.

REM --- 3. Icono ---------------------------------------------------------------
if exist "Limiter_icon.png" (
    echo [*] Generando Limitador.ico desde Limiter_icon.png...
    "%PYEXE%" make_icon.py
    if errorlevel 1 echo [WARN] make_icon.py fallo; se usara el icono existente.
)
echo.

REM --- 4. Comprobaciones antes de empaquetar ---------------------------------
echo [*] Ejecutando las pruebas...
"%PYEXE%" -m pytest tests -q
if errorlevel 1 (
    echo [ERROR] Hay pruebas en rojo: no se empaqueta.
    goto :end
)
echo.

echo [*] Validando los filtros de captura contra el driver...
"%PYEXE%" -m limitador.cli --check >nul 2>&1
echo     (el resultado completo esta en: Limitador.exe --cli --check)
echo.

REM --- 5. Empaquetado --------------------------------------------------------
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"

echo [*] Compilando con PyInstaller (1-2 minutos)...
"%PYEXE%" -m PyInstaller --clean --noconfirm Limitador.spec
if errorlevel 1 (
    echo [ERROR] PyInstaller fallo. Revisa el log de arriba.
    goto :end
)
echo.

if not exist "dist\Limitador.exe" (
    echo [ERROR] No se genero dist\Limitador.exe.
    goto :end
)

REM --- 6. Acceso directo ------------------------------------------------------
echo [*] Creando acceso directo en la raiz del proyecto...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$s = (New-Object -ComObject WScript.Shell).CreateShortcut('%~dp0Limitador.lnk');" ^
  "$s.TargetPath = '%~dp0dist\Limitador.exe';" ^
  "$s.WorkingDirectory = '%~dp0dist';" ^
  "$s.IconLocation = '%~dp0dist\Limitador.exe,0';" ^
  "$s.Description = 'Limitador de ancho de banda';" ^
  "$s.Save()"

REM --- 7. Verificacion del ejecutable ----------------------------------------
echo.
echo [*] Verificando el ejecutable (diagnostico sin privilegios)...
"dist\Limitador.exe" --cli --check

echo.
echo ============================================
echo   [OK] Compilado
echo ============================================
echo   EXE:       %cd%\dist\Limitador.exe
echo   Acceso:    %cd%\Limitador.lnk
echo.
echo   Para el instalador: build_installer.bat
echo ============================================

:end
echo.
pause
endlocal
