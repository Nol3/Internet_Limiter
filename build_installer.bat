@echo off
REM Build Limitador.exe + Setup_Limitador.exe (instalador wizard)
REM Requiere: Python 3.10+ en PATH, Inno Setup 6.x instalado
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo ============================================
echo   Limitador - Build completo + Instalador
echo ============================================
echo.

REM ============================================================
REM PASO 1: Compilar el ejecutable
REM ============================================================
echo [PASO 1/3] Compilando Limitador.exe...
echo.
call build.bat
echo.

if not exist "dist\Limitador.exe" (
    echo [ERROR] build.bat no generó dist\Limitador.exe. Abortando.
    goto :end
)

echo [OK] Limitador.exe compilado correctamente.
echo.

REM ============================================================
REM PASO 2: Crear carpeta de salida del instalador
REM ============================================================
echo [PASO 2/3] Preparando directorio de salida...
if not exist "installer_output" mkdir "installer_output"
echo [OK] Carpeta installer_output lista.
echo.

REM ============================================================
REM PASO 3: Compilar el instalador con Inno Setup
REM ============================================================
echo [PASO 3/3] Compilando instalador wizard...
echo.

REM Buscar Inno Setup en rutas comunes
set ISCC=
set IS_PATHS=^
    "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"^
    "%ProgramFiles%\Inno Setup 6\ISCC.exe"^
    "%ProgramFiles(x86)%\Inno Setup 5\ISCC.exe"^
    "%ProgramFiles%\Inno Setup 5\ISCC.exe"

for %%P in (%IS_PATHS%) do (
    if exist %%P (
        set ISCC=%%P
        goto :found_iscc
    )
)

REM No encontrado - buscar en PATH
where iscc >nul 2>&1
if not errorlevel 1 (
    set ISCC=iscc
    goto :found_iscc
)

echo [ERROR] Inno Setup no encontrado.
echo.
echo Para crear el instalador wizard, instala Inno Setup 6:
echo   https://jrsoftware.org/isdl.php
echo.
echo El ejecutable ya fue compilado en:
echo   %cd%\dist\Limitador.exe
echo.
echo Puedes distribuir ese .exe directamente sin el instalador.
goto :end

:found_iscc
echo [*] Inno Setup encontrado: %ISCC%
echo [*] Compilando installer.iss...
echo.

%ISCC% installer.iss

if errorlevel 1 (
    echo.
    echo [ERROR] Inno Setup falló al compilar el instalador.
    echo         Revisa los errores arriba.
    goto :end
)

echo.
if exist "installer_output\Setup_Limitador_1.0.0.exe" (
    echo ============================================
    echo   [OK] Todo compilado exitosamente
    echo ============================================
    echo.
    echo   EXE standalone:  %cd%\dist\Limitador.exe
    echo   Instalador:      %cd%\installer_output\Setup_Limitador_1.0.0.exe
    echo.
    echo   Distribuye el instalador para una experiencia profesional.
    echo   O distribuye dist\Limitador.exe directamente (portable).
    echo ============================================
) else (
    echo [WARN] El instalador se compiló pero no se encontró en installer_output\.
    echo        Revisa la carpeta manualmente.
)

:end
echo.
pause
endlocal
