[CmdletBinding()]
param(
    [int]$Port = 18331,
    [string]$Upstream = 'http://127.0.0.1:18221',
    [switch]$Open,
    [switch]$Stop,
    [switch]$Offline,
    [switch]$StartBackend
)

$ErrorActionPreference = 'Stop'
chcp 65001 > $null
$url = "http://127.0.0.1:$Port"

function Get-FuiConfig {
    try { Invoke-RestMethod -Uri "$url/fui/config" -TimeoutSec 2 }
    catch { return $null }
}

if ($Stop) {
    $cfg = Get-FuiConfig
    if ($null -eq $cfg) { Write-Output 'MOSS-FUI is already stopped.'; return }
    Invoke-RestMethod -Uri "$url/fui/shutdown" -Method Post -ContentType 'application/json' -Body '{}' | Out-Null
    Write-Output 'MOSS-FUI stopped.'
    return
}

if ($StartBackend) {
    $chatPs1 = 'F:\moss\agent-lab\chat.ps1'
    if (Test-Path $chatPs1) {
        Write-Output 'Ensuring MOSS backend (127.0.0.1:18221) is running...'
        & powershell.exe -NoProfile -File $chatPs1 | Out-Null
    }
}

$cfg = Get-FuiConfig
if ($null -eq $cfg) {
    $logs = Join-Path $PSScriptRoot 'logs'
    New-Item -ItemType Directory -Force -Path $logs | Out-Null
    $stamp = Get-Date -Format yyyyMMdd-HHmmss
    $outLog = Join-Path $logs "fui-$stamp.stdout.log"
    $errLog = Join-Path $logs "fui-$stamp.stderr.log"
    $serverScript = Join-Path $PSScriptRoot 'server.mjs'
    $nodeArgs = @($serverScript, '--port', "$Port", '--upstream', $Upstream)
    if ($Offline) { $nodeArgs += '--offline' }
    $proc = Start-Process -FilePath 'node' -ArgumentList $nodeArgs -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput $outLog -RedirectStandardError $errLog
    for ($i = 0; $i -lt 40; $i++) {
        if ($proc.HasExited) { throw (Get-Content -Raw -Encoding UTF8 -LiteralPath $errLog) }
        $cfg = Get-FuiConfig
        if ($cfg.app -eq 'moss-fui-console') { break }
        Start-Sleep -Milliseconds 150
    }
}

if ($null -eq $cfg -or $cfg.app -ne 'moss-fui-console') {
    throw "MOSS-FUI failed to start on $url"
}

Write-Output "$url/"
if ($Open) { Start-Process "$url/" }
