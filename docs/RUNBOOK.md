# Runbook: starting, stopping and handling each part

Day-to-day commands for running mcp-hotels. Every command here is typed in **PowerShell**
from the project folder:

```powershell
cd E:\AI-Lab\MCP-Hotels
```

`.\make <something>` is a shortcut defined in `make.cmd` / `scripts\tasks.ps1`. On Mac or Linux,
type `make <something>` instead. Everything else (`docker compose ...`) is the same on all systems.

> **The parts:** Redis (memory/cache) · mock supplier (fake hotel company) · MCP server (what
> Claude talks to) · dashboard (live charts) · tunnel (optional public URL). The first four run
> together in Docker. The tunnel only runs when you start it.

---

## 1. Quick reference

| I want to… | Command |
|---|---|
| Start everything | `.\make up` |
| Check what's running and healthy | `.\make status` |
| Stop everything | `.\make down` |
| Watch the logs of everything | `.\make logs` (press `Ctrl+C` to stop watching) |
| Open the dashboard | http://localhost:8002 |
| Start the public tunnel | `.\make tunnel` |
| Show the current tunnel URL | `.\make tunnel-url` |
| Stop the public tunnel | `.\make tunnel-down` |
| Break the supplier (demo) | `.\make supplier-down` |
| Fix the supplier (demo) | `.\make supplier-up` |
| Run the traffic spike (demo) | `.\make spike` |
| Zero the dashboard counters | `.\make reset` |
| Check all 4 tools work | `.\make smoke` |
| Run the automated tests | `.\make test` |

---

## 2. Daily start and stop

### Start (e.g. after turning on your PC)

1. Open **Docker Desktop** and wait until it says the engine is running.
2. Start the services:
   ```powershell
   .\make up
   ```
   The first run builds the images (a few minutes). Later runs take seconds.
3. Check:
   ```powershell
   .\make status
   ```
   You want `supplier OK`, `dashboard OK`, `redis OK`, `circuit breaker: closed`.
