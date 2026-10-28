# R1 offline mapping workflow (`data/r1_mapping`)

Offline import, candidate discovery, validation and reporting for roadmap track R1
(legacy S1-T6). Standard library only. **No command makes a network request**, calls SIG,
or reads `.env` or keys. File formats and reviewer instructions are in
[`markets/README.md`](../../markets/README.md).

Run every command from the repository root.

```bash
python -m data.r1_mapping inventory
```
Audits the SIG inventory declared in `markets/r1_sources.json` (committed recorded fixture
pages): hashes, cursor chain, page lengths, reported total, duplicate IDs and the single
`YES` exchange per market. Exit 1 on any problem.

```bash
python -m data.r1_mapping candidates --venue-snapshot data/venue_markets/output/venue_markets_2026-10-11.json
```
Regenerates `markets/r1_candidates.json` from a frozen S1-T5 output. The file's SHA-256
must equal a snapshot declared in `markets/r1_sources.json`; any other file is refused.
Suggestions are never written into decisions.

```bash
python -m data.r1_mapping validate
```
Validates `r1_sources.json`, `r1_mapping.json` and `sig_resolution_evidence.json` against
the audited inventory. Prints one `ERROR [<file:record> sig=<id> venue=<venue>] <code>:
<message>` line per structural failure and exits 1 if there is any. Unresolved decisions
are counted, not errors. When a decision is `verified_match`, the declared venue snapshot
must be available (at its declared path, or passed with `--venue-snapshot PATH`) so the
match can be re-checked; otherwise validation fails closed.

```bash
python -m data.r1_mapping report
python -m data.r1_mapping report --check
```
Regenerates `markets/R1_REPORT.md` from committed files only; `--check` exits 1 if the
committed report is stale.

```bash
python -m data.r1_mapping init --snapshot-id venue-20261011T183701Z --date 2026-10-28
```
Writes unresolved skeletons for every inventory contract. It refuses to run if
`r1_mapping.json` or `sig_resolution_evidence.json` already exists, so reviewed files are
never overwritten.

Tests (synthetic fixtures plus the committed recorded SIG pages; no network):

```bash
python -m pytest data/r1_mapping/tests -v
```
