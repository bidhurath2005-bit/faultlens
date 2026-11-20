# FaultLens helper for Windows PowerShell.
# Usage: powershell -ExecutionPolicy Bypass -File scripts\faultlens.ps1 <command>
#   setup | up | status | logs [service] | test | test-live | e2e | eval | bench | reset-demo | down | destroy
param([Parameter(Position = 0)][string]$Command = "help", [Parameter(Position = 1)][string]$Arg = "")
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Py = Join-Path $Root ".venv\Scripts\python.exe"

function Ensure-Venv {
    if (-not (Test-Path $Py)) {
        Write-Host "Creating Python 3.12 virtualenv (.venv)..." -ForegroundColor Cyan
        py -3.12 -m venv .venv
        & $Py -m pip install -q --upgrade pip
        & $Py -m pip install -q -r backend\requirements-dev.txt -r services\requirements.txt .\services\common
    }
}

function Wait-Healthy {
    Write-Host "Waiting for the API to become healthy..." -NoNewline
    for ($i = 0; $i -lt 90; $i++) {
        try { $h = Invoke-RestMethod -TimeoutSec 3 http://localhost:8000/api/v1/health; if ($h.status) { Write-Host " ok"; return } } catch { }
        Write-Host "." -NoNewline; Start-Sleep -Seconds 2
    }
    throw "API did not become healthy; run: scripts\faultlens.ps1 logs faultlens-api"
}

switch ($Command) {
    "setup" { & "$PSScriptRoot\setup.ps1" }
    "up" {
        if (-not (Test-Path .env)) { & "$PSScriptRoot\setup.ps1" }
        docker compose up -d --build
        Wait-Healthy
        Write-Host "UI:       http://localhost:3080   (user: admin, password: BOOTSTRAP_ADMIN_PASSWORD in .env)" -ForegroundColor Green
        Write-Host "API docs: http://localhost:8000/api/docs"
        Write-Host "Grafana:  http://localhost:3000   (admin / GRAFANA_ADMIN_PASSWORD in .env)"
        Write-Host "Tip: detectors need ~5 minutes of baseline traffic before statistical methods engage."
    }
    "status" {
        docker compose ps --format "table {{.Name}}\t{{.Status}}"
        try { Invoke-RestMethod http://localhost:8000/api/v1/health | ConvertTo-Json -Depth 3 } catch { Write-Warning "API not reachable" }
    }
    "logs" { if ($Arg) { docker compose logs -f --tail 100 $Arg } else { docker compose logs -f --tail 50 } }
    "test" {
        Ensure-Venv
        docker compose up -d postgres redis | Out-Null
        Push-Location backend; & $Py -m ruff check app tests; & $Py -m pytest -q; Pop-Location
        Push-Location services; & $Py -m ruff check .; $env:OTEL_ENABLED = "false"; & $Py -m pytest -q; Pop-Location
        Push-Location frontend; npx tsc -b --noEmit; npx eslint . --max-warnings 0; npx vitest run; Pop-Location
    }
    "test-live" { Ensure-Venv; Push-Location e2e; & $Py -m pytest test_integration.py -v; Pop-Location; Push-Location frontend; npx playwright test; Pop-Location }
    "e2e" { Ensure-Venv; & $Py -m pytest e2e\test_e2e_flow.py -v -s -c e2e\pytest.ini --rootdir e2e }
    "eval" { Ensure-Venv; & $Py -u e2e\rca_eval.py }
    "bench" { Ensure-Venv; & $Py e2e\benchmark.py }
    "reset-demo" {
        # Clears faults and resets FaultLens state (incidents, anomalies, investigations, audit) but keeps users.
        Ensure-Venv
        & $Py -c "import sys; sys.path.insert(0,'e2e'); from client import admin; a=admin(); print(a.post('/fault-injection/reset').json())"
        $sql = "TRUNCATE incidents, anomalies, investigations, root_cause_findings, recommendations, incident_events, incident_services, audit_events, fault_injections RESTART IDENTITY CASCADE;"
        docker compose exec -T postgres psql -U postgres -d faultlens -c $sql
        docker compose exec -T redis sh -c 'redis-cli -a "$REDIS_PASSWORD" --no-auth-warning FLUSHDB' | Out-Null
        Write-Host "Demo data reset." -ForegroundColor Green
    }
    "down" { docker compose down }
    "destroy" { docker compose down -v; Write-Host "Containers and volumes removed (.env kept)." }
    default {
        Write-Host "Commands: setup | up | status | logs [service] | test | test-live | e2e | eval | bench | reset-demo | down | destroy"
    }
}