4. Optional, only if you want to use the phone or ChatGPT: `.\make tunnel`, then
   update the connector URL in Claude/ChatGPT (it's new every time).

### Stop

```powershell
.\make tunnel-down    # only if the tunnel is running
.\make down           # stops Redis, supplier, MCP server and dashboard
```

`.\make down` keeps nothing important: the cache and counters are rebuilt as soon as
requests come in again. Your code and settings are untouched.

### After you change code or settings

| You changed… | Run |
|---|---|
| Python code (`mcp_server/`, `supplier/`, `dashboard/`) | `.\make up` (rebuilds and restarts what changed) |
| `.env` | `docker compose up -d` (restarts services whose settings changed) |
| `dashboard/index.html` only | `.\make up`, then refresh the browser |
| Dependencies in `pyproject.toml` | `$env:UV_SYSTEM_CERTS=1; uv sync`, then `.\make up` |

---

## 3. MCP server (port 8000)

The MCP server runs in **two places**, both using the same Redis and supplier:

| Copy | Who uses it | Started by |
|---|---|---|
| Docker container `mcp` (HTTP, `http://localhost:8000/mcp`) | the tunnel → claude.ai / Claude mobile / ChatGPT, the load test | `.\make up` |
| A process started by **Claude Desktop** (stdio) | Claude Desktop on this PC | Claude Desktop itself, when it opens |

### Docker copy

```powershell
docker compose restart mcp          # restart it
docker compose up -d mcp            # restart AND re-read .env (use this after editing .env)
docker compose stop mcp             # stop just the MCP server
docker compose logs -f mcp          # follow its logs (Ctrl+C to stop following)
.\make smoke                        # call all 4 tools once to prove it works
```

Number of worker processes: `MCP_WORKERS` in `.env` (default 4).

### Claude Desktop copy

- **Start / restart:** fully quit Claude Desktop (tray icon → **Quit**) and open it again.
- **Stop:** quit Claude Desktop, or remove the `hotels` entry from its config.
- **Config** (Settings → Developer → Edit Config):
  ```json
  { "mcpServers": { "hotels": { "command": "E:\\AI-Lab\\MCP-Hotels\\.venv\\Scripts\\mcp-hotels.exe" } } }
  ```
- It needs Redis and the supplier running (`.\make up`); otherwise searches fail with
  "supplier can't be reached".

### Run it by hand (for development)

```powershell
.venv\Scripts\mcp-hotels.exe                     # stdio (what Claude Desktop does)
.venv\Scripts\mcp-hotels.exe --http --port 8010  # HTTP on another port (8000 is Docker's)
.\make inspector                                 # MCP Inspector UI over stdio
```

---

## 4. Tunnel (public URL for claude.ai, Claude mobile, ChatGPT)

```powershell
.\make tunnel         # start; prints  Connector URL: https://<random>.trycloudflare.com/mcp
.\make tunnel-url     # print the current URL again (does NOT change it)
.\make tunnel-down    # stop; the URL stops working immediately
docker compose logs -f tunnel   # watch the tunnel's log
```

Important:
- **`.\make tunnel` always creates a new URL.** If one is already running, it is replaced.
  Use `.\make tunnel-url` when you only want to see the address.
- After a new URL, **update the connector** in claude.ai (Settings → Connectors) and in
  ChatGPT (Settings → Apps & Connectors).
- A brand-new URL may need **up to a minute** before it works everywhere.
- **No login protects it.** Anyone with the URL can use the (mock) tools. Run
  `.\make tunnel-down` when you're done.
- Which websites may call it: `MCP_ALLOWED_ORIGINS` in `.env`, then `docker compose up -d mcp`.
  Restarting the MCP server does **not** change the tunnel URL.

If `.\make tunnel` says *"The tunnel failed to start"* with `certificate signed by unknown
authority`: antivirus HTTPS scanning (Avast) is in the way. The tunnel trusts certificates in
`docker\extra-ca\`. Make sure that folder contains the antivirus root certificate (`.crt`).

---

## 5. Redis (port 6379): cache, counters, breaker state

Redis holds everything shared: cached searches, the last-known-good copies, rate-limit buckets,
the circuit-breaker state and the dashboard counters. Nothing in it is precious; it all
refills by itself.

```powershell
docker compose exec redis redis-cli ping                       # -> PONG means it's alive
docker compose exec redis redis-cli --scan --pattern "mh:c:fresh:*"   # list cached answers
docker compose exec redis redis-cli ttl <key>                  # seconds until a key expires
docker compose exec redis redis-cli hgetall mh:breaker:supplier       # circuit breaker state
docker compose exec redis redis-cli                            # interactive shell (type exit to leave)
```

Clearing things (handy before a demo):

```powershell
# Clear the cache (fresh + last-known-good copies). Next searches go to the supplier again.
docker compose exec redis sh -c 'redis-cli --scan --pattern "mh:c:*" | xargs -r redis-cli del'

# Force the circuit breaker back to CLOSED
docker compose exec redis redis-cli del mh:breaker:supplier mh:breaker:supplier:failures mh:breaker:supplier:probe

# Zero the dashboard counters only
.\make reset

# Wipe EVERYTHING in Redis (cache, counters, breaker, rate limits)
docker compose exec redis redis-cli flushall
```

What the key names mean:

| Key pattern | What it is |
|---|---|
| `mh:c:fresh:search:…` / `mh:c:fresh:hotel:…` | Fresh cache (10 min searches / 24 h details) |
| `mh:c:lkg:…` | Last-known-good copy (7 days), used when the supplier is down |
| `mh:rl:<client>` | Rate-limit bucket for one client |
| `mh:breaker:supplier` | Circuit breaker state (`closed` / `open` / `half_open`) |
| `mh:m:total:<counter>` / `mh:m:sec:…` | Dashboard counters (totals / per second) |

---

## 6. Mock supplier (port 8001)

The fake hotel company. It is slow on purpose (300–800 ms per call).

```powershell
curl.exe http://localhost:8001/health         # {"ok":true,"hotels":60,"down":false,"failure_rate":0.0}
.\make supplier-down                          # every call returns 503 (supplier "offline")
.\make supplier-up                            # back to normal (also resets the failure rate)
.\make supplier-fail                          # 50 % of calls fail
.\make supplier-fail -FailRate 0.2            # 20 % of calls fail
docker compose restart supplier               # restart (also resets down/failure mode)
docker compose logs -f supplier
```

Try it directly in a browser:
- http://localhost:8001/hotels/search?city=goa&check_in=2026-12-18&check_out=2026-12-20
- http://localhost:8001/hotels/GOA-001
- http://localhost:8001/docs (automatic API documentation)

Changing the hotels: edit `scripts\gen_hotels.py`, run `uv run python scripts\gen_hotels.py`,
then `.\make up`. To add a city, also add it to `SUPPORTED_CITIES` in `mcp_server\config.py`.

---

## 7. Dashboard (port 8002)

```
http://localhost:8002                 the live page (refreshes every second)
http://localhost:8002/api/metrics     the raw numbers as JSON
```

```powershell
.\make reset                          # zero all counters (cache and breaker are untouched)
docker compose restart dashboard
docker compose logs -f dashboard
```

The dashboard only *reads* Redis. Stopping it doesn't affect the MCP server.

---

## 8. Demo controls

```powershell
.\make reset            # clean counters before you start
.\make spike            # 5 -> 500 simulated AI agents for ~80 s (Ctrl+C stops early)
.\make supplier-down    # during the spike: breaker opens, answers come from cache
.\make supplier-up      # breaker recovers after ~15 s
.\make scenario         # the whole outage + recovery story, scripted (~40 s)
```

Shorter or smaller spike, for example 200 agents held for 20 s:

```powershell
$env:SPIKE_USERS=200; $env:SPIKE_HOLD_S=20; .\make spike
Remove-Item Env:SPIKE_USERS, Env:SPIKE_HOLD_S      # back to defaults
```

---

## 9. When something is wrong

| Symptom | Fix |
|---|---|
| `.\make status` shows services DOWN, or `docker` errors | Start Docker Desktop, wait, then `.\make up`. If Docker is stuck: `docker desktop restart`. |
| Claude says "supplier can't be reached" | `.\make supplier-up`; check `curl.exe http://localhost:8001/health` |
| Dashboard shows breaker **OPEN** but the supplier is up | Wait ~15 s (it tests one request, then closes), or reset it (section 5) |
| Claude Desktop doesn't show the hotels tools | Check the config path (section 3), quit Claude Desktop fully, reopen |
| Phone/ChatGPT connector stopped working | The tunnel URL changed or stopped: `.\make tunnel-url`; if none, `.\make tunnel` and update the connector |
| ChatGPT/website gets **403 Invalid Origin** | Add its address to `MCP_ALLOWED_ORIGINS` in `.env`, then `docker compose up -d mcp` |
| `running scripts is disabled on this system` | Type `.\make …` (uses `make.cmd`), not `.\scripts\tasks.ps1` |
| Port already in use (8000/8001/8002/6379) | Another program uses it. Find it: `Get-NetTCPConnection -LocalPort 8000 -State Listen` |
| `invalid peer certificate: UnknownIssuer` | Antivirus HTTPS scanning: use `$env:UV_SYSTEM_CERTS=1` for uv (see README → Troubleshooting) |
| Want a completely fresh start | `.\make tunnel-down; .\make down; .\make up` |
