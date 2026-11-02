<#
  JobFinder bootstrap for Windows. Called by "Start JobFinder.cmd" / "Update JobFinder.cmd" / "Connect AI agents.cmd".
  - Installs uv (Python manager) for this user only, no admin rights.
  - uv installs Python 3.12 and the locked dependencies into %LOCALAPPDATA%\JobFinder\venv
    (outside OneDrive/Desktop sync folders on purpose).
  - Starts the app and opens the dashboard.
#>
param([ValidateSet("start", "update", "agents", "doctor")][string]$Action = "start")

$ErrorActionPreference = "Continue"  # PS 5.1 turns native stderr (uv progress) into errors under "Stop"
$ProgressPreference = "SilentlyContinue"
$Root = Split-Path -Parent $PSScriptRoot
$AppHome = Join-Path $env:LOCALAPPDATA "JobFinder"
$BinDir = Join-Path $AppHome "bin"
$Venv = Join-Path $AppHome "venv"
$LogFile = Join-Path $AppHome "bootstrap.log"
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null

function Log([string]$msg) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
    Write-Host $msg
}

function Find-Uv {
    $cands = @((Join-Path $BinDir "uv.exe"), (Join-Path $env:USERPROFILE ".local\bin\uv.exe"), (Join-Path $env:USERPROFILE ".cargo\bin\uv.exe"))
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) { $cands = @($cmd.Source) + $cands }
    foreach ($c in $cands) { if ($c -and (Test-Path $c)) { return $c } }
    return $null
}

function Download([string]$url, [string]$dest) {
    $curl = Join-Path $env:SystemRoot "System32\curl.exe"
    if (Test-Path $curl) {
        & $curl -fsSL --retry 2 --connect-timeout 15 -o $dest $url 2>$null
        if ($LASTEXITCODE -eq 0) { return $true }
        # Some networks advertise IPv6 but drop it: retry over IPv4.
        & $curl -4 -fsSL --retry 2 --connect-timeout 15 -o $dest $url 2>$null
        if ($LASTEXITCODE -eq 0) { return $true }
    }
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $dest -TimeoutSec 120 -ErrorAction Stop
        return $true
    } catch { return $false }
}

function Install-Uv {
    Log "Installing uv (one-time, about 15 MB)..."
    $arch = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "aarch64" } else { "x86_64" }
    $zip = Join-Path $env:TEMP "uv-$arch.zip"
    $url = "https://github.com/astral-sh/uv/releases/latest/download/uv-$arch-pc-windows-msvc.zip"
    if (Download $url $zip) {
        Expand-Archive -Force -Path $zip -DestinationPath $BinDir -ErrorAction SilentlyContinue
        Remove-Item $zip -ErrorAction SilentlyContinue
        $uv = Join-Path $BinDir "uv.exe"
        if (Test-Path $uv) { return $uv }
    }
    Log "Direct download failed; trying the official installer..."
    try {
        $env:UV_INSTALL_DIR = $BinDir; $env:UV_NO_MODIFY_PATH = "1"
        Invoke-RestMethod https://astral.sh/uv/install.ps1 -ErrorAction Stop | Invoke-Expression | Out-Null
        $uv = Join-Path $BinDir "uv.exe"
        if (Test-Path $uv) { return $uv }
    } catch { Log "Installer failed: $($_.Exception.Message)" }
    $py = Get-Command py, python -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($py) {
        Log "Trying pip install uv with $($py.Source)..."
        & $py.Source -m pip install --user --quiet uv 2>&1 | Out-Null
        $found = Find-Uv
        if ($found) { return $found }
    }
    throw "Could not install uv. Check the internet connection (or proxy) and try again. Log: $LogFile"
}

