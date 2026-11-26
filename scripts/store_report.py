#!/usr/bin/env python3
"""Store one EA report (repository_dispatch 'ea_report' or workflow_dispatch input).

* validates and sanitises the payload (hash-only account id, % only, secrets masked);
* writes reports/YYYY/MM/DD/<acct>_<YYYYMMDDTHHMMSSZ>[_p<i>of<n>].json (idempotent);
* upserts the exit deals into reports/journal/<acct>/deals.jsonl keyed by deal ticket;
* writes the UTC dates it touched (one per line) to --dates-out for daily_summary.py.

Usage:
  python scripts/store_report.py --event "$GITHUB_EVENT_PATH" --dates-out touched.txt
  python scripts/store_report.py --payload-file sample_payload.json      # local test
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import reports_lib as rl  # noqa: E402
from zumi_common import UTC, load_config, mask_secrets, repo_root, set_github_output, utc_now  # noqa: E402


def store(root: Path, payload: dict, now: dt.datetime | None = None) -> dict:
    now = now or utc_now()
    cfg = load_config(root)
    rep, stripped = rl.sanitize_any(payload, cfg.get("reports", {}), now)
    path = rl.store_raw(root, rep)
    dates = {dt.datetime.fromtimestamp(rep["ts"], UTC).date()}
    if rep["kind"] == "ea_agg":
        dates.add(dt.date.fromisoformat(rep["data"]["day"]))
    else:
        rl.upsert_deals(root, rep["acct"], rep["data"]["trades"])
        for t in rep["data"]["trades"]:
            if t.get("t_out"):
                dates.add(dt.datetime.fromtimestamp(t["t_out"], UTC).date())
        for item in rep["data"]["log"]:
            dates.add(dt.datetime.fromtimestamp(item["t"], UTC).date())
    return {"path": path, "report": rep, "dates": sorted(dates), "stripped": stripped}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Store an EA report payload")
    ap.add_argument("--root")
    ap.add_argument("--event", default=os.environ.get("GITHUB_EVENT_PATH"))
    ap.add_argument("--payload-file", help="a JSON file holding the client_payload (local testing)")
    ap.add_argument("--dates-out")
    ap.add_argument("--msg-out", help="write a commit message summarising the report here")
    args = ap.parse_args(argv)
    root = repo_root(args.root)
    try:
        if args.payload_file:
            payload = json.loads(Path(args.payload_file).read_text(encoding="utf-8"))
            if isinstance(payload.get("client_payload"), dict):
                payload = payload["client_payload"]
        elif args.event:
            payload = rl.payload_from_event(json.loads(Path(args.event).read_text(encoding="utf-8")))
        else:
            print("error: no --event or --payload-file", file=sys.stderr)
            return 2
        res = store(root, payload)
    except (rl.PayloadError, json.JSONDecodeError) as exc:
        # Never echo the payload itself: it could hold something private.
        print(f"error: rejected payload: {exc}", file=sys.stderr)
        return 1
    rep = res["report"]
    if res["stripped"]:
        # key names only, never values
        print(f"warning: stripped non-aggregate / unknown keys: {', '.join(res['stripped'][:40])}")
    if rep["kind"] == "ea_agg":
        day = rep["data"]["periods"].get("day", {})
        print(f"stored {res['path'].relative_to(root)} acct={rep['acct']} aggregate day={rep['data']['day']} "
              f"n={day.get('n', 0)}")
    else:
        print(f"stored {res['path'].relative_to(root)} acct={rep['acct']} part={rep['part']}/{rep['parts']} "
              f"trades={len(rep['data']['trades'])} log={len(rep['data']['log'])}")
    if args.dates_out:
        Path(args.dates_out).write_text("".join(d.isoformat() + "\n" for d in res["dates"]), encoding="utf-8")
    if args.msg_out:
        if rep["kind"] == "ea_agg":
            day = rep["data"]["periods"].get("day", {})
            msg = (f"reports: {rep['acct']} aggregate {rep['data']['day']} ({rep['data'].get('reason', '-')}): "
                   f"day {day.get('n', 0)} trade(s), net R {day.get('net_r', 0) or 0:+.2f}, "
                   f"net {day.get('net_pct', 0) or 0:+.2f}%")
        else:
            pl = [t["pl_pct"] for t in rep["data"]["trades"] if t.get("pl_pct") is not None]
            msg = (f"reports: {rep['acct']} part {rep['part']}/{rep['parts']}, {len(rep['data']['trades'])} deal(s)"
                   + (f" {sum(pl):+.2f}%" if pl else "") + f", {len(rep['data']['log'])} log line(s), "
                   + " ".join(d.isoformat() for d in res["dates"]))
        Path(args.msg_out).write_text(mask_secrets(msg) + "\n", encoding="utf-8")
    set_github_output("acct", rep["acct"])
    set_github_output("dates", " ".join(d.isoformat() for d in res["dates"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
