"""Audit the SIG tournament-market inventory that R1 classifies.

The inventory comes from committed, reviewed recorded fixtures (``tests/fixtures/sig/
recorded/<run>/``), never from the live API. The audit proves only what the pages show: a
complete cursor walk at capture time. It does not show that the capture still reflects
today's inventory, and the pages carry no race ID, stage ID or Info-tab text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from data.r1_mapping.common import REPO_ROOT, Finding, load_json, load_wire_json, sha256_file

MARKETS_ROUTE = "GET /tournaments/{slug}/markets"


@dataclass
class InventoryAudit:
    source_id: str
    pages: list[dict[str, Any]] = field(default_factory=list)
    contracts: list[dict[str, Any]] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    complete: bool = False
    reported_total: int | None = None

    @property
    def by_market_id(self) -> dict[str, dict[str, Any]]:
        return {c["market_id"]: c for c in self.contracts}


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def audit_inventory(source: dict[str, Any], root: Path = REPO_ROOT) -> InventoryAudit:
    """Check the declared SIG inventory source and extract its contracts.

    ``source`` is the ``sig_inventory`` block of ``markets/r1_sources.json``. Every
    integrity or pagination problem becomes a finding; ``complete`` is true only when the
    cursor chain is whole and every count agrees.
    """
    source_id = str(source.get("id") or "sig_inventory")
    audit = InventoryAudit(source_id=source_id)
    where = f"sources:sig_inventory:{source_id}"

    def fail(code: str, message: str, at: str = where) -> None:
        audit.findings.append(Finding(code, at, message))

    run_dir = root / str(source.get("run_dir") or "")
    manifest_path = run_dir / "fixtures.json"
    if not source.get("run_dir") or not manifest_path.is_file():
        fail("inventory_source_missing", f"no fixture manifest at {manifest_path}")
        return audit

    declared_manifest_hash = source.get("fixture_manifest_sha256")
    if sha256_file(manifest_path) != declared_manifest_hash:
        fail("inventory_manifest_hash_mismatch", "fixtures.json hash differs from the declared one")
    manifest = load_json(manifest_path)
    listed = {f.get("file"): f for f in manifest.get("fixtures", [])}

    declared_pages = source.get("pages") or []
    if not declared_pages:
        fail("inventory_pages_missing", "no markets pages declared")
        return audit

    slug = source.get("tournament_slug")
    seen_ids: dict[str, str] = {}
    seen_exchanges: dict[str, str] = {}
    chain_ok = True
    totals: set[int | None] = set()
    item_count = 0

    for index, page in enumerate(declared_pages):
        page_where = f"{where}:page[{index}]"
        page_file = run_dir / str(page.get("file") or "")
        entry = listed.get(page.get("file"))
        if not page_file.is_file() or entry is None:
            fail("inventory_page_missing", f"{page.get('file')} not committed or not listed",
                 page_where)
            chain_ok = False
            continue
        actual_hash = sha256_file(page_file)
        if actual_hash != page.get("sha256") or actual_hash != entry.get("sha256"):
            fail("inventory_page_hash_mismatch",
                 f"{page.get('file')} hash differs from sources or fixtures.json", page_where)
        request = entry.get("request") or {}
        if request.get("route") != MARKETS_ROUTE:
            fail("inventory_page_wrong_route", f"route {request.get('route')!r}", page_where)
        if slug and request.get("path") != f"/tournaments/{slug}/markets":
            fail("inventory_page_wrong_tournament", f"path {request.get('path')!r}", page_where)
        params = request.get("params") or {}

        body = load_wire_json(page_file)
        data = body.get("data")
        pagination = body.get("pagination") or {}
        if not isinstance(data, list):
            fail("inventory_page_malformed", "body has no data list", page_where)
            chain_ok = False
            continue
        has_more = pagination.get("hasMore")
        next_cursor = pagination.get("nextCursor")
        total = _int_or_none(pagination.get("total"))
        limit = _int_or_none(params.get("limit"))
        totals.add(total)
        item_count += len(data)
        audit.pages.append({
            "file": page.get("file"),
            "sha256": actual_hash,
            "items": len(data),
            "request_cursor": params.get("cursor"),
            "has_more": has_more,
            "next_cursor": next_cursor,
            "total": total,
            "limit": limit,
        })
        if limit is not None and len(data) > limit:
            fail("inventory_page_over_limit", f"{len(data)} items > limit {limit}", page_where)

        if index == 0 and params.get("cursor"):
            fail("inventory_chain_not_from_start", "first page was requested with a cursor",
                 page_where)
            chain_ok = False
        if index > 0:
            previous = audit.pages[-2] if len(audit.pages) >= 2 else None
            if previous is None or params.get("cursor") != previous["next_cursor"]:
                fail("inventory_cursor_chain_broken",
                     "request cursor is not the previous page's nextCursor", page_where)
                chain_ok = False
        last = index == len(declared_pages) - 1
        if last and (has_more is not False or next_cursor is not None):
            fail("inventory_truncated",
                 "last declared page still reports hasMore/nextCursor; the walk stopped early",
                 page_where)
            chain_ok = False
        if not last and has_more is not True:
            fail("inventory_cursor_chain_broken", "a non-final page reports hasMore false",
                 page_where)
            chain_ok = False

        for pos, market in enumerate(data):
            _extract_contract(market, f"{page_where}:data[{pos}]", audit, seen_ids,
                              seen_exchanges)

    if len(totals) != 1 or None in totals:
        fail("inventory_total_inconsistent", f"pages report totals {sorted(map(str, totals))}")
        chain_ok = False
    else:
        audit.reported_total = totals.pop()
        if item_count != audit.reported_total or len(seen_ids) != audit.reported_total:
            fail("inventory_count_mismatch",
                 f"{item_count} items, {len(seen_ids)} unique ids, total {audit.reported_total}")
            chain_ok = False

    audit.complete = chain_ok and not any(
        f.code.startswith(("inventory_page_hash", "inventory_duplicate")) for f in audit.findings
    )
    if source.get("declared_complete") is True and not audit.complete:
        fail("inventory_incomplete_presented_as_complete",
             "sources declare this inventory complete, but the audit shows it is not")
    audit.contracts.sort(key=lambda c: (_int_or_none(c["market_id"]) or 0, c["market_id"]))
    return audit


def _extract_contract(
    market: Any,
    where: str,
    audit: InventoryAudit,
    seen_ids: dict[str, str],
    seen_exchanges: dict[str, str],
) -> None:
    def fail(code: str, message: str) -> None:
        audit.findings.append(Finding(code, where, message))

    if not isinstance(market, dict) or not isinstance(market.get("id"), str) or not market["id"]:
        fail("inventory_market_malformed", "market without a string id")
        return
    market_id = market["id"]
    if market_id in seen_ids:
        fail("inventory_duplicate_market_id", f"market {market_id} also at {seen_ids[market_id]}")
        return
    seen_ids[market_id] = where
    if market.get("isComposite") is not False or market.get("isMultiOutcome") is not False:
        fail("inventory_unsupported_market_shape",
             f"market {market_id} is composite or multi-outcome; R1 models single YES contracts")
        return
    exchanges = market.get("exchanges")
    if not isinstance(exchanges, list) or len(exchanges) != 1:
        fail("inventory_unsupported_market_shape",
             f"market {market_id} has {len(exchanges or [])} exchanges; R1 expects exactly one")
        return
    exchange = exchanges[0]
    exchange_id = exchange.get("id") if isinstance(exchange, dict) else None
    option = exchange.get("option") if isinstance(exchange, dict) else None
    if not isinstance(exchange_id, str) or not exchange_id:
        fail("inventory_market_malformed", f"market {market_id} exchange has no id")
        return
    if option != "YES":
        fail("invalid_yes_no_association",
             f"market {market_id} exchange {exchange_id} option {option!r}, expected 'YES'")
        return
    if exchange_id in seen_exchanges:
        fail("inventory_duplicate_exchange_id",
             f"exchange {exchange_id} also on {seen_exchanges[exchange_id]}")
        return
    seen_exchanges[exchange_id] = market_id
    audit.contracts.append({
        "market_id": market_id,
        "exchange_id": exchange_id,
        "option": option,
        "title": market.get("title"),
        "status": market.get("status"),
        "categories": list(market.get("categories") or []),
        "settlement_date": market.get("settlementDate"),
        "created_at": market.get("createdAt"),
    })
