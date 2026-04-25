@echo off
REM Arregla error "WinError 1058: servicio deshabilitado".
REM Ejecutar como administrador (click derecho -> Ejecutar como admin).

echo [*] Parando servicio WinDivert...
sc stop WinDivert >nul 2>&1
sc stop WinDivert1.4 >nul 2>&1

echo [*] Habilitando arranque bajo demanda...
sc config WinDivert start= demand >nul 2>&1
sc config WinDivert1.4 start= demand >nul 2>&1

echo [*] Eliminando servicio (se recreara al abrir Limitador)...
sc delete WinDivert >nul 2>&1
sc delete WinDivert1.4 >nul 2>&1

echo.
echo [OK] Servicio reseteado. Ejecuta Limitador.exe normalmente.
pause
