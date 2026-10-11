# Pide UAC una sola vez y ejecuta la prueba de aceptación como administrador.
#
# Protección: si el proceso no termina en TimeoutSec segundos se mata el árbol de
# procesos completo. Al morir el proceso se cierran los descriptores de WinDivert y el
# tráfico de red vuelve a fluir, así que la prueba no puede dejarte sin conexión.
#
# Uso:  powershell -NoProfile -ExecutionPolicy Bypass -File tools\run_elevated.ps1 -Seconds 8
param(
    [int]$Seconds = 8,
    [int]$TimeoutSec = 0
)

$root = Split-Path -Parent $PSScriptRoot
$runner = Join-Path $root "tools\run_elevated.cmd"
$output = Join-Path $root "tools\acceptance_output.txt"

if ($TimeoutSec -le 0) { $TimeoutSec = ($Seconds * 4) + 90 }

if (Test-Path $output) { Remove-Item $output -Force }

Write-Host "Solicitando privilegios de administrador (acepta el aviso de Windows)…"
$process = Start-Process -Verb RunAs -PassThru -FilePath "cmd.exe" `
    -ArgumentList "/c", "`"$runner`" --seconds $Seconds"

if (-not $process.WaitForExit($TimeoutSec * 1000)) {
    Write-Host "[AVISO] la prueba excedió $TimeoutSec s: matando el proceso para liberar la red…"
    & taskkill.exe /PID $process.Id /T /F | Out-Null
    Start-Sleep -Seconds 2
}

if (Test-Path $output) {
    Get-Content $output
} else {
    Write-Host "[ERROR] no se generó $output (¿se canceló el UAC?)"
}
