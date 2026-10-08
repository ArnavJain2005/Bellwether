# Synthetic SIG fixtures (spec-derived)

Every file here is **synthetic**. None is a recorded SIG response, and none was produced
by calling SIG or by reading a competition data dump. Values come from the examples in
`docs/api/openapi.json` where the spec gives one. Where it does not, the value is an
obvious placeholder chosen to satisfy the documented type and nullability (for example
`"cursor-page-2"` or `"Placeholder market B"`).

Each file is `{"provenance": ..., "openapi": [JSON pointers], "body": <response JSON>}`.
Tests load `body` and serve it through `httpx.MockTransport`.

Recorded responses replace these in S1-T4 (fixture recorder). Until then the client is
checked only against the spec's shapes, not against SIG's actual behaviour.
