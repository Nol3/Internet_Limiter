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

Write-Host "Solicitando privilegios de administrador (acepta el aviso de Windows)…"
$inner = "`"$python`" -u `"$root\tools\$Script`" $ScriptArgs > `"$output`" 2>&1"
$process = Start-Process -Verb RunAs -PassThru -FilePath "cmd.exe" `
    -ArgumentList "/c", $inner

if (-not $process.WaitForExit($TimeoutSec * 1000)) {
    Write-Host "[AVISO] el script excedió $TimeoutSec s: matando el proceso para liberar la red…"
    & taskkill.exe /PID $process.Id /T /F | Out-Null
    Start-Sleep -Seconds 2
}

if (Test-Path $output) {
    Get-Content -Encoding UTF8 $output
} else {
    Write-Host "[ERROR] no se generó $output (¿se canceló el aviso de UAC?)"
}
