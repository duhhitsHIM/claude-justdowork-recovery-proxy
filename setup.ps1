<#
.SYNOPSIS
    Automated 1-click setup for Claude JustDoWork Recovery Proxy.
    Sets up python virtual environment, installs dependencies,
    saves the API key, safely configures Claude Code's settings.json,
    and launches the proxy.
#>

param(
    [string]$ApiKey,
    [string]$Model = "claude-opus-4-8",
    [switch]$SkipSettings
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $ScriptDir

Write-Host "`n========================================================" -ForegroundColor Cyan
Write-Host "   Claude JustDoWork Recovery Proxy - 1-Click Setup" -ForegroundColor Cyan
Write-Host "========================================================`n" -ForegroundColor Cyan

# 1. Check Python (prefer py launcher or valid python, avoiding Windows Store dummy shim)
$pythonCmd = $null
$candidates = @("py", "python", "python3")
foreach ($c in $candidates) {
    try {
        $testOut = & $c -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $testOut) {
            $pythonCmd = $c
            break
        }
    } catch {}
}

if (-not $pythonCmd) {
    # Check common user install paths directly as fallback
    $commonPaths = @(
        "$HOME\AppData\Local\Programs\Python\Python313\python.exe",
        "$HOME\AppData\Local\Programs\Python\Python312\python.exe",
        "$HOME\AppData\Local\Programs\Python\Python311\python.exe",
        "$HOME\AppData\Local\Programs\Python\Python310\python.exe",
        "C:\Python313\python.exe",
        "C:\Python312\python.exe"
    )
    foreach ($p in $commonPaths) {
        if (Test-Path $p) {
            $pythonCmd = $p
            break
        }
    }
}

if (-not $pythonCmd) {
    Write-Host "[X] Python is not installed or not in PATH." -ForegroundColor Red
    Write-Host "    Please install Python 3.8+ from python.org and re-run.`n"
    Pause
    exit 1
}

# 2. Virtual Environment
$venvPython = Join-Path $ScriptDir ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "[*] Creating Python virtual environment (.venv)..." -ForegroundColor Yellow
    & $pythonCmd -m venv .venv
    if (-not (Test-Path $venvPython)) {
        Write-Host "[X] Failed to create virtual environment." -ForegroundColor Red
        Pause
        exit 1
    }
}

# 3. Install Requirements
Write-Host "[*] Installing/verifying dependencies (flask, requests, pystray, pillow)..." -ForegroundColor Yellow
& $venvPython -m pip install -q -r requirements.txt pillow
if ($LASTEXITCODE -ne 0) {
    Write-Host "[X] Failed to install Python dependencies." -ForegroundColor Red
    Pause
    exit 1
}
Write-Host "[OK] Dependencies ready." -ForegroundColor Green

# 4. API Key Resolution
$keyFile = Join-Path $ScriptDir "key.txt"
if (-not $ApiKey) {
    if ($env:UPSTREAM_API_KEY) {
        $ApiKey = $env:UPSTREAM_API_KEY
    } elseif (Test-Path $keyFile) {
        $ApiKey = (Get-Content $keyFile -Raw).Trim()
    }
}

if (-not $ApiKey) {
    Write-Host "`nEnter your JustDoWork API Key (hidden input): " -ForegroundColor Yellow -NoNewline
    $sec = Read-Host -AsSecureString
    $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
    $ApiKey = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
    Write-Host ""
}

if (-not $ApiKey) {
    Write-Host "[X] No API key provided. Setup cancelled." -ForegroundColor Red
    Pause
    exit 1
}

# Write key.txt without trailing newline or BOM
[System.IO.File]::WriteAllText($keyFile, $ApiKey, [System.Text.Encoding]::ASCII)
Write-Host "[OK] API key saved to key.txt (git-ignored)." -ForegroundColor Green

