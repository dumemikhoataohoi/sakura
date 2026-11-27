# AGENTS.md: entry point for coding agents (and Claude's daily analysis)

Repository **sakura** is the GitHub data bot of the Zumi XAU EA (MT5, XAUUSDm on Exness). It prepares macro/calendar
context for the EA, stores the EA's trade/analysis reports, renders journal analytics and raises alerts on a long-lived PR.

Read first: `docs/architecture.md` (data flow), `docs/data-dictionary.md` (every file and field),
`docs/workflows.md` (triggers, permissions, alerts PR), `docs/mql5-bridge.md` (EA side).

## Daily analysis routine (Claude)

0. Read `docs/user-requirements.md` (binding user requirements + checklist) and check every item; report any item
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
   2. Check `python scripts/control.py effective` (what the EA applies now) and `control/CHANGELOG.md`.
   3. Decide. Apply with `python scripts/coordinator_report.py --apply`, or edit `control/control.json` (bump `seq`,
      codes `M###` only, reason codes from `control/reason_codes.md`, no prices in `notes_vi`) and run
      `python scripts/control.py normalize`. Commit `control/` and `reports/coord/`, pull --rebase, push.
   4. Refresh the file at least every 48 h while directives should stay active (expiry), never later than 72 h
      (staleness = NEUTRAL). Boosts only take effect up to the user's local `InpGhMaxRiskMult`.
7. Write findings for the user. Macro values are context and risk filters only: the macro1 research found no
   out-of-sample edge from them for 1R gold trades. Do not present them as signals.

## Rules for changes

* Python 3, standard library + `requests` only. Keep `python -m unittest discover -s tests` green and offline.
* Keep outputs deterministic and pretty-printed with sorted keys (small diffs, commit only on change).
* Keep the `latest.json` schema backward compatible; bump `v` for breaking changes and update the MQL5 parser.
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
