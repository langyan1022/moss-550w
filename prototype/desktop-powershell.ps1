chcp 65001 > $null
$utf8 = [System.Text.UTF8Encoding]::new()
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'
$ErrorActionPreference = 'Stop'

try {
    $commands = [ScriptBlock]::Create([Console]::In.ReadToEnd())
    $global:LASTEXITCODE = 0
    & $commands
    exit $global:LASTEXITCODE
} catch {
    [Console]::Error.WriteLine($_.ToString())
    exit 1
}
