# Workflows

All three workflows use only the built-in `GITHUB_TOKEN`, with the smallest permissions that work.

## `data.yml`

* Triggers: `cron: "17 * * * *"` (minute offset from :00, where the scheduler is congested) and `workflow_dispatch`.
* Permissions: `contents: write`.
* Concurrency: group `data-<ref>`, `cancel-in-progress: false` (a run in progress is never killed mid-push).
* Steps: `python scripts/build_latest.py --msg-file ...` then `scripts/commit_push.sh` on `data/`.
  * The commit message lists the series that moved, e.g. `data: DGS10 4.92->4.91 (2026-11-24); VIXCLS 35.89->26.62 (2026-11-23)`.
  * Nothing is committed when nothing changed. Expect a few commits per day (new FRED prints, calendar roll, daily heartbeat).
* Then `python scripts/agents_status.py --msg-file ...` and `scripts/commit_push.sh` on `reports/agents/` (a second commit):
  GitHub agent self-monitoring, one health row per agent / sub-agent of `docs/agents.md` plus the daily KPIs. It reads repo
  files only (no event text, no extra permission) and rewrites the day's file only when a status, a reason or a KPI changed,
  so it commits on change (and once per UTC day), not every hour.
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
{"v":2,"kind":"ea_agg","acct":"0123456789abcdef","ts":1795600800,
 "data":{"ea":"Zumi","reason":"manual","day":"2026-11-25","eq":{"day_pl_pct":-0.4},
         "periods":{"day":{"n":2,"wins":1,"losses":1,"be":0,"net_r":0.5,"net_pct":-0.4,"max_ls":1,"cur_ls":1,"lead_ls":0,
                           "dd_pct":0.9,"rule":{"no_sl":0},"by_method":{"M001":{"n":2,"wins":1,"net_r":0.5}}}}}}
