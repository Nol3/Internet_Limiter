# Lanza el diagnóstico en vivo (limitador en consola) con privilegios, sin esperar.
# Uso: powershell -NoProfile -ExecutionPolicy Bypass -File tools\live_check.ps1
$root = Split-Path -Parent $PSScriptRoot
$runner = Join-Path $root "tools\live_check.cmd"
$output = Join-Path $root "tools\live_output.txt"

if (Test-Path $output) { Remove-Item $output -Force }

Write-Host "Solicitando privilegios de administrador (acepta el aviso de Windows)…"
Start-Process -Verb RunAs -FilePath "cmd.exe" -ArgumentList "/c", "`"$runner`"" | Out-Null
Write-Host "Limitador lanzado. Espera a que termine (unos 25 s) y luego revisa tools\live_output.txt"
