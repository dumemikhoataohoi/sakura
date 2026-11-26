import json
import os
import re
import unittest
from unittest import mock

from helpers import FakeDataSession, FakeResponse, fixture_json, make_root, synthetic_fred

import alerts
import build_latest
import store_report
from gh_api import GitHub
from zumi_common import load_config, parse_utc


class FakeGitHubAPI:
    """In-memory stand-in for the few REST endpoints alerts.py uses."""

    def __init__(self, branches=("main",), prs=None):
        self.branches = {b: "sha-" + b for b in branches}
        self.labels = set()
        self.files = {}
        self.prs = dict(prs or {})  # number -> {"labels": [...], "comments": [...]}
        self.log = []

    def request(self, method, url, headers=None, timeout=None, json=None, params=None):
        path = re.sub(r"^https://api\.github\.com/repos/[^/]+/[^/]+", "", url)
        self.log.append((method, path))
        assert headers["Authorization"].startswith("Bearer ")
        if method == "GET" and path == "/issues":
            items = [{"number": n, "pull_request": {}} for n, pr in self.prs.items() if params["labels"] in pr["labels"]]
            return FakeResponse(200, payload=items)
        if method == "POST" and path == "/labels":
            if json["name"] in self.labels:
                return FakeResponse(422, payload={"message": "already_exists"})
            self.labels.add(json["name"])
            return FakeResponse(201, payload={})
        m = re.match(r"/git/ref/heads/(.+)", path)
        if method == "GET" and m:
            b = m.group(1)
            return FakeResponse(200, payload={"object": {"sha": self.branches[b]}}) if b in self.branches else FakeResponse(404, payload={})
        if method == "POST" and path == "/git/refs":
            self.branches[json["ref"].split("/")[-1]] = json["sha"]
            return FakeResponse(201, payload={})
        m = re.match(r"/contents/(.+)", path)
        if m and method == "GET":
            key = (params["ref"], m.group(1))
            return FakeResponse(200, payload={"sha": "f1"}) if key in self.files else FakeResponse(404, payload={})
        if m and method == "PUT":
            self.files[(json["branch"], m.group(1))] = json["content"]
            return FakeResponse(201, payload={})
        if method == "POST" and path == "/pulls":
            if json["head"] not in self.branches:
                return FakeResponse(422, payload={})
            n = max(self.prs, default=0) + 1
            self.prs[n] = {"labels": [], "comments": []}
            return FakeResponse(201, payload={"number": n})
        m = re.match(r"/issues/(\d+)/(labels|comments)", path)
        if m:
            pr = self.prs[int(m.group(1))]
            if m.group(2) == "labels":
                pr["labels"].extend(json["labels"])
                return FakeResponse(200, payload=[])
            if method == "GET":
                return FakeResponse(200, payload=[{"body": b} for b in pr["comments"]])
            pr["comments"].append(json["body"])
            return FakeResponse(201, payload={})
        raise AssertionError(f"unexpected {method} {path}")


