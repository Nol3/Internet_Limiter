# Pide UAC una sola vez y ejecuta la prueba de aceptación como administrador.
#
# Protección: si el proceso no termina en TimeoutSec segundos se mata el árbol de
# procesos completo. Al morir el proceso se cierran los descriptores de WinDivert y el
# tráfico de red vuelve a fluir, así que la prueba no puede dejarte sin conexión.
#
# Uso:
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\run_elevated.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\run_elevated.ps1 `
#       -Script demo.py -ScriptArgs "--seconds 12" -TimeoutSec 300
param(
    [string]$Script = "acceptance_test.py",
    [int]$Seconds = 8,
    [int]$TimeoutSec = 0,
    [string]$ScriptArgs = ""
)

$root = Split-Path -Parent $PSScriptRoot
$name = [System.IO.Path]::GetFileNameWithoutExtension($Script)
$output = Join-Path $root "tools\${name}_output.txt"
$python = Join-Path $root ".venv\Scripts\python.exe"

if ($TimeoutSec -le 0) { $TimeoutSec = ($Seconds * 4) + 120 }

if (Test-Path $output) { Remove-Item $output -Force }

# El lanzamiento va en un .cmd temporal en lugar de una linea de comandos construida a mano:
# citar rutas, argumentos y la redireccion desde PowerShell se rompe con facilidad, y el
# fallo inmediato se confunde con "el usuario cancelo el aviso de UAC" (paso, y costo horas).
$runner = Join-Path $env:TEMP "limitador_$name.cmd"
$lines = @(
    "@echo off",
    "chcp 65001 >nul",
    "cd /d `"$root`"",
    "`"$python`" -u `"$root\tools\$Script`" $ScriptArgs > `"$output`" 2>&1",
    "echo EXITCODE=%ERRORLEVEL% >> `"$output`""
)
Set-Content -Path $runner -Value $lines -Encoding ASCII

Write-Host "Solicitando privilegios de administrador (si Windows lo pide, acepta el aviso)…"
try {
    $process = Start-Process -Verb RunAs -PassThru -FilePath "cmd.exe" `
        -ArgumentList "/c", "`"$runner`"" -ErrorAction Stop
} catch {
    Write-Host "[ERROR] no se pudo iniciar con privilegios: $($_.Exception.Message)"
    Remove-Item $runner -Force -ErrorAction SilentlyContinue
    exit 1
}

if (-not $process.WaitForExit($TimeoutSec * 1000)) {
    Write-Host "[AVISO] el script excedió $TimeoutSec s: matando el proceso para liberar la red…"
    & taskkill.exe /PID $process.Id /T /F | Out-Null
    Start-Sleep -Seconds 2
}

if (Test-Path $output) {
    Get-Content -Encoding UTF8 $output
} else {
    Write-Host "[ERROR] el script no dejó salida en $output (código de salida $($process.ExitCode))"
}
Remove-Item $runner -Force -ErrorAction SilentlyContinue
