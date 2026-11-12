# Concepts, explained simply

Each section explains one idea used in this project: what it is, why a travel company
cares, and where it lives in the code.

---

## MCP (Model Context Protocol)

**What:** A standard way for an AI model like Claude to use outside tools. A program called
an *MCP server* publishes a list of tools (name + description + inputs). Claude reads the
list, decides which tool fits the user's request, and calls it with arguments. Here,
*"hotel in Goa under ₹5,000 this weekend"* becomes
`search_hotels(city="Goa", check_in="2026-11-13", check_out="2026-11-15", max_price_inr=5000)`.

**Two ways to connect ("transports"):**
- **stdio**: Claude Desktop/Code starts the server as a child process and they talk through
  its standard input and output. Simple and local.
- **Streamable HTTP**: the server listens on a URL (`http://localhost:8000/mcp`). Needed for
  remote use and for many clients at once. Ours is *stateless*: each request is
  self-contained, so any worker process can answer it, which makes scaling out easy.

**Why descriptions matter:** the model only knows what a tool does from its description.
Vague descriptions lead to wrong tool choices. See the descriptions in `mcp_server/server.py`.

---

## The supplier and "looks"

A real online travel agency doesn't own hotel rooms. It asks *suppliers* (hotel chains,
wholesalers) for availability and prices. Every such question is a **look**. Suppliers
care how many looks you send compared with how many bookings you produce: the
**look-to-book ratio**. Too many looks per booking and they throttle you or charge you.

AI agents make this worse: an agent may search ten variations before answering, and thousands
of users ask the same "Goa this weekend" question. Without protection, every one of those
reaches the supplier.

**In this project:** the dashboard shows the ratio *without cache* (every look the AI asked
for) and *with cache* (looks that actually reached the supplier).

---

## Caching

**What:** Remember an answer for a while and reuse it instead of asking again. We store answers
in **Redis**, a very fast in-memory database shared by all server processes.

**How long?** It's a trade-off between freshness and load:
- Search results: **10 minutes**. Prices drift, but slowly enough for browsing.
- Hotel details (name, amenities): **24 hours**. They hardly change.
- Live price before booking: **never cached**. Booking at a wrong price is worse than a slow answer.

Each search result carries `price_as_of`, so the user and the model know how old the price is.

**Code:** `mcp_server/cache.py`, used by `mcp_server/search.py`.

---

## Request coalescing ("single-flight")

**Problem:** the cache is empty (say it just expired) and 200 agents ask the same question in
the same second. All 200 would miss the cache and call the supplier: a *cache stampede*.

**Fix:** the first request becomes the "leader" and calls the supplier. The other 199 see a call
for the same key already in progress and simply wait for its answer. **One** supplier call
instead of 200.

**Code:** `SingleFlight` in `mcp_server/cache.py`. The test
`test_spike_of_identical_searches_makes_one_supplier_call` proves it.

---

## Rate limiting (token bucket)

**What:** Each client has a bucket that holds up to 30 tokens and refills at 10 tokens per
second. Each request takes one token. Empty bucket → the request is refused with
*"Rate limited, retry after N s"*.

**Why a bucket?** It allows short bursts (an agent firing off 5 searches at once is normal)
but caps the sustained rate, so one misbehaving client can't use up capacity meant for
everyone.

**Who is "a client"?** The `X-Client-Id` header, otherwise the MCP session, otherwise the
caller's IP. `book_hotel` is never rate limited, because bookings are what the business exists for.

**Code:** `mcp_server/ratelimit.py`. The bucket logic runs as a Lua script *inside* Redis so
two simultaneous requests can't both take the last token.

---

## Circuit breaker

**Problem:** the supplier is down. If we keep calling it, every request waits for a timeout
(slow for users) and we pile more load onto a system that is already struggling.

**Fix:** a breaker, like the one in your home's fuse box:
- **Closed** (normal): calls flow through. Failures are counted.
- **Open**: after 5 failures within 30 s, stop calling the supplier at all. Answer from cache
  or fail fast with a clear message.
- **Half-open**: after a 15 s cooldown, let **one** test request through. If it works →
  closed. If it fails → open again for another cooldown.

The breaker's state lives in Redis, so every server process and the dashboard agree on it.

**Code:** `mcp_server/breaker.py`.

---

## Graceful degradation (stale fallback)

**What:** when the supplier can't answer, give the best *honest* answer we have instead of
an error. Every cached answer is stored twice: a fresh copy (10 min) and a
**last-known-good** copy (7 days). If the supplier fails and the fresh copy has expired, we
serve the old copy marked `stale: true`, with a `notice` telling the model the prices may
have changed.

**Honesty rule:** `check_live_price` never pretends. If the supplier is down it says it
can't confirm the price, so nobody books at a stale price.

**Code:** `HotelService._cached()` in `mcp_server/search.py`.

---

## Load shedding with priorities

**Problem:** even with a cache, too many cache misses at once can pile up supplier calls and
make everything slow, including bookings.

**Fix:** cap concurrent supplier calls (40 per worker) and refuse low-value work first:
searches may use up to 60 % of the slots, live price checks 90 %, bookings 100 %. A refused
search still falls back to stale cache when possible. Under pressure, the system protects
revenue first.

**Code:** `mcp_server/shedding.py`.

---

## Fail open vs fail closed

When a *protection* component breaks (for example Redis is unreachable), should requests be
blocked (fail closed) or allowed (fail open)? The rate limiter and breaker here **fail
open**: losing the protection briefly is better than taking the whole service down. A bank
might choose differently for, say, fraud checks.

---

## Load testing

**What:** simulate many users to see how the system behaves before real users do. `make
spike` uses **Locust** to ramp from 5 to 500 simulated AI agents in about 20 seconds.
Things to watch on the dashboard: MCP requests/s goes up a lot, supplier looks/s barely
moves, the cache hit rate climbs, and the noisy bot gets rate limited.

A real finding from building this: under the first spike, the server froze at 100 % CPU.
Profiling (`py-spy`) showed the time going into the SDK's pretty-printed error tracebacks,
triggered by a too-small Redis connection pool. The fixes (plain logging, a blocking
connection pool, several worker processes) are explained in comments in
`mcp_server/server.py` and `mcp_server/redis_client.py`. This is exactly why load tests exist.
