# Windows equivalent of the Makefile (no `make` needed).
#   .\make.ps1 up        .\make.ps1 spike        .\make.ps1 supplier-down
param(
    [Parameter(Position = 0)][string]$Target = "help",
    [double]$FailRate = 0.5
)
$ErrorActionPreference = "Stop"
$Supplier = "http://localhost:8001"
$Dashboard = "http://localhost:8002"

function Post($url, $body) {
    if ($body) { Invoke-RestMethod -Method Post -Uri $url -ContentType "application/json" -Body $body | ConvertTo-Json -Compress }
    else { Invoke-RestMethod -Method Post -Uri $url | ConvertTo-Json -Compress }
}

switch ($Target) {
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
        docker compose --profile tunnel up -d --no-deps tunnel
        $url = $null
        foreach ($i in 1..30) {
            $m = docker compose logs tunnel 2>&1 | Select-String -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' | Select-Object -First 1
            if ($m) { $url = $m.Matches[0].Value; break }
            Start-Sleep -Seconds 2
        }
        if ($url) {
            Write-Host ""
            Write-Host "Connector URL:  $url/mcp" -ForegroundColor Green
            Write-Host "Add it at claude.ai -> Settings -> Connectors -> Add custom connector."
            Write-Host "Stop it with:   .\make.ps1 tunnel-down"
        } else {
            Write-Host "No URL yet - check: docker compose logs tunnel"
        }
    }
    "tunnel-down"   { docker compose --profile tunnel rm -sf tunnel }
    default {
        Write-Host "Targets: up, down, logs, test, smoke, scenario, inspector, spike,"
        Write-Host "         supplier-fail [-FailRate 0.5], supplier-down, supplier-up, reset, tunnel, tunnel-down"
    }
}