```

Per-trade (v1, `kind: "ea_report"`) payloads are rejected while `config/settings.json` has `reports.public_mode: true`
(the run fails with a short message and the payload is not echoed). Unknown or per-trade keys inside a v2 payload are
stripped; the log lists their names only.

Or from a shell with the `gh` CLI (logged in with an account or token that can write to the repo), where
`dispatch.json` is `{"event_type": "ea_report", "client_payload": { ...the object above... }}`:

```bash
gh api repos/<owner>/<repo>/dispatches --input dispatch.json
```

## `control.yml`

* Trigger: `push` to `main` touching `control/**`, and `workflow_dispatch`.
* Runs `python scripts/control.py check --prev-ref <before>`: schema, bounds, expiry, reason codes, public text,
  normalized form, and `seq` greater than in the previous commit. On failure the run fails and posts a
  `control_invalid` alert on the alerts PR (the EA treats an invalid file as NEUTRAL anyway).
* Permissions: same as `alerts.yml` (the `contents: write` is only used if the alerts PR must be created).

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
| `gold:<acct>` / `gold:stooq` | gold daily move >= 1.5 x ATR14 (the EA's `mkt.move_atr`; stooq block when enabled) |
| `dayloss` | newest aggregate report (< 6 h old): EA day P/L, equity vs day start or the day's closed P/L <= -3 % |
| `streak` | current losing streak (day / week / month-to-date block) >= 4; a new comment when it grows |
| `dd` | drawdown of the rolling week or month to date >= `alerts.dd_alert_pct` (5 %) of balance |
| `rule` | rule-break counts of today's block (no SL, risk above cap, entry in a news block, daily loss cap); a new comment when a count grows. In private detail mode the per-position rule engine is used instead |
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

## Vault: encrypted files in the public repository

The repository stays public; its data and documents are stored encrypted. `scripts/vault.py` (standard library only)
implements **ZV2**, a PRF stream cipher with encrypt-then-MAC built on HMAC-SHA256 only:

| item | definition |
|---|---|
| master key `K` | 32 bytes, 64 hex characters: env `ZUMI_KEY` (Actions: repo secret `ZUMI_KEY`; Claude: env var set by the user); EA: first line of the file `MQL5\Files\zumi_vault.key` |
| sub-keys | `Ke = HMAC(K, "zumi-enc-v1")`, `Km = HMAC(K, "zumi-mac-v1")` |
| nonce | `N = HMAC(Km, "nonce" \|\| u16be(len(path)) \|\| path \|\| plaintext)[:16]`: deterministic (SIV-style), so an unchanged file gives the same bytes (quiet diffs, commit only on change) |
| keystream | block `i` = `HMAC(Ke, N \|\| uint32be(i))`, 32 bytes; `ct = pt XOR keystream` |
| tag | `HMAC(Km, "tag" \|\| N \|\| u16be(len(path)) \|\| path \|\| ct)[:32]`, compared in constant time before decrypting; then `N` is re-derived from the plaintext and compared too |
| file | one text line `ZV2:` + base64url(`N \|\| tag \|\| ct`) without padding, stored as `<original name>.zv` (`control/CHANGELOG.md.zv`) |

`path` is the repo-relative path of the plaintext file. The length prefix makes `(path, data)` unambiguous, so two
paths can never share a nonce or keystream. (ZV1 had no length prefix and is dropped; no real data was ever encrypted
with it.) Fail closed: a missing or malformed key, a wrong key, a moved file or one changed byte raises `VaultError`,
and no reader treats that as "missing" or "empty". Deterministic encryption reveals only when two versions of the same
file are identical.

**Config** `config/settings.json` > `vault`:
* `enabled`: the switch (shipped `false`).
* `key_fp`: the 8-hex key fingerprint, written by `migrate`.
* `phase`, `ea_bridge_min`.
* `paths`: the phase 1 globs.
* `phase2_paths`, `plain_exceptions`, `generic_commit_messages`.

The vault is **active** when `enabled` is true *or* `key_fp` is set: `key_fp` is a latch, so flipping `enabled` alone
never downgrades the repository. Phase-2 paths are encrypted only when `phase >= 2` **and** `ea_bridge_min >= "2.8"`.
`AGENTS.md`, `config/`, `scripts/`, `tests/`, `mql5/` and `.github/` are never encrypted.

| phase | files | readers |
|---|---|---|
| 1 | `reports/**` (raw reports, daily / weekly / monthly pages, `coord/`, `agents/`, journal), `docs/**` except `docs/dashboard/index.html`, `control/CHANGELOG.md`, `data/calendar/**`, `data/quality.json` | Actions and Claude (with `ZUMI_KEY`) |
| 2 | `control/control.json`, `data/latest.json` | also the EA: needs bridge 2.8 and the key file on every live terminal. Bridge 2.3 reads both in plaintext, so phase 2 stays off until then |

**Safety rules.**
* **A `.zv` stays encrypted.** A path that has a `.zv` is always written encrypted, whatever `enabled` says. Without
  the key the writer fails; it never writes plaintext. Only `vault.py dec` turns a `.zv` back into plaintext, and it
  asks for confirmation (non-interactive runs need `--yes-plaintext`).
* **Downgrade protection.** While the vault is active, a vault-set path is read only from its `.zv`. A plaintext copy
  alone, with no `.zv` (planted, or a hand edit), raises `VaultError`. Use `vault.py enc <file>` for your own edits.
* **Gate.** `python scripts/vault.py check` is the first step after the install in `data`, `reports`, `alerts` and
  `control`. It fails fast when:
  * the key is missing while the vault is active or `.zv` files exist;
  * the key's fingerprint differs from `vault.key_fp` (a typo or the wrong secret; only fingerprints are printed);
  * a `.zv` does not decrypt;
  * a vault-set file is left in plaintext.

  With the vault off and no `.zv` files, it passes without a key.

**Workflows.** `ZUMI_KEY: ${{ secrets.ZUMI_KEY }}` is passed as env only to the steps that read or write vault files:
* data: check, build, calendar, commit, agents;
* reports: check, store;
* alerts: check, evaluate;
* control: check, validate.

It is never echoed, and GitHub masks it in logs. There are no permission changes, and the rule against `${{ }}`
interpolation of event text is unchanged. While the vault is active, or any `.zv` exists:

* **Commit messages are generic:** `data: update`, `reports: update`, `agents: update`. Claude uses `coord: update` /
  `control: update` for its own commits. The detailed text stays in the local console.
* **Actions logs print counts only** (`vault.public_log`), because the logs of a public repository are public. This
  includes control.py's warnings and errors, which in phase 2 could quote values. Local runs print details.
* **Alert comments** on the public PR contain only three things:
  * an opaque id (`z` + 12 hex, an HMAC of the alert key, used for de-duplication);
  * the severity;
  * a ZV2 blob: `- **[HIGH]** `z1a2b3c4d5e6f` ZV2:...`.

  Decrypt a comment with `python scripts/vault.py alerts < comment.txt`. Alerts that do not fit in one comment
  (60000 characters) are posted as numbered parts ("part i/n"); each alert sits in one part with its marker, so nothing
  is cut or re-posted. Without the key, alerts.py posts one data-free `vault_nokey` notice and fails.
* **Dashboard:** `docs/dashboard/index.html` stays plaintext and `data.json` stays encrypted. When the page finds
  `data.json.zv` (a `ZV2:` file), it shows "dữ liệu đã mã hóa – xem bằng vault.py". Read the data with
  `vault.py cat docs/dashboard/data.json`.

**What stays visible** even in phase 1:
* file names and paths (dates, ISO weeks, the account hash in raw report names);
* file sizes (about 4/3 of the plaintext + 70 bytes);
* commit times and frequency (when the EA reported);
* commit messages;
* the alerts' severity and count;
* the whole **git history before the migration**, plus older alert comments. Old commits keep the plaintext; only a
  history rewrite, which this repo does not do, would remove it.

**CLI.**
* `vault.py cat|enc|dec|check [files]`.
* `vault.py doctor`: key present yes/no, key fingerprint `HMAC(K, "fp")[:4]` compared with `vault.key_fp`, the files
  each phase would encrypt, and whether every reader can decrypt.
* `vault.py migrate --phase 1|2 [--dry-run]`. It writes each `.zv`, re-reads it from disk and decrypts it before
  removing the plaintext, then records `vault.key_fp`. It refuses when:
  * `ZUMI_KEY` is missing;
  * the self-test or a round trip fails;
  * the key does not match `key_fp`;
  * `vault.enabled` is false;
  * for phase 2, the config does not set `phase: 2` and `ea_bridge_min: "2.8"`.
* `vault.py kat`: the known-answer vectors for MQL5 (dummy key).

**Rollout.**

1. Generate a key once on a trusted machine: `python -c "import secrets; print(secrets.token_hex(32))"`. Store it in
   a password manager; losing it makes every `.zv` file unreadable.
2. Put it in three places:
   * GitHub > Settings > Secrets and variables > Actions > New repository secret `ZUMI_KEY`;
   * the Claude environment variable `ZUMI_KEY`;
   * later (phase 2 only) the file `MQL5\Files\zumi_vault.key` on each terminal (first line, 64 hex). Use
     `Common\Files` with `InpGhUseCommonFiles`. It is never an EA input, so it is not saved in `.set` files.
3. Check: run `python scripts/vault.py doctor` in Claude (it shows the fingerprint and "self-test OK").
4. Phase 1, in one local step before any workflow runs:
   * set `vault.enabled: true`;
   * run `python scripts/vault.py migrate --phase 1` (it records `key_fp`);
   * run `vault.py check`;
   * commit with a generic message.

   An enabled but not yet migrated repo is refused by the readers (downgrade protection). The EA keeps reading
   plaintext `control.json` / `latest.json`.
5. Phase 2, after every live EA runs bridge 2.8 with the key file and logs the same fingerprint:
   * set `phase: 2` and `ea_bridge_min: "2.8"`;
   * run `migrate --phase 2`;
   * commit.

   The EA then latches: its file `zgb_vault_latch` makes it refuse a 404 or a plaintext copy from then on.
6. Rollback (deliberate only): set `phase: 1` and run `vault.py dec --yes-plaintext control/control.json
   data/latest.json`. Then delete `zgb_vault_latch` on each terminal. To leave the vault entirely, also set `enabled:
   false`, clear `key_fp` and `dec` every file.

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