# 5. Configure Claude Code settings.json
if (-not $SkipSettings) {
    $claudeDir = Join-Path $HOME ".claude"
    $claudeSettingsFile = Join-Path $claudeDir "settings.json"
    
    if (-not (Test-Path $claudeDir)) {
        New-Item -ItemType Directory -Path $claudeDir -Force | Out-Null
    }

    $settingsObj = [ordered]@{}
    if (Test-Path $claudeSettingsFile) {
        try {
            $rawContent = Get-Content $claudeSettingsFile -Raw
            if ($rawContent.Trim()) {
                $settingsObj = $rawContent | ConvertFrom-Json
                # Backup existing configuration
                $backupPath = Join-Path $claudeDir "settings.json.bak"
                Copy-Item $claudeSettingsFile $backupPath -Force
                Write-Host "[*] Backed up existing Claude settings to settings.json.bak" -ForegroundColor DarkGray
            }
        } catch {
            Write-Host "[!] Could not parse existing settings.json. Creating fresh configuration." -ForegroundColor Yellow
        }
    }

    if (-not $settingsObj.PSObject.Properties['env']) {
        $settingsObj | Add-Member -NotePropertyName "env" -NotePropertyValue ([ordered]@{}) -Force
    }

    # Set required proxy environment variables safely on PSCustomObject
    $envProps = @{
        "ANTHROPIC_BASE_URL" = "http://127.0.0.1:8181"
        "ANTHROPIC_MODEL" = $Model
        "ENABLE_TOOL_SEARCH" = "false"
    }
    foreach ($k in $envProps.Keys) {
        if ($settingsObj.env.PSObject.Properties[$k]) {
            $settingsObj.env.$k = $envProps[$k]
        } else {
            $settingsObj.env | Add-Member -NotePropertyName $k -NotePropertyValue $envProps[$k] -Force
        }
    }
    if (-not $settingsObj.env.PSObject.Properties["ANTHROPIC_API_KEY"]) {
        $settingsObj.env | Add-Member -NotePropertyName "ANTHROPIC_API_KEY" -NotePropertyValue "justdowork" -Force
    }

    $newJson = $settingsObj | ConvertTo-Json -Depth 10
    [System.IO.File]::WriteAllText($claudeSettingsFile, $newJson, [System.Text.Encoding]::UTF8)
    Write-Host "[OK] Claude Code settings automatically configured at $claudeSettingsFile" -ForegroundColor Green
}

# 6. Launch Background Tray & Proxy
Write-Host "[*] Launching Proxy and Tray icon in the background..." -ForegroundColor Yellow
$vbsPath = Join-Path $ScriptDir "start_tray.vbs"
Start-Process "wscript.exe" -ArgumentList "`"$vbsPath`""

# 7. Health Check Verification
Write-Host "[*] Waiting for proxy to respond on http://127.0.0.1:8181..." -ForegroundColor Yellow
$healthy = $false
for ($i = 0; $i -lt 15; $i++) {
    Start-Sleep -Milliseconds 600
    try {
        $resp = Invoke-RestMethod -Uri "http://127.0.0.1:8181/health" -TimeoutSec 2 -ErrorAction SilentlyContinue
        if ($resp -and $resp.status -eq "ok") {
            $healthy = $true
            break
        }
    } catch {}
}

if ($healthy) {
    Write-Host "`n========================================================" -ForegroundColor Green
    Write-Host " [SUCCESS] Proxy is alive & Claude Code is configured!" -ForegroundColor Green
    Write-Host "========================================================" -ForegroundColor Green
    Write-Host " -> Proxy running in system tray: http://127.0.0.1:8181" -ForegroundColor Cyan
    Write-Host " -> Claude Code Base URL pointed to proxy." -ForegroundColor Cyan
    Write-Host "`nYou're ready to go! Simply run in any terminal:" -ForegroundColor Yellow
    Write-Host "    claude`n" -ForegroundColor White
} else {
    Write-Host "`n[!] Proxy started, but took longer than expected to respond to health check." -ForegroundColor Yellow
    Write-Host "    Check proxy_out.log or tray.log if you encounter issues.`n" -ForegroundColor DarkGray
}

Write-Host "Press any key to close..."
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