class AlertRulesTest(unittest.TestCase):
    """Detail-mode (v1) rules, run against a private-mode root."""

    def setUp(self):
        self.root = make_root(public_mode=False)
        self.cfg = load_config(self.root)

    def build_with_vix_jump(self, now):
        vix = synthetic_fred("VIXCLS", 18.0).rstrip("\n").split("\n")
        vix[-1] = vix[-1].split(",")[0] + ",24.0"  # +33% day
        build_latest.build(self.root, now, session=FakeDataSession(overrides={"VIXCLS": "\n".join(vix) + "\n"}))

    def test_data_and_ea_rules(self):
        now = parse_utc("2026-11-26T14:00:00Z")
        self.build_with_vix_jump(now)
        payload = fixture_json("payload_basic.json")
        payload["ts"] = int(parse_utc("2026-11-26T13:00:00Z").timestamp())
        store_report.store(self.root, payload, now)
        keys = {a["key"] for a in alerts.evaluate(self.root, self.cfg, now)}
        acct = "a1b2c3d4e5f60718"
        self.assertIn("vix_jump:2026-11-25", keys)
        self.assertIn(f"dayloss:{acct}:2026-11-26", keys)
        self.assertIn(f"gold:{acct}:2026-11-26", keys)
        self.assertIn(f"streak:{acct}:501", keys)
        self.assertIn(f"rule:{acct}:no_sl:503", keys)
        self.assertIn(f"rule:{acct}:risk_cap:502", keys)
        self.assertIn(f"rule:{acct}:news:504", keys)
        self.assertTrue(any(k.startswith(f"eaflag:{acct}:spread_wide") for k in keys))
        self.assertIn("event:NFP:2026-11-27T13:30:00Z", keys)  # within 24 h

    def test_quiet_day_has_no_alerts(self):
        now = parse_utc("2026-11-20T09:00:00Z")
        build_latest.build(self.root, now, session=FakeDataSession())
        self.assertEqual(alerts.evaluate(self.root, self.cfg, now), [])

    def test_quality_failure_alert(self):
        now = parse_utc("2026-11-20T09:00:00Z")
        build_latest.build(self.root, now, session=FakeDataSession())
        (self.root / "data/quality.json").write_text(json.dumps(
            {"ok": False, "issues": [{"sid": "VIXCLS", "kind": "range", "level": "error", "msg": "VIXCLS: value 9999"}]}))
        keys = [a["key"] for a in alerts.evaluate(self.root, self.cfg, now)]
        self.assertTrue(any(k.startswith("quality:2026-11-20:") for k in keys))

    def test_workflow_failure_alert(self):
        now = parse_utc("2026-11-20T09:00:00Z")
        build_latest.build(self.root, now, session=FakeDataSession())
        with mock.patch.dict(os.environ, {"WF_NAME": "data", "WF_CONCLUSION": "failure"}):
            keys = [a["key"] for a in alerts.evaluate(self.root, self.cfg, now)]
        self.assertIn("wf_fail:data:2026-11-20", keys)


class AlertPostingTest(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(make_root())
        self.now = parse_utc("2026-11-26T09:00:00Z")
        self.items = [alerts.alert("vix_jump:2026-11-25", "high", "VIX tăng", "VIX up"),
                      alerts.alert("event:NFP:x", "info", "NFP sắp tới", "NFP soon")]

    def test_creates_branch_pr_and_dedupes(self):
        api = FakeGitHubAPI()
        gh = GitHub("test-token", "owner/repo", session=api)
        number, posted = alerts.post_alerts(gh, self.cfg, self.items, self.now, "main")
        self.assertEqual((number, posted), (1, 2))
        self.assertIn("alerts", api.branches)
        self.assertIn(("alerts", "alerts/README.md"), api.files)
        self.assertEqual(api.prs[1]["labels"], ["zumi-alerts"])
        body = api.prs[1]["comments"][0]
        self.assertIn("<!-- zumi-alert-key: vix_jump:2026-11-25 -->", body)
        self.assertIn("VIX tăng", body)
        self.assertIn("EN: VIX up", body)
        # second run: same alerts are not posted again, the existing PR is reused
        number, posted = alerts.post_alerts(gh, self.cfg, self.items, self.now, "main")
        self.assertEqual((number, posted), (1, 0))
        self.assertEqual(len(api.prs), 1)
        extra = self.items + [alerts.alert("dayloss:a:2026-11-26", "high", "lỗ", "loss")]
        _, posted = alerts.post_alerts(gh, self.cfg, extra, self.now, "main")
        self.assertEqual(posted, 1)
        self.assertNotIn("vix_jump", api.prs[1]["comments"][-1])

    def test_reuses_existing_labelled_pr(self):
        api = FakeGitHubAPI(branches=("main", "alerts"), prs={5: {"labels": ["zumi-alerts"], "comments": []}})
        number, posted = alerts.post_alerts(GitHub("t", "o/r", session=api), self.cfg, self.items, self.now, "main")
        self.assertEqual((number, posted), (5, 2))
        self.assertNotIn(("POST", "/pulls"), api.log)


if __name__ == "__main__":
    unittest.main()
