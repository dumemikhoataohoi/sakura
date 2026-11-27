# AGENTS.md: entry point for coding agents (and Claude's daily analysis)

Repository **sakura** is the GitHub data bot of the Zumi XAU EA (MT5, XAUUSDm on Exness). It prepares macro/calendar
context for the EA, stores the EA's trade/analysis reports, renders journal analytics and raises alerts on a long-lived PR.

Read first: `docs/agents.md` (multi-agent roles), `docs/architecture.md` (data flow), `docs/data-dictionary.md` (every file and field),
`docs/workflows.md` (triggers, permissions, alerts PR), `docs/mql5-bridge.md` (EA side).

## Encrypted files (vault)

The repo is public, but files selected by `config/settings.json` > `vault` are stored encrypted as `<name>.zv`
(ZV2: HMAC-SHA256 stream cipher + 32-byte tag, `scripts/vault.py`). Summary, since the docs themselves are encrypted:

1. Phase 1: `reports/**`, `docs/**`, `control/CHANGELOG.md`, `data/calendar/**`, `data/quality.json` (never this file).
2. Phase 2, only once every live EA runs bridge 2.8 with its key file: `control/control.json`, `data/latest.json`.
3. `ZUMI_KEY` (64 hex, set by the user) must be in the environment; without it every reader stops with an error, and
   a plaintext copy of an encrypted file is refused. A `.zv` is never written back as plaintext (only `vault.py dec`).
4. Read any file with `python scripts/vault.py cat <file>` (e.g. `docs/workflows.md`); alerts PR comments carry only
   `id + severity + ZV2 blob`: `python scripts/vault.py alerts < comment.txt`. Check with `python scripts/vault.py doctor`.
5. Never print, log or commit the key (doctor shows only its fingerprint); while the vault is on, use generic commit
   messages (`reports: update`, `coord: update`, `control: update`): they stay public, as do file names and history.

Scripts read and write through `vault.read_text` / `vault.write_text`, so the routine below works unchanged once
`ZUMI_KEY` is set; to read a page yourself, use `vault.py cat` instead of opening the file. Edit an encrypted file by
hand (e.g. the calendar) with `vault.py dec --yes-plaintext <file>`, edit, then `vault.py enc <file>` before any commit.

## Daily analysis routine (Claude)

0. Read `docs/user-requirements.md` and, for any EA research/upgrade, follow `docs/research-protocol.md` step by step.
   Read `docs/user-requirements.md` (binding user requirements + checklist) and check every item; report any item
   that is not ✅. Then analyse the market first (multi-timeframe trend, reversal patterns/signals, S/R, news) from
   the EA's `mkt` / `mkt.ctx` report block before any control decision.
1. `data/quality.json`: is `ok` true? Any `stale` warnings?
2. `data/latest.json`: VIX level/change, real yield (`DFII10`), USD (`DTWEXBGS`), `flags`, `events.next` (news in the
   next sessions). Values are as of D-1 or older; check `age`.
3. `reports/daily/<yesterday UTC>.md` (and today's if it exists): aggregate metrics, rule-break counts, losing streak,
   drawdown, EA flags. Reports are aggregated (no individual trades); methods appear as codes `M###` whose names only the
   user knows.
4. `reports/weekly/<this ISO week>.md` and `reports/monthly/<YYYY-MM>.md`: method-code / phase breakdown, weekday x
   4-hour grid, month-to-date drawdown.
5. The open PR labelled `zumi-alerts`: alerts posted since the last review.
6. Coordination (control channel, `docs/control.md`):
   1. `python scripts/coordinator_report.py` -> read `reports/coord/<today>.md` (rule hits + proposed control.json).
      v2 rules: R01 streak, R02 Wilson health switch per `M### x phase` (90-day history of the stored daily blocks),
      R08 restore (cell or blocked-signal lower bound >= 50 %), optional R02 `reduce` risk allocation (`alloc_enable`),
      R05 drawdown ladder (3 % / 5 % -> x0.75 / x0.5 and open-risk cap 9 / 6 %), R09 side `auto` (needs `by_side`),
      R03 news. Replayed walk-forward with `scripts/coord_replay.py` (research streams stay outside the repo): on the
      EA streams the per-cell rules almost never have enough trades, so expect a win% lift of ~0 points; treat the
      coordinator as a risk tool (news, streak, drawdown ladder), not as a win-rate lever.
   2. Check `python scripts/control.py effective` (what the EA applies now) and `control/CHANGELOG.md`.
   3. Decide. Apply with `python scripts/coordinator_report.py --apply`, or edit `control/control.json` (bump `seq`,
      codes `M###` only, reason codes from `control/reason_codes.md`, no prices in `notes_vi`) and run
      `python scripts/control.py normalize`. Commit `control/` and `reports/coord/` (vault on: generic message
      `coord: update`, no codes or values in it), pull --rebase, push.
   4. Refresh the file at least every 48 h while directives should stay active (expiry), never later than 72 h
      (staleness = NEUTRAL). Boosts only take effect up to the user's local `InpGhMaxRiskMult`.
7. Write findings for the user. Macro values are context and risk filters only: the macro1 research found no
   out-of-sample edge from them for 1R gold trades. Do not present them as signals.

## Rules for changes

* Python 3, standard library + `requests` only. Keep `python -m unittest discover -s tests` green and offline.
* Keep outputs deterministic and pretty-printed with sorted keys (small diffs, commit only on change).
* Keep the `latest.json` schema backward compatible; bump `v` for breaking changes and update the MQL5 parser.
* Vault: every repo file is read with `vault.read_text` / `vault.exists` / `vault.glob` and written with
  `vault.write_text` (it encrypts when the config selects the path). A `vault.VaultError` must stop the reader, never
  be treated as a missing or empty file. Keep `scripts/vault.py`, the MQL5 `ZGB_Vault*` code, its KAT block and
  `tests/test_vault*.py` / `tests/test_mql5_static.py` in sync. Tests use dummy keys only.
* Privacy: never write an account number, email, money amount, token or webhook URL to the repo, a log or a commit
  message. Accounts are hashes; money is % of balance. Use `mask_secrets()` for any free text.
* The repository is PUBLIC: only aggregate (v2) EA reports are accepted (`reports.public_mode: true`). Do not add
  per-trade fields, times, prices, tickets or method names to anything published; extend the whitelist in
  `scripts/agg.py` (and its tests) deliberately.
* Workflows use only `GITHUB_TOKEN` with minimal `permissions`. Never interpolate `client_payload` (or any event text)
  with `${{ }}` into `run:`; read the event file from Python.
* The calendar (`data/calendar/events_us.csv`) is maintained by hand; see the data dictionary for the procedure.
* Control channel: never weaken the EA-side hard limits (cap `InpGhMaxRiskMult`, max risk per trade, loss stops, crisis
  layer, NEUTRAL fallback, no remote trading, no SL/TP changes). Keep `scripts/control.py`, the MQL5 validator and
  `tests/test_control.py` / `tests/test_mql5_static.py` in sync.
* The MQL5 module cannot be compiled here; after editing it, run `tests/test_mql5_static.py` and ask the user to
  compile in MetaEditor.
