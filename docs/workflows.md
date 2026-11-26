# Workflows

All three workflows use only the built-in `GITHUB_TOKEN`, with the smallest permissions that work.

## `data.yml`

* Triggers: `cron: "17 * * * *"` (minute offset from :00, where the scheduler is congested) and `workflow_dispatch`.
* Permissions: `contents: write`.
* Concurrency: group `data-<ref>`, `cancel-in-progress: false` (a run in progress is never killed mid-push).
* Steps: `python scripts/build_latest.py --msg-file ...` then `scripts/commit_push.sh` on `data/`.
  * The commit message lists the series that moved, e.g. `data: DGS10 4.92->4.91 (2026-11-24); VIXCLS 35.89->26.62 (2026-11-23)`.
  * Nothing is committed when nothing changed. Expect a few commits per day (new FRED prints, calendar roll, daily heartbeat).
* Failure modes: a FRED outage keeps the history and the previous values (warning in `quality.json`); a range/spike/schema
  error keeps the previous `latest.json` and writes `quality.json` (alert). In public repositories GitHub disables scheduled
  workflows after 60 days without repository activity; the data commits normally keep the repo active.

## `reports.yml`

* Triggers: `repository_dispatch` with `types: [ea_report]` (sent by the EA) and `workflow_dispatch` with a `payload`
  input (manual test).
* Permissions: `contents: write`.
* **No concurrency group on purpose**: GitHub keeps only one pending run per group and cancels older pending runs, which
  would drop EA reports. Each report writes its own file, and `commit_push.sh` handles push races (`pull --rebase`; on a
  conflict in a generated summary it resets to `origin` and re-runs the store + summary command).
* The payload is untrusted input: it is read only from `$GITHUB_EVENT_PATH` by Python and never interpolated with `${{ }}`
  into a shell command. Rejected payloads fail the run (which raises a `wf_fail` alert) without echoing their content.

Manual test (Actions tab > reports > Run workflow), payload example:

```json
{"v":1,"kind":"ea_report","acct":"0123456789abcdef","rid":"0123456789abcdef-1795600000","part":1,"parts":1,"ts":1795600000,
 "data":{"ea":"Zumi","reason":"manual","eq":{"day_pl_pct":-0.4},"trades":[],"log":[{"t":1795600000,"m":"manual test"}]}}
```

Or from a shell with the `gh` CLI (logged in with an account or token that can write to the repo), where
`dispatch.json` is `{"event_type": "ea_report", "client_payload": { ...the object above... }}`:

```bash
gh api repos/<owner>/<repo>/dispatches --input dispatch.json
```

## `alerts.yml`

* Triggers: `workflow_run` of `data` and `reports` (`completed`, any conclusion except `cancelled`) and `workflow_dispatch`
  (`dry_run` input prints instead of commenting).
* Permissions: `contents: write` (only to create branch `alerts` and its marker commit when missing),
  `pull-requests: write` (open the PR), `issues: write` (labels and comments).
* Concurrency: group `alerts`; at most one pending run, which is fine because every run re-evaluates the whole repo state.
* Rules (thresholds in `config/settings.json` > `alerts` / `rules`):

| key prefix | condition |
|---|---|
| `vix_jump`, `vix_level` | VIX daily change >= 15 % or >= 3 points; VIX crosses 30 |
| `real_yield`, `usd` | DFII10 moves >= 12 bp; broad USD moves >= 1 % |
| `gold:stooq` / `gold:<acct>` | gold daily move >= 1.5 x ATR14 (stooq D-1, or the EA's live market block) |
| `dayloss` | EA day P/L or equity vs day start <= -3 % |
| `streak` | >= 4 losing positions in a row (last loss within 48 h) |
| `rule` | rule-engine breaks in the last 48 h: no SL, risk above cap, entry in a news block, daily/weekly loss cap |
| `eaflag` | flags raised by the EA with `ZGB_Flag()` |
| `event` | NFP / CPI / FOMC within the next 24 h |
| `quality`, `stale`, `stale_series`, `no_latest` | data-quality failures, `latest.json` older than 48 h |
| `wf_fail` | the upstream `data` / `reports` run failed |

* Messages are bilingual (Vietnamese first, English second) and grouped into one comment per run. Each alert key is
  embedded as `<!-- zumi-alert-key: ... -->`; keys already present in the PR are not posted again.

### One-time setup of the alerts PR

`GITHUB_TOKEN` can only open pull requests when **Settings > Actions > General > Workflow permissions > "Allow GitHub
Actions to create and approve pull requests"** is enabled. Either enable it (the first alert creates branch `alerts`,
a marker commit `alerts/README.md`, the PR "Zumi alerts (do not merge)" and the label `zumi-alerts`), or create it by hand:

```bash
git switch -c alerts && mkdir -p alerts && echo "# Zumi alerts channel" > alerts/README.md
git add alerts && git commit -m "alerts: marker" && git push -u origin alerts
gh pr create --head alerts --base main --title "Zumi alerts (do not merge)" --body "Alert channel" --label zumi-alerts
```

Never merge this PR. If it gets merged or closed, the next alert opens a new one (a fresh marker commit makes the branch
differ from `main` again). Claude then needs to subscribe to the new PR number.

## Running locally

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v          # offline tests (fixtures, fake HTTP)
python scripts/build_latest.py                   # live FRED fetch, writes data/
python scripts/build_latest.py --offline         # rebuild latest.json from data/history only
python scripts/store_report.py --payload-file tests/fixtures/payload_basic.json --dates-out /tmp/d.txt
python scripts/daily_summary.py --dates-file /tmp/d.txt
python scripts/alerts.py --dry-run               # print the alerts that would be posted
```
