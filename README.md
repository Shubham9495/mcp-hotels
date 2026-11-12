# mcp-hotels

A hotel-search **MCP server** that Claude can talk to in plain language ("Find me a hotel in
Goa under ₹5,000 this weekend"), built to stay up when AI agents flood it with traffic.

> **All data is fake.** Hotels, prices and bookings are generated locally. Nothing here
> calls a real travel site.

What it demonstrates:

| Problem | What this project does | Where |
|---|---|---|
| AI agents repeat the same searches thousands of times | Redis cache (10 min) + **request coalescing** (identical concurrent searches → one supplier call) | `mcp_server/cache.py` |
| One noisy client can starve everyone | **Token-bucket rate limit** per client id | `mcp_server/ratelimit.py` |
| The supplier goes down | **Circuit breaker** + **stale fallback** (last known good prices, clearly marked) | `mcp_server/breaker.py` |
| Overload | **Priority load shedding**: bookings > live price checks > searches | `mcp_server/shedding.py` |
| Supplier cost per booking | **Look-to-book** shown with and without the cache | `dashboard/` |

New to all this? Read **[docs/CONCEPTS.md](docs/CONCEPTS.md)** (the ideas, in plain words)
and **[docs/FILES.md](docs/FILES.md)** (what every file does).

---

## How it fits together

```
 Claude Desktop / Claude Code                Locust (make spike)
        │ stdio                                   │ HTTP
        ▼                                         ▼
 ┌──────────────────────────── MCP server (port 8000) ─────────────────────────┐
 │  tools: search_hotels · get_hotel_details · check_live_price · book_hotel   │
 │  rate limit ─► cache ─► coalescing ─► circuit breaker ─► load shedding      │
 └─────────────┬───────────────────────────────────────────────┬──────────────┘
               │ cache, counters, breaker state, rate buckets    │ only on cache miss
               ▼                                                 ▼
         Redis (6379)  ◄──── reads ──── Dashboard (8002)   Mock supplier (8001)
                                                           slow (300–800 ms), can fail
```

---

## 1. Setup

You need:

- **Docker Desktop** (runs Redis, the supplier, the MCP server and the dashboard)
- **uv** (Python package manager): `pip install uv`, or see https://docs.astral.sh/uv/
- **Node.js** (only for MCP Inspector)
- `make` is optional. On Windows, use `.\make <target>` instead of `make <target>`. (`make.cmd` runs `scripts	asks.ps1`
  for you, so Windows' "running scripts is disabled" policy doesn't get in the way.)

```bash
git clone <this repo> mcp-hotels && cd mcp-hotels
uv sync                      # installs Python 3.11+ deps into .venv (uv fetches Python if needed)
make up                      # or: .\make up
```

Then open the dashboard at **http://localhost:8002**.

| Service | URL |
|---|---|
| Dashboard | http://localhost:8002 |
| MCP server (HTTP) | http://localhost:8000/mcp |
| Mock supplier | http://localhost:8001/health |
| Redis | localhost:6379 |

Run the tests with `make test` (24 tests: cache, coalescing, breaker, rate limiter, shedding).

---

## 2. Connect Claude

### Claude Desktop (stdio)

Claude Desktop starts the server itself as a local process. The server then talks to Redis
and the supplier running in Docker. Open *Settings → Developer → Edit Config* and add:

```json
{
  "mcpServers": {
    "hotels": {
      "command": "uv",
      "args": ["--directory", "E:\\AI-Lab\\MCP-Hotels", "run", "mcp-hotels"]
    }
  }
}
```

Change the path to wherever you cloned the repo. If Claude Desktop can't find `uv`, put the
full path to `uv.exe` in `command` (run `where uv` to find it).

**If the server log shows `invalid peer certificate: UnknownIssuer`** (antivirus or corporate
HTTPS inspection), `uv run` is trying to download build tools at startup. Skip `uv` entirely
and start the already-installed executable from the project's virtualenv (run `uv sync` once first):

```json
{
  "mcpServers": {
    "hotels": {
      "command": "E:\\AI-Lab\\MCP-Hotels\\.venv\\Scripts\\mcp-hotels.exe"
    }
  }
}
``` Restart Claude Desktop, and
the four hotel tools appear under the tools icon.

### Claude Code

```bash
# stdio (recommended locally)
claude mcp add hotels -- uv --directory /path/to/mcp-hotels run mcp-hotels

# or over HTTP, using the server running in Docker
claude mcp add --transport http hotels-http http://localhost:8000/mcp
```

Check it with `claude mcp list`, then ask Claude for a hotel.

### MCP Inspector (a debugging UI for MCP servers)

```bash
make inspector            # opens a browser UI connected over stdio
```

### Optional: use it as a remote connector (ngrok / Cloudflare Tunnel)

To use the server from claude.ai or the Claude mobile app as a custom connector, it needs a
public HTTPS URL. A Cloudflare quick tunnel is built in (no account needed). It runs inside
Docker, so the traffic never goes through Windows' network filters:

```bash
make up
make tunnel            # Windows: .\make tunnel
# -> Connector URL: https://<random-words>.trycloudflare.com/mcp
```

1. On **claude.ai in a web browser**, go to **Settings → Connectors → Add custom connector**, give
   it a name (e.g. "Mock Hotels"), and paste the connector URL. No OAuth settings are needed.
2. Connectors added on claude.ai also appear in the **Claude mobile app** on the same account.
   Turn it on in a chat and ask for a hotel. Your PC must stay on, with Docker running.
3. When you're done: `make tunnel-down`. Each `make tunnel` gets a **new random URL**, so update
   the connector after restarting the tunnel.

How it works: Claude's servers call the tunnel URL, and Cloudflare forwards the request to the
`mcp` container. The tunnel rewrites the Host header to `mcp:8000`, so the server's
DNS-rebinding check accepts it. `Origin: https://claude.ai` is allowed (`MCP_ALLOWED_ORIGINS`),
and rate limits use the caller's real IP from Cloudflare's `Cf-Connecting-Ip` header.

Using ngrok instead (`ngrok http 8000`): add the ngrok hostname to `MCP_ALLOWED_HOSTS` in
`.env`, then run `make up`.

⚠️ This demo server has **no authentication**. Anyone with the URL can call it. The data is
fake and rate limits apply, but stop the tunnel when you're done.

---

## 3. Demo script

1. **`make up`**, then open http://localhost:8002. Click *Reset counters* for a clean slate.
2. In Claude: *"Find me a 4-star hotel in Goa under ₹6,000 for this Friday to Sunday for 2
   people."* Show the tool calls: `search_hotels` (city, dates, `max_price_inr`, `min_stars`),
   then perhaps `check_live_price`.
3. **`make spike`**: 500 simulated AI agents ramp up in about 20 s. Watch the dashboard:
   - *MCP requests/s* jumps to several hundred, while *Supplier looks/s* stays low.
   - The *cache hit rate* climbs past 90 %.
   - *Rate-limited* goes up: a "noisy bot" in the load test gets throttled while everyone else is served.
4. **`make supplier-down`** while the spike is still running. Misses fail, the breaker turns
   **OPEN** on the dashboard, and Claude gets answers from the cache. Search results come back
   with `"source": "cache"` or `"stale_cache"`, `stale: true` once the fresh copy has expired,
   and a notice that the supplier is down. `check_live_price` refuses rather than guessing.
   Ask Claude the same question again; it should say the prices are cached.
5. **`make supplier-up`**. After the cooldown (15 s), one probe request goes through, the
   breaker goes **Half-open**, then **Closed**.
6. Point at the **look-to-book** panel. Without the cache, every AI search would have hit the
   supplier; with it, only a small share does.

Other knobs: `make supplier-fail` (50 % of supplier calls return 503; set `FAIL_RATE=0.2` to
change), `make scenario` (the whole failure/recovery story scripted, no Claude needed),
`make smoke` (calls every tool once).

### Typical numbers (laptop, 4 workers)

Measured with `make spike` (500 agents):

| | |
|---|---|
| MCP throughput | ~500 requests/s |
| Supplier looks | ~60/s (mostly uncached live-price checks + bookings) |
| Cache hit rate | ~93 % |
| Search latency | p50 12 ms, p95 ~480 ms |
| Load-shed / supplier errors | 0 / 0 |

---

## 4. Tools exposed to Claude

| Tool | What it does | Cached? |
|---|---|---|
| `search_hotels(city, check_in, check_out, guests=2, max_price_inr=None, min_stars=None, limit=5)` | Up to 10 hotels, cheapest first, each with `price_as_of` and `stale` | 10 min (+ 7-day stale copy) |
| `get_hotel_details(hotel_id)` | Name, area, stars, amenities, description | 24 h |
| `check_live_price(hotel_id, check_in, check_out, guests)` | Price straight from the supplier (the "checkout" check) | **never** |
| `book_hotel(hotel_id, check_in, check_out, guest_name, guests)` | **Mock** booking: re-checks the price and returns a fake confirmation id | never |

Cities: Goa, Manali, Jaipur, Udaipur, Kochi, Rishikesh. Bad cities, dates or ids get a
friendly error the model can read and fix.

---

## 5. Configuration

Every setting is an environment variable with a sensible default. See
[`.env.example`](.env.example); copy it to `.env` to change values. The most useful ones:

| Variable | Default | Meaning |
|---|---|---|
| `CACHE_SEARCH_TTL_S` | 600 | How long search results count as fresh |
| `RATE_LIMIT_BURST` / `RATE_LIMIT_PER_S` | 30 / 10 | Token bucket per client |
| `BREAKER_FAILURE_THRESHOLD` / `BREAKER_WINDOW_S` / `BREAKER_COOLDOWN_S` | 5 / 30 / 15 | Open after 5 failures in 30 s; probe after 15 s |
| `SUPPLIER_MAX_INFLIGHT` | 40 | Concurrent supplier calls per worker before shedding |
| `MCP_WORKERS` | 4 | HTTP worker processes |

Over HTTP the rate-limit identity is the `X-Client-Id` header, falling back to the MCP
session id and then the client IP. Over stdio it is `stdio`.

---

## 6. Troubleshooting

- **`invalid peer certificate: UnknownIssuer` during `uv sync` or `docker compose build`.**
  Antivirus or corporate HTTPS inspection is in the way.
  - For uv: `set UV_SYSTEM_CERTS=1` (PowerShell: `$env:UV_SYSTEM_CERTS=1`).
  - For Docker builds: export the inspecting root certificate as PEM into
    `docker/extra-ca/` (any `*.crt`, gitignored). The Dockerfile trusts it.
- **HTTP calls from Windows to `localhost:8000/mcp` fail with "peer unexpectedly closed
  connection".** Some antivirus web shields (seen with Avast) rewrite local HTTP traffic that
  looks like MCP and corrupt the response. Use stdio from the host (Claude Desktop, Claude
  Code, Inspector), which never touches the network. Run HTTP clients inside Docker, as
  `make smoke`, `make scenario` and `make spike` already do. Alternatively, add an
  exception in the antivirus.
- **Claude says the tools failed with "supplier can't be reached".** Run `make up` and check
  http://localhost:8001/health. If you ran `make supplier-down`, run `make supplier-up`.
- **Docker Desktop shows "unable to start".** Quit and restart Docker Desktop
  (`docker desktop restart`).
