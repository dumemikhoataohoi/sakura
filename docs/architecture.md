# Architecture

```
                 FRED CSV (no key)      stooq CSV (optional)     data/calendar/events_us.csv (hand-maintained)
                        \                      |                          /
                         v                     v                         v
  data.yml (cron :17 hourly) --> scripts/build_latest.py --> quality guards --> data/latest.json
                                         |                                     data/history/*.csv
                                         |                                     data/quality.json
                                         v
                              commit only if changed (message lists what moved)
                                                                   |
   MT5 EA + mql5/ZumiGitHubBridge.mqh  <---- raw.githubusercontent / API contents (GET latest.json)
        |
        |  POST /repos/<owner>/<repo>/dispatches  {event_type: "ea_report", client_payload}
        v
  reports.yml --> scripts/store_report.py (+ agg.py whitelist validator) --> reports/YYYY/MM/DD/<acct>_<ts>.json
                 (public repo: aggregate v2 only; per-trade v1 + journal only when reports.public_mode=false)
              --> scripts/daily_summary.py --> reports/daily|weekly|monthly/*.md, docs/dashboard/data.json
                                                                   |
  alerts.yml (workflow_run after data / reports, + manual) --> scripts/alerts.py
        --> one comment on the open PR labelled "zumi-alerts" (branch "alerts")  --> Claude is notified
                                                                    \--> (optional, EA side) Telegram / Discord

  Claude (daily): reads AGENTS.md -> data/latest.json, reports/daily/<yesterday>.md, weekly page, alerts PR
        --> scripts/coordinator_report.py --> reports/coord/<day>.md (suggestion) --> Claude decides
        --> control/control.json (+ CHANGELOG) --> control.yml validates --> EA ZGB_ReadControl (hourly, clamped, NEUTRAL fallback)
```

## Components

| part | role |
|---|---|
| `scripts/fred.py`, `gold.py`, `calendar_events.py` | fetch / parse / merge sources; D-1 summaries |
| `scripts/quality.py` | schema, staleness, range and spike guards |
| `scripts/build_latest.py` | orchestrates the data run; writes only on change; writes the commit message |
| `scripts/reports_lib.py` | payload sanitising (privacy), raw storage, deal journal, position reconstruction |
| `scripts/store_report.py` | entry point of `reports.yml` |
| `scripts/journal_stats.py` | metrics, streaks, drawdown episodes, groupings, heatmap, rule engine |
| `scripts/daily_summary.py` | renders daily / weekly / monthly pages and the dashboard data |
| `scripts/alerts.py`, `gh_api.py` | alert rules, de-duplication, alerts PR management |
| `scripts/commit_push.sh` | commit-if-changed, `pull --rebase` + retry, reset-and-regenerate on conflict |
| `mql5/ZumiGitHubBridge.mqh` | EA include: read latest.json, build/queue/send reports, MAE/MFE, optional chat channels |

## Design decisions

* **Git as the database ("git scraping").** Every change is a commit, so history, diffs and rollbacks come for free and
  Claude can read everything with plain file access. Files are pretty-printed with sorted keys so diffs stay small.
* **Causality.** `latest.json` only contains observations dated before the build day (`asof: "D-1"`), and FRED's own
  publication lag makes the newest value D-1 or D-2. The research behind this project (macro1) found **no out-of-sample
  edge** from these macro series for 1R gold trades, so the feed is meant as context / risk filter (news blocks, VIX regime),
  not as a signal.
* **Fail safe.** If anything is missing or stale, the EA gets "no data" and trades normally. A quality failure never
  overwrites the last good `latest.json`.
* **Idempotency.** Reports are stored by content; deals are upserted by ticket; positions and summaries are derived.
  Re-sending a report (EA retry, queue replay) changes nothing.
* **Privacy.** The repo is public, so the EA sends aggregated statistics only (no trades, times, prices, SL/TP,
  tickets); method names become local codes; reports leave at clock-aligned slots; account hash only (salted in the
  EA); money only in %; secrets masked in every stored string and commit message; no tokens in the repository.
* **Minimal permissions.** Workflows use only `GITHUB_TOKEN`: `contents: write` for data/reports, plus
  `pull-requests: write` and `issues: write` for alerts. The EA's PAT is a fine-grained token limited to this repository.
* **PR comments as the notification bus.** Claude subscribes to the alerts PR. Hidden `<!-- zumi-alert-key: ... -->` markers
  in earlier comments de-duplicate alerts, so no alert state is committed.
