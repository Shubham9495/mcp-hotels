# mcp-hotels demo commands. On Windows without `make`, use:  .\make <target>
SUPPLIER   ?= http://localhost:8001
DASHBOARD  ?= http://localhost:8002
FAIL_RATE  ?= 0.5

.PHONY: help up down logs test smoke scenario inspector spike supplier-fail supplier-down supplier-up reset tunnel tunnel-down

help:            ## list targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/ -/'

up:              ## build and start redis, supplier, MCP server, dashboard
	docker compose up -d --build
	@echo "Dashboard: $(DASHBOARD)   MCP (HTTP): http://localhost:8000/mcp"

down:            ## stop everything
	docker compose down

logs:            ## follow service logs
	docker compose logs -f --tail=50

test:            ## unit tests (cache, coalescing, breaker, rate limiter, shedding)
	uv run pytest -q

smoke:           ## call every tool once over HTTP (inside the Docker network)
	docker compose run --rm --no-deps mcp python scripts/smoke.py --url http://mcp:8000/mcp

scenario:        ## scripted failure + recovery walk-through
	docker compose run --rm --no-deps mcp python scripts/scenario.py

inspector:       ## open MCP Inspector against the stdio server
	npx -y @modelcontextprotocol/inspector uv run mcp-hotels

spike:           ## 5 -> 500 simulated AI agents in ~20 s (Locust, headless)
	docker compose --profile loadtest run --rm --no-deps loadtest

supplier-fail:   ## make FAIL_RATE (default 0.5) of supplier calls return 503
	curl -s -X POST $(SUPPLIER)/admin/failure-rate -H "content-type: application/json" -d '{"rate": $(FAIL_RATE)}'; echo

supplier-down:   ## take the supplier fully down
	curl -s -X POST $(SUPPLIER)/admin/down; echo

supplier-up:     ## bring the supplier back (also clears the failure rate)
	curl -s -X POST $(SUPPLIER)/admin/up; echo

reset:           ## zero dashboard counters
	curl -s -X POST $(DASHBOARD)/api/reset; echo

tunnel:          ## PUBLIC https URL (Cloudflare quick tunnel) for a Claude custom connector - no auth!
	docker compose --profile tunnel up -d --no-deps --force-recreate tunnel
	@for i in $$(seq 1 30); do \
	  url=$$(docker compose logs tunnel 2>&1 | grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' | grep -v '//api\.' | head -1); \
	  if [ -n "$$url" ]; then echo "Connector URL: $$url/mcp  (stop with: make tunnel-down)"; exit 0; fi; sleep 2; \
	done; echo "No URL yet - check: docker compose logs tunnel"

tunnel-down:     ## stop the public tunnel
	docker compose --profile tunnel rm -sf tunnel
