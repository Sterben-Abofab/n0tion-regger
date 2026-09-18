#Requires -Version 5.1
<#
.SYNOPSIS
  Запуск notion-fable-proxy (Windows-аналог bridge/start.sh).
  Тот же принцип: uvicorn server:app на 127.0.0.1:8765, одиночный экземпляр.
#>
$ErrorActionPreference = 'Stop'

$BridgeDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Port = if ($env:NOTION_FABLE_PORT) { [int]$env:NOTION_FABLE_PORT } else { 8765 }

# Проверка "уже запущен" — аналог `ss -ltn | grep 127.0.0.1:PORT` в start.sh.
$listening = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listening) {
  Write-Output "Notion Fable bridge already running on 127.0.0.1:$Port"
  exit 0
}

$RepoRoot = Split-Path -Parent $BridgeDir
$VenvPython = Join-Path $RepoRoot '.runtime\notion-agent-cli-venv\Scripts\python.exe'
$VenvPythonW = Join-Path $RepoRoot '.runtime\notion-agent-cli-venv\Scripts\pythonw.exe'
# pythonw.exe — без консольного окна (аналог setsid -f в start.sh).
# -WindowStyle Hidden здесь нельзя: в Windows PowerShell 5.1 он несовместим
# с -RedirectStandardOutput/-RedirectStandardError.
$PythonExe = if (Test-Path -LiteralPath $VenvPythonW) { $VenvPythonW } else { $VenvPython }
if (-not (Test-Path -LiteralPath $PythonExe)) {
  Write-Error "Missing venv python: $VenvPython. Run scripts\Install-Local.ps1 first."
}

Set-Location -LiteralPath $BridgeDir

$logFile = Join-Path ([IO.Path]::GetTempPath()) 'notion-fable-proxy.log'
$errFile = Join-Path ([IO.Path]::GetTempPath()) 'notion-fable-proxy.err.log'
# Два разных файла обязательны: Windows PowerShell 5.1 запрещает
# -RedirectStandardOutput и -RedirectStandardError в один файл
# (аналог `> log 2>&1` в start.sh).
$arguments = @('-m', 'uvicorn', 'server:app', '--host', '127.0.0.1', '--port', "$Port")
$process = Start-Process -FilePath $PythonExe -ArgumentList $arguments `
  -WorkingDirectory $BridgeDir `
  -RedirectStandardOutput $logFile -RedirectStandardError $errFile `
  -PassThru

Start-Sleep -Seconds 2
try {
  $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/healthz" -TimeoutSec 10
  $health | ConvertTo-Json -Compress
} catch {
  Write-Error "Bridge did not become healthy. See $logFile and $errFile. $($_.Exception.Message)"
}
