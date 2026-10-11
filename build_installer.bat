@echo off
REM ===========================================================================
REM  Limitador - compila el ejecutable + el instalador (Inno Setup 6.x)
REM  Requiere: Python 3.10+ (o uv) y Inno Setup 6.x instalado.
REM ===========================================================================
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo ============================================
echo   Limitador - ejecutable + instalador
echo ============================================
echo.

echo [PASO 1/2] Compilando Limitador.exe...
call build.bat
if not exist "dist\Limitador.exe" (
    echo [ERROR] build.bat no genero dist\Limitador.exe. Abortando.
    goto :end
)
echo.

echo [PASO 2/2] Compilando el instalador con Inno Setup...
set ISCC=
for %%P in (
    "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
    "%ProgramFiles%\Inno Setup 6\ISCC.exe"
    "%ProgramFiles(x86)%\Inno Setup 5\ISCC.exe"
    "%ProgramFiles%\Inno Setup 5\ISCC.exe"
) do (
    if exist %%P (
        set ISCC=%%P
        goto :found_iscc
    )
)
where iscc >nul 2>&1
if not errorlevel 1 (
    set ISCC=iscc
    goto :found_iscc
)

echo [ERROR] Inno Setup no encontrado.
echo         Instalalo desde https://jrsoftware.org/isdl.php
echo.
echo         El ejecutable portable ya esta listo en dist\Limitador.exe
goto :end

:found_iscc
echo [*] Inno Setup: %ISCC%
if not exist "installer_output" mkdir "installer_output"
%ISCC% installer.iss
if errorlevel 1 (
    echo [ERROR] Inno Setup fallo al compilar installer.iss.
    goto :end
)

echo.
echo ============================================
echo   [OK] Todo compilado
echo ============================================
echo   Portable:    %cd%\dist\Limitador.exe
for %%F in ("installer_output\*.exe") do echo   Instalador:  %%F
echo ============================================

:end
echo.
pause
endlocal
