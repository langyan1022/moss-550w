[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $Name,
    [Parameter(Mandatory)] [string] $Executable,
    [string[]] $TestArguments = @(),
    [double] $MinFreeGiB = 0,
    [string] $WorkDirectory = $PSScriptRoot
)

$ErrorActionPreference = 'Stop'
chcp 65001 > $null
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new()
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$OutputEncoding = [System.Text.UTF8Encoding]::new()

$runDirectory = Join-Path $PSScriptRoot 'prototype/runs'
New-Item -ItemType Directory -Path $runDirectory -Force | Out-Null
$lockPath = Join-Path $runDirectory 'hardware-benchmark.lock'
try {
    $lease = [System.IO.File]::Open($lockPath, 'OpenOrCreate', 'ReadWrite', 'None')
} catch [System.IO.IOException] {
    if (($_.Exception.HResult -band 0xFFFF) -in 32,33) {
        Write-Output '另一项硬件测试正在运行，本项未启动。'
        exit 73
    }
    throw
}

try {
    if ($MinFreeGiB -gt 0) {
        $available = (Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory / 1MB
        if ($available -lt $MinFreeGiB) {
            Write-Output ('{0} 未启动。可用内存 {1:F2} GiB，所需余量 {2:F2} GiB。' -f $Name, $available, $MinFreeGiB)
            exit 4
        }
    }
    Push-Location -LiteralPath $WorkDirectory
    try {
        Write-Output "开始独占测试 $Name"
        & $Executable @TestArguments
        exit $LASTEXITCODE
    } finally {
        Pop-Location
    }
} finally {
    $lease.Dispose()
}
