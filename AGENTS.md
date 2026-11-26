# AGENTS.md: entry point for coding agents (and Claude's daily analysis)

Repository **sakura** is the GitHub data bot of the Zumi XAU EA (MT5, XAUUSDm on Exness). It prepares macro/calendar
context for the EA, stores the EA's trade/analysis reports, renders journal analytics and raises alerts on a long-lived PR.

Read first: `docs/architecture.md` (data flow), `docs/data-dictionary.md` (every file and field),
`docs/workflows.md` (triggers, permissions, alerts PR), `docs/mql5-bridge.md` (EA side).

## Daily analysis routine (Claude)

1. `data/quality.json`: is `ok` true? Any `stale` warnings?
2. `data/latest.json`: VIX level/change, real yield (`DFII10`), USD (`DTWEXBGS`), `flags`, `events.next` (news in the
   next sessions). Values are as of D-1 or older; check `age`.
3. `reports/daily/<yesterday UTC>.md` (and today's if it exists): metrics, rule breaks, losing streak, phase log, EA flags.
4. `reports/weekly/<this ISO week>.md`: method/phase breakdown, weekday x hour heatmap, drawdown episodes.
5. The open PR labelled `zumi-alerts`: alerts posted since the last review.
6. Write findings for the user. Macro values are context and risk filters only: the macro1 research found no
   out-of-sample edge from them for 1R gold trades. Do not present them as signals.

## Rules for changes

* Python 3, standard library + `requests` only. Keep `python -m unittest discover -s tests` green and offline.
* Keep outputs deterministic and pretty-printed with sorted keys (small diffs, commit only on change).
* Keep the `latest.json` schema backward compatible; bump `v` for breaking changes and update the MQL5 parser.
* Privacy: never write an account number, email, money amount, token or webhook URL to the repo, a log or a commit
  message. Accounts are hashes; money is % of balance. Use `mask_secrets()` for any free text.
* Workflows use only `GITHUB_TOKEN` with minimal `permissions`. Never interpolate `client_payload` (or any event text)
  with `${{ }}` into `run:`; read the event file from Python.
* The calendar (`data/calendar/events_us.csv`) is maintained by hand; see the data dictionary for the procedure.
* The MQL5 module cannot be compiled here; after editing it, run `tests/test_mql5_static.py` and ask the user to
  compile in MetaEditor.
