# Windows equivalent of the Makefile (no `make` needed). Run it via make.cmd in the repo root.
#   .\make up        .\make spike        .\make supplier-down
param(
    [Parameter(Position = 0)][string]$Target = "help",
    [double]$FailRate = 0.5
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)   # repo root
$Supplier = "http://localhost:8001"
$Dashboard = "http://localhost:8002"

function Post($url, $body) {
    if ($body) { Invoke-RestMethod -Method Post -Uri $url -ContentType "application/json" -Body $body | ConvertTo-Json -Compress }
    else { Invoke-RestMethod -Method Post -Uri $url | ConvertTo-Json -Compress }
}

function Get-TunnelUrl {
    # The tunnel's own hostname from its logs; api.trycloudflare.com is Cloudflare's API, never the tunnel.
    $logs = docker compose --profile tunnel logs tunnel 2>&1 | Out-String
    $m = [regex]::Matches($logs, 'https://[a-z0-9-]+\.trycloudflare\.com') |
         Where-Object { $_.Value -ne "https://api.trycloudflare.com" } | Select-Object -Last 1
    if ($m) { return $m.Value } else { return $null }
}

function Check($name, $url) {
    try { Invoke-RestMethod -Uri $url -TimeoutSec 3 | Out-Null; Write-Host ("  {0,-14} OK    {1}" -f $name, $url) -ForegroundColor Green }
    catch { Write-Host ("  {0,-14} DOWN  {1}" -f $name, $url) -ForegroundColor Red }
}

switch ($Target) {
    "status" {
        Write-Host "Containers:"; docker compose --profile tunnel ps --format "  {{.Service}}`t{{.Status}}"
        Write-Host "Health:"
        Check "supplier" "$Supplier/health"
        Check "dashboard" "$Dashboard/api/metrics"
        $ping = docker compose exec -T redis redis-cli ping 2>$null
        if ($ping -eq "PONG") { Write-Host "  redis          OK" -ForegroundColor Green } else { Write-Host "  redis          DOWN" -ForegroundColor Red }
        try {
            $s = Invoke-RestMethod -Uri "$Supplier/health" -TimeoutSec 3
            Write-Host "  supplier mode: down=$($s.down) failure_rate=$($s.failure_rate)"
            $b = (Invoke-RestMethod -Uri "$Dashboard/api/metrics" -TimeoutSec 3).breaker
            Write-Host "  circuit breaker: $($b.state)"
        } catch {}
        $t = Get-TunnelUrl
        $running = docker compose --profile tunnel ps -q tunnel 2>$null
        if ($running -and $t) { Write-Host "Tunnel: RUNNING  $t/mcp" -ForegroundColor Yellow } else { Write-Host "Tunnel: off" }
    }
    "tunnel-url" {
        $t = Get-TunnelUrl
        if ($t) { Write-Host "Connector URL:  $t/mcp" -ForegroundColor Green } else { Write-Host "No tunnel running - start one with: .\make tunnel" }
    }
    "up"            { docker compose up -d --build; Write-Host "Dashboard: $Dashboard   MCP (HTTP): http://localhost:8000/mcp" }
    "down"          { docker compose down }
    "logs"          { docker compose logs -f --tail=50 }
    "test"          { uv run pytest -q }
    "smoke"         { docker compose run --rm --no-deps mcp python scripts/smoke.py --url http://mcp:8000/mcp }
    "scenario"      { docker compose run --rm --no-deps mcp python scripts/scenario.py }
    "inspector"     { npx -y @modelcontextprotocol/inspector uv run mcp-hotels }
    "spike"         { docker compose --profile loadtest run --rm --no-deps loadtest }
    "supplier-fail" { Post "$Supplier/admin/failure-rate" (@{ rate = $FailRate } | ConvertTo-Json) }
    "supplier-down" { Post "$Supplier/admin/down" $null }
    "supplier-up"   { Post "$Supplier/admin/up" $null }
    "reset"         { Post "$Dashboard/api/reset" $null }
    "tunnel" {
        Write-Host "Starting a PUBLIC Cloudflare quick tunnel to the MCP server (no auth - mock data only)..."
        docker compose --profile tunnel up -d --no-deps --force-recreate tunnel
        $url = $null
        $failed = $false
        foreach ($i in 1..30) {
            $logs = docker compose logs tunnel 2>&1 | Out-String
            # The tunnel's own hostname; api.trycloudflare.com is Cloudflare's API, never the tunnel.
            $m = [regex]::Matches($logs, 'https://[a-z0-9-]+\.trycloudflare\.com') |
                 Where-Object { $_.Value -ne "https://api.trycloudflare.com" } | Select-Object -First 1
            if ($m) { $url = $m.Value; break }
            if ($logs -match 'failed to request quick Tunnel|ERR ') { $failed = $true; break }
            Start-Sleep -Seconds 2
        }
        if ($url) {
            Write-Host ""
            Write-Host "Connector URL:  $url/mcp" -ForegroundColor Green
            Write-Host "Add it at claude.ai -> Settings -> Connectors -> Add custom connector."
            Write-Host "Stop it with:   .\make tunnel-down"
        } elseif ($failed) {
            Write-Host "The tunnel failed to start. Last log lines:" -ForegroundColor Red
            docker compose logs --tail 5 tunnel
        } else {
            Write-Host "No URL yet - check: docker compose logs tunnel"
        }
    }
    "tunnel-down"   { docker compose --profile tunnel rm -sf tunnel }
    default {
        Write-Host "Targets: up, down, status, logs, test, smoke, scenario, inspector, spike,"
        Write-Host "         supplier-fail [-FailRate 0.5], supplier-down, supplier-up, reset,"
        Write-Host "         tunnel, tunnel-url, tunnel-down       (full guide: docs\RUNBOOK.md)"
    }
}
