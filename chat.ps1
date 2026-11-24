[CmdletBinding()]
param(
    [switch]$Stop,
    [switch]$Open,
    [ValidatePattern('^http://(127\.0\.0\.1|localhost|\[::1\]):\d{1,5}$')] [string]$MemoryUrl,
    [ValidateSet('auto', 'cuda', 'vulkan', 'cpu')] [string]$QwenBackend = 'auto',
    [ValidatePattern('^Vulkan[0-9]+$')] [string]$VulkanDevice,
    [switch]$AllowCpu
)

$ErrorActionPreference = 'Stop'
chcp 65001 > $null
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new()
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$OutputEncoding = [System.Text.UTF8Encoding]::new()
$appDir = Join-Path $PSScriptRoot 'prototype'
$python = Join-Path $appDir '.venv/Scripts/python.exe'
$instance = Join-Path $appDir 'data/instance.json'
$url = 'http://127.0.0.1:18221'
$loopbackRequest = @{}
if ((Get-Command Invoke-RestMethod).Parameters.ContainsKey('NoProxy')) {
    $loopbackRequest.NoProxy = $true
}

function Get-ChatHealth {
    try { Invoke-RestMethod @loopbackRequest -Uri "$url/api/health" -TimeoutSec 2 }
    catch { return $null }
}

$state = Get-ChatHealth
if ($Stop) {
    if ($null -eq $state) { Write-Output 'MOSS is stopped.'; return }
    $owner = Get-Content -Raw -Encoding UTF8 -LiteralPath $instance | ConvertFrom-Json
    if ($state.app -ne 'moss-local-chat' -or $state.pid -ne $owner.pid) { throw 'Unexpected service identity.' }
    $payload = @{token=$owner.token} | ConvertTo-Json -Compress
    Invoke-RestMethod @loopbackRequest -Uri "$url/api/shutdown" -Method Post -ContentType 'application/json' -Body $payload | Out-Null
    for ($i=0; $i -lt 40; $i++) {
        if (-not (Get-Process -Id $owner.pid -ErrorAction SilentlyContinue)) { Write-Output 'MOSS stopped.'; return }
        Start-Sleep -Milliseconds 250
    }
    throw 'MOSS is still stopping. See prototype/logs.'
}

$QwenBackend = $QwenBackend.ToLowerInvariant()
if ($VulkanDevice) {
    $VulkanDevice = 'Vulkan' + $VulkanDevice.Substring(6)
    if ($QwenBackend -notin @('auto', 'vulkan')) { throw 'VulkanDevice requires the auto or vulkan backend.' }
}
if ($AllowCpu -and $QwenBackend -ne 'auto') { throw 'AllowCpu applies to automatic backend selection.' }

if ($null -eq $state) {
    & uv sync --directory $appDir --frozen --inexact --python 3.11.15
    if ($LASTEXITCODE) { throw 'Could not prepare the prototype environment.' }
    $logs = Join-Path $appDir 'logs'
    New-Item -ItemType Directory -Force -Path $logs | Out-Null
    $stamp = Get-Date -Format yyyyMMdd-HHmmss-ffff
    $outLog = Join-Path $logs "$stamp.stdout.log"
    $errLog = Join-Path $logs "$stamp.stderr.log"
    $arguments = @('-u', 'serve.py')
    if ($MemoryUrl) { $arguments += @('--memory-url', $MemoryUrl) }
    if ($QwenBackend -ne 'auto') { $arguments += @('--qwen-backend', $QwenBackend) }
    if ($VulkanDevice) { $arguments += @('--vulkan-device', $VulkanDevice) }
    if ($AllowCpu) { $arguments += '--allow-cpu' }
    $proc = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $appDir -WindowStyle Hidden -PassThru -RedirectStandardOutput $outLog -RedirectStandardError $errLog
    for ($i=0; $i -lt 150; $i++) {
        if ($proc.HasExited) { throw (Get-Content -Raw -Encoding UTF8 -LiteralPath $errLog) }
        $state = Get-ChatHealth
        if ($state.app -eq 'moss-local-chat') { break }
        Start-Sleep -Milliseconds 400
    }
}
if ($state.app -ne 'moss-local-chat') { throw 'MOSS is unavailable on port 18221. See prototype/logs.' }
if ($MemoryUrl -and $state.memory_url -ne $MemoryUrl) { throw 'Close the current MOSS service with .\chat.ps1 -Stop before changing the memory service.' }
if (($PSBoundParameters.ContainsKey('QwenBackend') -and $state.qwen_backend -ne $QwenBackend) -or
    ($VulkanDevice -and $state.vulkan_device -ne $VulkanDevice) -or
    ($PSBoundParameters.ContainsKey('AllowCpu') -and $state.allow_cpu -ne $AllowCpu.IsPresent)) {
    throw '关闭当前服务后才能更改本机 Qwen 的启动选项。运行 .\chat.ps1 -Stop。'
}
Write-Output "$url/"
if ($Open) { Start-Process "$url/" }
