"""A fake key never appears in logs, reprs, exceptions or the database."""

import asyncio
import logging

import httpx

from bot.sig_writer import CancelIncomplete, WriteNoResponse
from bot.tests.kit import KEY, error, make_intent, make_rig, ok_body, reply, sig_config


def test_key_never_leaks(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    echo = ok_body(terminalReasonCode=f"Bearer {KEY}")  # a body that echoes the key
    rig = make_rig(
        tmp_path,
        [
            reply(200, echo),
            error(400, "VALIDATION_ERROR"),
            httpx.ReadTimeout(f"timed out with Bearer {KEY}"),
            httpx.ReadTimeout(KEY),
            httpx.ReadTimeout(KEY),
            httpx.ReadTimeout(KEY),
        ],
    )
    asyncio.run(rig.path.place(make_intent()))
    asyncio.run(rig.path.place(make_intent(sig_exchange_id="37")))
    asyncio.run(rig.path.place(make_intent(sig_exchange_id="38")))

    # The key went out only in the Authorization header.
    for seen in rig.sig.seen:
        assert seen.headers["authorization"] == f"Bearer {KEY}"
        assert KEY.encode() not in seen.content

    dump = "\n".join(rig.conn.iterdump())
    assert "terminalReasonCode" in dump  # the body was stored, redacted
    assert KEY not in dump
    assert KEY not in caplog.text
    for obj in (rig.writer, rig.path, sig_config(), sig_config().api_key):
        assert KEY not in repr(obj) and KEY not in str(obj)

    exceptions = [
        WriteNoResponse(method="POST", path="/orders", kind="ReadTimeout"),
        CancelIncomplete(path="/orders/cancel-all", last="HTTP 503"),
    ]
    for exc in exceptions:
        assert KEY not in str(exc) and KEY not in repr(exc)


def test_no_response_exception_keeps_only_the_class_name(tmp_path):
    rig = make_rig(tmp_path, [httpx.ConnectError(f"refused {KEY}")])
    try:
        asyncio.run(rig.writer.post_order(b"{}"))
    except WriteNoResponse as exc:
        assert exc.kind == "ConnectError"
        assert KEY not in str(exc)
        assert exc.__cause__ is None and exc.__suppress_context__
    else:
        raise AssertionError("expected WriteNoResponse")
