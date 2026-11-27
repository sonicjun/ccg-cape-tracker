<#
.SYNOPSIS
    Local Windows launcher for the CCG 47-ft Cape Class Maritime Tracker.
.DESCRIPTION
    Creates/activates .venv if missing, installs requirements.txt, generates mock AIS
    positions when --mock / -Mock is passed or data/positions.json is missing, and
    serves the static web app on http://localhost:8000.
.EXAMPLE
    .\start.ps1
    .\start.ps1 --mock
#>

[CmdletBinding(PositionalBinding = $false)]
param(
    [switch]$Mock,
    [int]$Port = 8000,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ExtraArgs
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$UseMock = $Mock.IsPresent -or ($ExtraArgs -contains "--mock") -or ($ExtraArgs -contains "-mock")

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  CCG 47-ft Cape Class SAR Fleet Tracker - Local Launcher" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

# 1. Create virtual environment if missing
$VenvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$ActivateScript = Join-Path $PSScriptRoot ".venv\Scripts\Activate.ps1"

if (-not (Test-Path $VenvPython)) {
    Write-Host "[1/4] Creating Python virtual environment (.venv)..." -ForegroundColor Yellow
    python -m venv .venv
} else {
    Write-Host "[1/4] Found existing virtual environment (.venv)." -ForegroundColor Green
}

if (Test-Path $ActivateScript) {
    . $ActivateScript
}

# 2. Install dependencies
Write-Host "[2/4] Ensuring Python dependencies from requirements.txt..." -ForegroundColor Yellow
& $VenvPython -m pip install --quiet -r requirements.txt

# 3. Populate data/positions.json if missing or if --mock was requested
$PositionsFile = Join-Path $PSScriptRoot "data\positions.json"
if ($UseMock -or -not (Test-Path $PositionsFile)) {
    Write-Host "[3/4] Generating simulated maritime AIS positions (--mock)..." -ForegroundColor Yellow
    & $VenvPython scripts\fetch_ais.py --mock
} else {
    Write-Host "[3/4] Using existing AIS cache at data\positions.json (pass --mock to refresh mock data)." -ForegroundColor Green
}

# 4. Launch browser and local static HTTP server
$Url = "http://localhost:$Port"
Write-Host "[4/4] Starting local static server at $Url (Press Ctrl+C to stop)..." -ForegroundColor Cyan
Start-Process $Url
& $VenvPython -m http.server $Port
