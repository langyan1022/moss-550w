[CmdletBinding()]
param(
    [switch] $Install,
    [string] $Summary,
    [string] $Weights,
    [string] $Engine,
    [int] $Port = 18176
)

$ErrorActionPreference = 'Stop'
chcp 65001 > $null
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new()
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$OutputEncoding = [System.Text.UTF8Encoding]::new()

Push-Location $PSScriptRoot
try {
    if ($Install) {
        foreach ($name in 'pydantic','nanobot') {
            $python = Join-Path $PSScriptRoot "$name/.venv/Scripts/python.exe"
            if (-not (Test-Path -LiteralPath $python)) {
                & uv venv --python 3.11.15 "$name/.venv"
                if ($LASTEXITCODE) { throw "uv venv failed for $name" }
            }
            & uv pip sync --python $python "$name/requirements.lock"
            if ($LASTEXITCODE) { throw "uv pip sync failed for $name" }
        }
        & uv sync --directory langgraph --frozen --python 3.11.15
        if ($LASTEXITCODE) { throw 'uv sync failed for langgraph' }
    }
    $python = Join-Path $PSScriptRoot 'pydantic/.venv/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $python)) { throw 'Run .\run.ps1 -Install to create the environments.' }
    if ($Summary) {
        & $python summarize.py $Summary
    } else {
        $options = @('--port', "$Port")
        if ($Weights) { $options += @('--weights', $Weights) }
        if ($Engine) { $options += @('--engine', $Engine) }
        & $python run_lab.py @options
    }
    $code = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $code
