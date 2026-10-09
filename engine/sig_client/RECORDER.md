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
configured base URL, a scoped read without exactly the configured `tournamentId`, and
any URL that holds the active credential.

**Logical reads and HTTP attempts.** A logical read is one page or object request
above. The largest plan is 28 logical reads (1 tournament, 3 market pages, prices, 10
exchange books, 10 market books, 3 order pages). Each read may take up to 4 HTTP
attempts under the client's retry policy (for 503 and transient transport failures),
so a run sends at most 112 HTTP requests. The preview prints both numbers for the
chosen options. At one admission every 4 s, 28 reads take about two minutes, and
retries add to that.

**Rate budget: capture standalone.** The recorder uses one `RateLimiter` of its own for
every attempt: 15 a minute, burst 1. That limiter lives in the recorder's process and
**cannot see** the trading bot, the logger, `sig_dump.py` or any other client of the
same account; it coordinates nothing with them. SIG's limit is per account, across all
keys. Run a capture only while every other same-account client is stopped.

**Rate limiting stops the run.** After any HTTP 429 the recorder sends no further
request in that capture. The client still handles the 429 as usual: it imposes the
`Retry-After` cooldown, or blocks the limiter for an oversized one, before the request
hook refuses its retry. The recorder never calls `clear_block()`. A 429 or a block is
reported as `aborted` with an explanation. Do not start another capture to get around
a server cooldown or a blocked limiter; wait until SIG's limit has passed and nothing
else is using the account.

## How responses are observed

`rest.py` is unchanged. The recorder builds the httpx client with the same settings as
`SigRestClient.create`, wraps its transport, and adds two httpx event hooks:

- `_BoundedTransport` wraps the transport httpx would use anyway and adds no retries or
  request handling. It reads each response body, decoded (so a compressed body counts
  at its decoded size), and stops at the 8 MiB limit, for every status. The stream is
  closed whether the read completes, overflows, fails or is cancelled. A response hook
  could not do this, because `aread()` reads the whole body first. The read runs inside
  the client's `send()`, so it stays under the client's read deadline.
- The request hook (above) reads only the method and URL, never headers.
- The response hook keeps the status, allowlisted headers and, for 2xx, the decoded
  body bytes in memory, and notes any 429. The client decodes exactly the same bytes.

**Credential check.** Before anything from a response is written, the recorder looks
for the active API key, in memory only, in:
- the raw body bytes;
- every decoded JSON key and string, which catches `\u` escapes, `\/` and UTF-16 bodies;
- the kept header values;
- each next-page cursor, before it is sent.

A match means nothing derived from that response is written: no body file, no
temporary file, no hash. The step becomes `credential_detected`, the run stops
(`aborted`, exit 1) with a fixed message, and the key should be treated as exposed.
As a last line of defence, every manifest string is checked again; a match is
replaced with a fixed marker, counted in `withheld_fields`, and the run fails. Bodies
are never sanitised: a body is stored byte-for-byte or not at all.

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
stored only if it passed the credential check and decodes under the client's own rules
(`parse_float=Decimal`, no NaN/Infinity). Otherwise only its size is kept.

`manifest.json` (format `bellwether.sig-capture/1`) records:

| Field | Content |
| --- | --- |
| `provenance.kind` | `recorded` (genuine SIG run, no injected transport) or `synthetic` |
| `run` | id, `status`, `stop_reason`, start/finish times (UTC) |
| `code`, `spec` | git commit and dirty flag; `docs/api/openapi.json` SHA-256 and version |
| `target`, `selection`, `limits`, `plan` | base URL, tournament, options, planned reads |
| `steps[]` | route, safe params, `outcome`, `client_validation_passed`, error, page info |
| `steps[].attempts[]` | path, allowlisted params, attempt outcome, HTTP status, allowlisted headers, body file + SHA-256, or the error body's shape and documented code |
| `collections` | per paginated read: pages fetched, `complete`, `truncated`, note |
| `not_run`, `anomalies` | planned reads never attempted; pagination inconsistencies |
| `refused_requests`, `refused_after_429` | requests the hook refused before sending |
| `credential_detected`, `withheld_fields` | credential check results |
| `transformations` | every deliberate omission, listed below |
| `review` | `sanitized: false`, `required_before_commit: true` |

`load_replayable(run_dir)` returns the bodies of steps whose outcome is `passed`,
checked against their SHA-256, ready to serve through `httpx.MockTransport`.

## What is never recorded

- The active API credential, in any form the credential check covers (above).
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
| `succeeded` | 0 | every planned read ran, passed client validation and was stored complete |
| `partial` | 1 | the run finished, but some read failed (validation, 400/404, body over the limit), pagination was inconsistent, or a manifest field was withheld |
| `aborted` | 1 | stopped early: credential detected, any 429, blocked limiter, auth, tournament mismatch, transport, deadline, 5xx |
| `error` | 1 | a recorder failure (for example a disk write); temporary files are removed |
| `interrupted` | 130 | cancelled or Ctrl+C; the step in flight is marked `interrupted` |

The manifest is rewritten after every step, so a crash leaves `in_progress`, which is
never mistaken for success. Failures are never hidden:

- **Validation failure:** the step is `validation_failed` and its body is kept
  **locally** as evidence, but `load_replayable` excludes it, and the client is not
  loosened.
- **Body over the limit:** the step is `body_too_large`, with no body and not
  replayable, and the run is not `succeeded`. A truncated body is never treated as
  complete.
- **Pagination:** a collection is `complete` only when its last page said
  `hasMore: false`. Stopping at the page cap is recorded as `truncated`: a sample, not
  the full collection.
- **Run directory:** an existing one is never reused or overwritten.

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
