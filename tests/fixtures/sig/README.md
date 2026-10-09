# SIG fixtures

Two kinds of fixture live here. Never mix them up, and never relabel one as the other.

## Synthetic fixtures (spec-derived): the `*.json` files in this folder

Every file directly in this folder is **synthetic**. None is a recorded SIG response, and
none was produced by calling SIG or by reading a competition data dump. Values come from
the examples in `docs/api/openapi.json` where the spec gives one. Where it does not, the
value is an obvious placeholder chosen to satisfy the documented type and nullability (for
example `"cursor-page-2"` or `"Placeholder market B"`).

Each file is `{"provenance": ..., "openapi": [JSON pointers], "body": <response JSON>}`.
Tests load `body` and serve it through `httpx.MockTransport`.

They stay alongside the recorded fixtures below. For `GET /orders`, error envelopes and
later markets pages they are still the only evidence, so there the client is checked
against the spec's shapes, not against SIG's actual behaviour.

## Fixtures derived from recorded responses: `recorded/<run id>/`

Each directory comes from one genuine, read-only capture made with
`engine/sig_client/record_fixtures.py` (see `engine/sig_client/RECORDER.md`). The capture
itself stays local and uncommitted; only reviewed derived fixtures are committed here.

```
recorded/<run id>/
  .gitattributes                "* -text": bytes are hashed, so no line-ending conversion
  source-provenance.json        sanitized provenance derived from the capture manifest
  fixtures.json                 the effective fixture manifest (what the tests verify)
  bodies/NN-<step>-a<attempt>.json
```

- **`source-provenance.json`** is **sanitized** provenance derived from the capture
  manifest. It is **not** the byte-exact capture manifest: its `format` is
  `bellwether.sig-source-provenance/1`, and its `label` and `sanitization` fields say so. It keeps the capture identity, code and spec provenance,
  the recorded requests, step outcomes, pagination and coverage. Every other field holds
  the capture manifest's value, with these exceptions:
  - The original SHA-256 and size of each **redacted** body are omitted. They would let
    anyone verify guesses of the redacted values.
  - The capture manifest's own SHA-256 is not recorded anywhere, because it could verify a
    reconstruction.
  - Original hashes are kept only for bodies committed unmodified.

  Never use this file to validate a fixture body.
- **`fixtures.json`** lists every committed body with its own SHA-256 and size, its label,
  the recorded request and response status, the source body it came from and its
  redactions. It records the original hash only for unmodified bodies. It also holds the
  SHA-256 of `source-provenance.json`. The replay tests check every body and the
  provenance file against it, and check that every 64-hex string in the folder is
  accounted for.
- **Labels.** `recorded response, unmodified` means byte-identical to what SIG sent (after
  HTTP content decoding); the fixture hash equals the source hash.
  `derived from recorded response, redacted` means only the listed JSON paths were
  replaced; every other byte is as SIG sent it. A redacted body is not an original and
  must never be described as byte-exact.
- **Redactions** name each JSON path and its replacement value, never the original value.

Tests: `tests/sig_client/test_sig_recorded_replay.py` serves these bodies through
`SigRestClient` with `httpx.MockTransport`, only for the exact recorded path and query.

### `recorded/20261009T122244Z-fa008a/`

A read-only capture of 2026-10-09 (code `a42cfb4`, clean): tournament `midterm-elections`,
exchange `947`, market `258`, depth 5, markets page limit 10 with one page, and
`--skip-orders`.

| Fixture | Endpoint | Label | Redacted paths |
| --- | --- | --- | --- |
| `01-tournament-a1.json` | `GET /tournaments/{slug}` | derived, redacted | `$.myBalance` → `12345.67` and `$.isPendingEnrolment` → `true` (fixed test values; they say nothing about the account) |
| `02-tournament-markets-p1-a1.json` | `GET /tournaments/{slug}/markets` | derived, redacted | `$.data[0..9].creator.id` → `11111111-1111-4111-8111-111111111111` and `$.data[0..9].creator.username` → `redacted-creator-1` (one consistent placeholder for the single creator); in `$.data[0..9].thumbnailUrl`, the shared first UUID segment → `22222222-2222-4222-8222-222222222222` (scheme, host, path shape and per-image file name unchanged) |
| `03-exchange-prices-a1.json` | `GET /exchanges/prices` | unmodified | none |
| `04-exchange-orderbook-947-a1.json` | `GET /exchanges/{id}/orderbook` | unmodified | none |
| `05-market-orderbook-258-a1.json` | `GET /markets/{id}/orderbook` | unmodified | none |

Prices, quantities, timestamps, nulls, key order, pagination and unknown fields are
unchanged. `fixtures.json` holds the derived hashes, plus original hashes for the three
unmodified bodies only.

**Observed extra field.** Every market in `02` carries `thumbnailUrl`, which
`docs/api/openapi.json` does not list for `TournamentMarket`. The field is kept, with
its shared UUID segment redacted as above. The wire model ignores it (`extra="ignore"`).

**Observed formats.** Book timestamps (`asOf.at`) carry seven fractional digits and a
`+00:00` offset (for example `2026-10-09T12:22:56.4607814+00:00`). Other timestamps use
milliseconds and `Z`. The client keeps both as strings.

**Exchange 947 belongs to market 258.** `03` and `04` both report `marketId: "258"` for
exchange `947`, and `05` (`/markets/258/orderbook`) lists `947` as its only exchange.
Market 258 is not on markets page 1.

**Tournament scope.** `01` and the single `tournament` context in `05` name the
configured slug and UUID. `03` and `04` do not repeat the tournament id; their scope rests
on the exact `tournamentId` sent, which `fixtures.json` records.

**Spec hash.** The capture's spec SHA-256 is of the checked-out `docs/api/openapi.json`
(CRLF on that Windows checkout). The committed blob is LF-only with identical content, so
an LF checkout hashes differently.

### Coverage

- **Response envelopes:** five endpoints recorded: tournament, markets page, bulk prices,
  exchange orderbook and market orderbook. **`GET /orders` is not recorded.**
- **Pagination:** markets has the first page only (`hasMore: true`, cursor present, total
  237); the cursor was never followed and no last page was seen, so full pagination is
  **not** covered. Orders pagination is not covered.
- **Item variants not seen** (coverage gaps, not extra acceptance requirements): settled
  or closed markets; a null `latestPrice`, `creator` or `username`; markets with more than
  one exchange; null `bestBid`, `bestAsk` or `spread`; a non-empty `missingIds`; an empty
  book side; any order item.