function Sync-Env([string]$uv) {
    Log "Preparing Python and libraries (first run takes 1-3 minutes)..."
    Push-Location $Root
    try {
        & $uv sync --frozen --no-dev --python 3.12 2>&1 | ForEach-Object { Add-Content -Path $LogFile -Value $_ -Encoding UTF8 }
        if ($LASTEXITCODE -ne 0) {
            Log "Locked install failed, retrying with resolution..."
            & $uv sync --no-dev --python 3.12 2>&1 | ForEach-Object { Add-Content -Path $LogFile -Value $_ -Encoding UTF8 }
            if ($LASTEXITCODE -ne 0) { throw "Dependency install failed. See $LogFile" }
        }
    } finally { Pop-Location }
}

try {
    # Use the Windows certificate store (works behind corporate TLS inspection) and keep the venv out of synced folders.
    $env:UV_SYSTEM_CERTS = "1"
    $env:UV_NATIVE_TLS = "1"  # older uv versions
    $env:UV_PROJECT_ENVIRONMENT = $Venv
    $env:UV_PYTHON_PREFERENCE = "managed"
    $env:PYTHONUTF8 = "1"

    $uv = Find-Uv
    if (-not $uv) { $uv = Install-Uv }
    Log "uv: $uv"
    $py = Join-Path $Venv "Scripts\python.exe"
    $stamp = Join-Path $AppHome "synced.txt"
    $lockHash = (Get-FileHash (Join-Path $Root "uv.lock")).Hash + "|" + $Root

    if ($Action -eq "update") {
        if ((Test-Path (Join-Path $Root ".git")) -and (Get-Command git -ErrorAction SilentlyContinue)) {
            Log "Fetching latest version..."
            Push-Location $Root; git pull --ff-only; Pop-Location
            $lockHash = (Get-FileHash (Join-Path $Root "uv.lock")).Hash + "|" + $Root
        } else {
            Log "Not a git checkout: download the latest zip and replace this folder, then run Update again."
        }
        Sync-Env $uv
        Set-Content -Path $stamp -Value $lockHash
        & $py -m jobfinder doctor
        Log "Update complete."
        exit 0
    }

    if (-not (Test-Path $py) -or -not (Test-Path $stamp) -or ((Get-Content $stamp -Raw).Trim() -ne $lockHash)) {
        Sync-Env $uv
        Set-Content -Path $stamp -Value $lockHash
    }

    switch ($Action) {
        "start" { & $py -m jobfinder start }
        "doctor" { & $py -m jobfinder doctor }
        "agents" {
            Write-Host ""
            Write-Host "Connect JobFinder to an AI agent app (MCP):"
            $clients = @("codex", "claude-desktop", "claude-code", "vscode", "opencode", "cursor")
            for ($i = 0; $i -lt $clients.Count; $i++) { Write-Host ("  {0}) {1}" -f ($i + 1), $clients[$i]) }
            $pick = Read-Host "Number (or several, e.g. 1,4)"
            $small = Read-Host "Will the app use a small or local model (Ollama, LM Studio, 7B-14B)? y/N"
            $liteArg = @()
            if ($small -match '^\s*[yYsS]') { $liteArg = @("--lite"); Write-Host "Using guided mode (one question at a time)." }
            foreach ($n in ($pick -split "[,\s]+" | Where-Object { $_ })) {
                $i = 0
                if ([int]::TryParse($n, [ref]$i) -and $i -ge 1 -and $i -le $clients.Count) {
                    & $py -m jobfinder mcp-install $clients[$i - 1] @liteArg
                } else {
                    Write-Host "Ignoring '$n' (choose 1-$($clients.Count))"
                }
            }
        }
    }
    if ($LASTEXITCODE -ne 0 -and $Action -eq "start") { throw "JobFinder stopped with an error. See $LogFile and $(Join-Path $AppHome 'workspace\logs')" }
} catch {
    Log "ERROR: $($_.Exception.Message)"
    Write-Host ""
    Write-Host "Something went wrong. Details are in: $LogFile" -ForegroundColor Red
    Read-Host "Press Enter to close"
    exit 1
}
