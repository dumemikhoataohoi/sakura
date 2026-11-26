"""Tiny GitHub REST client (requests only) for the alerts PR. Token is never printed."""
from __future__ import annotations

import base64

from zumi_common import mask_secrets

API = "https://api.github.com"


class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"GitHub API {status}: {mask_secrets(message)[:300]}")
        self.status = status


class GitHub:
    def __init__(self, token: str, repo: str, session=None, api: str = API):
        if session is None:
            import requests
            session = requests.Session()
        self.s = session
        self.repo = repo
        self.api = api.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "sakura-alerts-bot",
        }

    def request(self, method: str, path: str, ok=(200, 201, 204), **kw):
        url = path if path.startswith("http") else f"{self.api}/repos/{self.repo}{path}"
        resp = self.s.request(method, url, headers=self.headers, timeout=30, **kw)
        if resp.status_code not in ok:
            raise GitHubError(resp.status_code, getattr(resp, "text", ""))
        return resp

    def paginate(self, path: str, params: dict | None = None) -> list:
        out, url, params = [], path, dict(params or {}, per_page=100)
        while url:
            resp = self.request("GET", url, params=params)
            out.extend(resp.json())
            url = (getattr(resp, "links", {}) or {}).get("next", {}).get("url")
            params = None
        return out

    # ---- alerts PR helpers
    def find_open_pr_with_label(self, label: str) -> int | None:
        items = self.paginate("/issues", {"state": "open", "labels": label})
        prs = sorted(i["number"] for i in items if "pull_request" in i)
        return prs[0] if prs else None

    def ensure_label(self, label: str, color: str = "d73a4a") -> None:
        try:
            self.request("POST", "/labels", json={"name": label, "color": color,
                                                  "description": "Long-lived PR whose comments are Zumi alerts"})
        except GitHubError as exc:
            if exc.status != 422:  # 422 = already exists
                raise

    def branch_sha(self, branch: str) -> str | None:
        try:
            return self.request("GET", f"/git/ref/heads/{branch}").json()["object"]["sha"]
        except GitHubError as exc:
            if exc.status == 404:
                return None
            raise

    def create_branch(self, branch: str, sha: str) -> None:
        self.request("POST", "/git/refs", json={"ref": f"refs/heads/{branch}", "sha": sha})

    def put_file(self, branch: str, path: str, text: str, message: str) -> None:
        sha = None
        try:
            sha = self.request("GET", f"/contents/{path}", params={"ref": branch}).json().get("sha")
        except GitHubError as exc:
            if exc.status != 404:
                raise
        body = {"message": message, "branch": branch,
                "content": base64.b64encode(text.encode()).decode()}
        if sha:
            body["sha"] = sha
        self.request("PUT", f"/contents/{path}", json=body)

    def create_pr(self, head: str, base: str, title: str, body: str) -> int:
        return self.request("POST", "/pulls", json={"head": head, "base": base, "title": title,
                                                    "body": body}).json()["number"]

    def add_labels(self, number: int, labels: list[str]) -> None:
        self.request("POST", f"/issues/{number}/labels", json={"labels": labels})

    def comment_bodies(self, number: int) -> list[str]:
        return [c.get("body", "") for c in self.paginate(f"/issues/{number}/comments")]

    def comment(self, number: int, body: str) -> None:
        self.request("POST", f"/issues/{number}/comments", json={"body": body})
