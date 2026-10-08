# SIG fixture recorder (S1-T4-A)

`engine/sig_client/record_fixtures.py` records a **small, bounded, read-only** sample of
SIG responses through `SigRestClient`, so the client can later be checked against what
SIG actually sends, not only against the OpenAPI examples in `tests/fixtures/sig/`.

Only Arnav runs it against SIG (CLAUDE.md, hard rule 1). Development and tests use
`httpx.MockTransport`; any run with an injected transport is labelled `synthetic`.

## Usage

Preview is the default. It reads no credentials, creates no files and sends no
requests:

```bash
python -m engine.sig_client.record_fixtures --tournament-slug midterm-elections --tournament-id bda92870-621e-47b0-bc3c-3602c5c26f55 --exchange-id <EXCHANGE_ID> --market-id <MARKET_ID>
```

Add `--capture` to record. That reads `SUSQ_API_KEY` through `engine/config.py` and
sends GET requests to SIG. Run `--help` for every option.

| Option | Default | Bound |
| --- | --- | --- |
| `--tournament-slug`, `--tournament-id` | required | checked against `GET /tournaments/{slug}` |
| `--exchange-id` (repeat) | required | 1–10, digits, no duplicates |
| `--market-id` (repeat) | required | 1–10, digits, no duplicates |
| `--markets-page-limit` / `--markets-max-pages` | 10 / 1 | 1–100 / 1–3 |
| `--orders-page-limit` / `--orders-max-pages` | 10 / 1 | 1–50 / 1–3 |
| `--orders-status` | `open` | `open`, `closed`, `expired`, `all` |
| `--skip-orders` | off | omits `GET /orders` entirely |
| `--depth` | 5 | 1–20 levels per side |
| `--output-root` | `sig_captures/` | a new run directory is created inside it |

Arguments are validated before credentials are read or any request is possible.
Rejected values are never echoed.

## What one run reads

In order, each through the existing client and its retries, deadlines and validation:

1. `GET /tournaments/{slug}`: also the slug/UUID verification for every later read.
2. `GET /tournaments/{slug}/markets`: up to `--markets-max-pages` pages.
3. `GET /exchanges/prices?ids=…&tournamentId=…`: all selected exchanges.
4. `GET /exchanges/{id}/orderbook`: once per selected exchange.
5. `GET /markets/{id}/orderbook`: once per selected market.
6. `GET /orders?tournamentId=…`: up to `--orders-max-pages` pages (unless skipped).

No POST or DELETE, no orders, cancels, fills, positions or realtime tokens. A request
hook refuses, before sending, anything that is not a GET to a planned path under the
configured base URL, or a scoped read without exactly the configured `tournamentId`.

**Rate budget.** The recorder runs in its own process, so it cannot share the bot's
limiter. It uses one `RateLimiter` of its own for every read: 15 reads a minute, burst
1 (one read every 4 s), so it and the bot's 80 stay under SIG's 100. The largest
possible plan is 28 reads, about two minutes. Any HTTP 429 stops the run. A blocked
limiter (a Retry-After too long to hold) stops the run with an explanation; the
recorder never calls `clear_block()`.

## How responses are observed

`rest.py` is unchanged. The recorder builds the httpx client with the same settings as
`SigRestClient.create` and adds two httpx event hooks:

- the request hook (above) reads only the method and URL, never headers;
- the response hook reads the body. httpx would read it next anyway, inside the same
  `send()` and therefore inside the client's read deadline. The client then decodes the
  same cached bytes.

## Capture format

```
sig_captures/<UTC time>-<random>/
  .gitignore          "*": the whole run is ignored by git wherever it is written
  manifest.json       written atomically after every step
  bodies/NN-<step>-a<attempt>.json
```

`bodies/` holds **2xx bodies only, byte-for-byte** as received (after HTTP content
decoding). They are never parsed and re-serialised, so every number keeps its exact
digits (`0.535`, `12.50`, `1E-3`), and nulls and key order are unchanged. A body is
stored only if it decodes under the client's own rules (`parse_float=Decimal`, no
NaN/Infinity) and is at most 8 MiB. Otherwise only its size and SHA-256 are kept.

`manifest.json` (format `bellwether.sig-capture/1`) records:

| Field | Content |
| --- | --- |
| `provenance.kind` | `recorded` (genuine SIG run, no injected transport) or `synthetic` |
| `run` | id, `status`, `stop_reason`, start/finish times (UTC) |
| `code`, `spec` | git commit and dirty flag; `docs/api/openapi.json` SHA-256 and version |
| `target`, `selection`, `limits`, `plan` | base URL, tournament, options, planned reads |
| `steps[]` | route, safe params, `outcome`, `client_validation_passed`, error, page info |
| `steps[].attempts[]` | path, allowlisted params, HTTP status, allowlisted headers, body file + SHA-256, or the error body's shape and documented code |
| `collections` | per paginated read: pages fetched, `complete`, `truncated`, note |
| `not_run`, `anomalies` | planned reads never attempted; pagination inconsistencies |
| `transformations` | every deliberate omission, listed below |
| `review` | `sanitized: false`, `required_before_commit: true` |

`load_replayable(run_dir)` returns the bodies of steps that passed client validation,
checked against their SHA-256, ready to serve through `httpx.MockTransport`.

## What is never recorded

- Request headers: no `Authorization`, API key, cookie, or complete request object.
- Response headers other than the media type and a delta-seconds `Retry-After`: no
  `Set-Cookie`, `Location`, server or tracing headers.
- Non-2xx bodies: only the status, the body's shape and a *documented* SIG error code.
  SIG's message text and unknown codes are dropped.
- Exception or error text: only client error classes, documented codes, the client's
  fixed descriptions and its validation summaries (field locations and error types,
  never values).
- Query parameters outside an allowlist (`cursor`, `depth`, `exchangeId`, `ids`,
  `limit`, `marketId`, `status`, `tournamentId`); only their count is kept.

## Outcomes and exit status

| Run status | Exit | Meaning |
| --- | --- | --- |
| `succeeded` | 0 | every planned read ran and passed client validation |
| `partial` | 1 | the run finished, but some read failed (validation, 400/404) or pagination was inconsistent |
| `aborted` | 1 | stopped early: auth, 429, blocked limiter, tournament mismatch, transport, deadline, 5xx |
| `error` | 1 | a recorder failure (for example a disk write); temporary files are removed |
| `interrupted` | 130 | cancelled or Ctrl+C; the step in flight is marked `interrupted` |

The manifest is rewritten after every step, so a crash leaves `in_progress`, which is
never mistaken for success. A validation failure is never hidden. The step is
`validation_failed` and its body is kept **locally** as evidence, but `load_replayable`
excludes it, and the client is not loosened. A collection is `complete` only when its
last page said `hasMore: false`. Stopping at the page cap is recorded as `truncated`:
a sample, not the full collection. An existing run directory is never reused or
overwritten.

## Before a capture becomes a committed fixture

A capture is **not sanitised**. Removing credentials does not make it safe to publish.

- The tournament body includes the account's balance (`myBalance`), and orders bodies
  are the account's own orders. Decide what may be committed, and redact by hand only
  if the manifest then records the redaction.
- Keep the manifest with any committed body, so its provenance (`recorded`, commit,
  spec hash, time) stays attached. Never relabel synthetic data as recorded.
- Captures stay out of git by default: the root `.gitignore` lists `sig_captures/`, and
  each run directory carries its own `*` ignore file. Committing one is a deliberate
  act, reviewed by Arnav.
