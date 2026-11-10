# Starts the Gomez MT5 bridge. Double-click start_bridge.bat instead of running this directly.
Set-Location $PSScriptRoot
$port = 8765
$isCustomer = Test-Path "license.json"   # a downloaded customer bridge (your own bridge has no license.json)

if ($isCustomer) {
    # Customer bridge: never stop another program. If 8765 is taken (for example YOUR own bridge while you
    # test as a customer), use the next free port; the website looks on 8765-8769 by itself.
    function Test-PortBusy($p) {
        $c = New-Object Net.Sockets.TcpClient
        try { return $c.ConnectAsync("127.0.0.1", $p).Wait(400) -and $c.Connected } catch { return $false } finally { $c.Close() }
    }
    while ($port -lt 8770 -and (Test-PortBusy $port)) { $port++ }
    if ($port -ne 8765) { Write-Host "Port 8765 is used by another program; this bridge uses port $port." -ForegroundColor Yellow }
} else {
    # 1) Free the port. If an old bridge (for example the one the removed installer set up)
    #    is still running, it would answer your Cloudflare link instead of this app.py.
    $busy = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    foreach ($c in $busy) {
        $p = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue
        Write-Host "Port $port is already in use by '$($p.ProcessName)' (PID $($c.OwningProcess)). Stopping it..." -ForegroundColor Yellow
        Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue
    }
    if ($busy) { Start-Sleep -Seconds 1 }
}

# 2) Python environment (installs Python automatically the first time if it is missing)
$pyCmd = if (Get-Command py -ErrorAction SilentlyContinue) { "py" } elseif (Get-Command python -ErrorAction SilentlyContinue) { "python" } else { $null }
if (-not $pyCmd) {
    Write-Host "Python is not installed yet. Installing it now (one time, about 1 minute)..." -ForegroundColor Yellow
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
        $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + [Environment]::GetEnvironmentVariable("Path", "Machine")
        $pyCmd = if (Get-Command py -ErrorAction SilentlyContinue) { "py" } elseif (Get-Command python -ErrorAction SilentlyContinue) { "python" } else { $null }
    }
    if (-not $pyCmd) {
        Write-Host "Could not install Python automatically. Install it from https://www.python.org/downloads/ (tick 'Add Python to PATH'), then start this again." -ForegroundColor Red
        Start-Process "https://www.python.org/downloads/"
        exit 1
    }
}
if (-not (Test-Path ".venv")) { & $pyCmd -m venv .venv }
& ".\.venv\Scripts\Activate.ps1"
if (Test-Path "requirements.txt") {
    pip install -q -r requirements.txt
} else {
    Write-Host "requirements.txt not found next to app.py - skipping pip install." -ForegroundColor Yellow
}

# 3) Non-blocking check: warn if copy_settings.json refers to an account missing from config.json
try {
    $cfg = Get-Content "config.json" -Raw | ConvertFrom-Json
    if (Test-Path "copy_settings.json") {
        $cs = Get-Content "copy_settings.json" -Raw | ConvertFrom-Json
        $logins = @($cfg.terminals | ForEach-Object { [string]$_.login })
        foreach ($r in @("master","slave")) {
            if ($cs.$r -and ($logins -notcontains [string]$cs.$r)) {
                Write-Host "WARNING: copy_settings.json $r = $($cs.$r) is not in config.json. Copy trading will fail until fixed." -ForegroundColor Red
            }
        }
    }
} catch { Write-Host "WARNING: could not read config.json: $_" -ForegroundColor Red }

# 4) Customer install (license.json present): open the website a few seconds after the bridge starts;
#    it finds this bridge by itself and connects. Nothing to type.
if (Test-Path "license.json") {
    try {
        $lj = Get-Content "license.json" -Raw | ConvertFrom-Json
        $site = if ($lj.site) { $lj.site } else { $lj.server }
        if ($site) {
            # ".../terminal_test.html" -> ".../terminal_test.html#setup" ; ".../app" -> ".../app/#setup"
            $url = ($site -replace '[#?].*$', '') + '#setup'   # never add a "/": ".../terminal_test" would become a 404
            Start-Process powershell -WindowStyle Hidden -ArgumentList "-NoProfile", "-Command", "Start-Sleep 8; Start-Process '$url'"
        }
    } catch {}
    Write-Host "Make sure MetaTrader 5 is open and logged in to your account." -ForegroundColor Yellow
}

# 5) Run
Write-Host "Starting bridge on port $port." -ForegroundColor Green
Write-Host "On this PC: http://127.0.0.1:$port" -ForegroundColor Cyan
Write-Host "For your Cloudflare link, in a second window run:  cloudflared tunnel --url http://localhost:$port" -ForegroundColor Cyan
Write-Host "Keep this window open." -ForegroundColor Yellow
python -m uvicorn app:app --host 0.0.0.0 --port $port
