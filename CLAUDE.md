# mcp-hotels: notes for Claude Code

Demo hotel-search MCP server, built for an interview with an online travel company's hotels
team. It shows: natural-language hotel search via MCP; stability under AI traffic spikes (rate
limits, caching, coalescing); graceful degradation (circuit breaker + stale cache); and a
look-to-book ratio kept low by caching.

**All data is MOCK. Never scrape or call any real travel site or external travel API.**

## Layout
- `supplier/`: mock supplier API (FastAPI, port 8001) + `data/hotels.json` (130 fake hotels, 13 cities)
- `mcp_server/`: MCP server (mcp SDK v2 `MCPServer`, formerly FastMCP; stdio or HTTP on 8000)
  - `server.py` tool definitions only (thin) · `search.py` cache/breaker/shedding orchestration
  - `cache.py` Redis cache + SingleFlight · `ratelimit.py` · `breaker.py` · `shedding.py`
  - `metrics.py` counter names + Redis counters · `config.py` env settings · `validation.py`
- `dashboard/`: metrics page (port 8002) · `loadtest/locustfile.py` · `scripts/` · `tests/`
- File-by-file guide: `docs/FILES.md`; concepts: `docs/CONCEPTS.md`; operations: `docs/RUNBOOK.md`; design: `docs/ARCHITECTURE.md`

## Commands
- `uv sync` · `uv run pytest -q` · `make up|down|spike|scenario|smoke|inspector|supplier-down|supplier-up`
- Windows without make: `.\make <target>`
- stdio server: `uv run mcp-hotels`; HTTP: `uv run mcp-hotels --http --workers 4`

## Conventions
- Python 3.11+, type hints everywhere, small functions.
- No global mutable state except clearly named singletons (`SERVICE` in server.py, `STATE` in supplier).
- All config via env vars with defaults; document new ones in `.env.example`.
- Shared state (cache, rate buckets, breaker, metrics) lives in Redis so stdio and HTTP
  processes and multiple workers agree. Coalescing and shedding are per process.
- Tool descriptions are written for the model: specific about inputs, outputs and when to use them.
- Tool errors must be friendly and actionable (raise `ToolError`); never let tracebacks reach logs
  at high volume (see the logging note in server.py).
- Add pytest tests for cache, coalescing and breaker logic changes (`fakeredis` + `FakeSupplier`).
- Never print secrets.

## Environment gotchas (developer machine)
- Avast's web shield corrupts host→localhost HTTP MCP traffic. Use stdio on the host and
  run HTTP clients inside Docker (`make smoke/scenario/spike` already do).
- HTTPS inspection: uv needs `UV_SYSTEM_CERTS=1`; Docker builds trust `docker/extra-ca/*.crt`.
- Git Bash rewrites `/paths` in docker exec args; prefix with `MSYS_NO_PATHCONV=1`.
