"""Integration test of scripts/commit_push.sh against a local bare repository (no network)."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import REPO

SCRIPT = REPO / "scripts" / "commit_push.sh"


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@unittest.skipUnless(shutil.which("git") and shutil.which("bash"), "git/bash not available")
class CommitPushTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sakura_git_"))
        self.origin = self.tmp / "origin.git"
        git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.origin))
        seed = self.tmp / "seed"
        git(self.tmp, "clone", "-q", str(self.origin), str(seed))
        for repo in (seed,):
            git(repo, "config", "user.name", "t")
            git(repo, "config", "user.email", "t@example.invalid")
        (seed / "data").mkdir()
        (seed / "data" / "a.txt").write_text("1\n")
        git(seed, "add", ".")
        git(seed, "commit", "-q", "-m", "seed")
        git(seed, "push", "-q", "origin", "HEAD:main")
        self.a = self.tmp / "a"
        self.b = self.tmp / "b"
        git(self.tmp, "clone", "-q", str(self.origin), str(self.a))
        git(self.tmp, "clone", "-q", str(self.origin), str(self.b))
        self.msg = self.tmp / "msg.txt"
        self.msg.write_text("data: test\n")

    def run_script(self, cwd, regen, *paths):
        return subprocess.run(["bash", str(SCRIPT), str(self.msg), regen, *paths], cwd=cwd,
                              capture_output=True, text=True, env={"PATH": "/usr/bin:/bin:/usr/local/bin",
                                                                   "TARGET_BRANCH": "main", "HOME": str(self.tmp)})

    def test_nothing_to_commit(self):
        res = self.run_script(self.a, "true", "data/")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("nothing to commit", res.stdout)

    def test_rebase_on_race(self):
        (self.b / "data" / "b.txt").write_text("b\n")
        self.assertEqual(self.run_script(self.b, "true", "data/").returncode, 0)
        (self.a / "data" / "c.txt").write_text("c\n")  # different file: rebase succeeds
        res = self.run_script(self.a, "true", "data/")
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        log = git(self.a, "log", "--oneline", "origin/main")
        self.assertEqual(log.count("data: test"), 2)

    def test_conflict_resets_and_regenerates(self):
        (self.b / "data" / "a.txt").write_text("from b\n")
        self.assertEqual(self.run_script(self.b, "true", "data/").returncode, 0)
        (self.a / "data" / "a.txt").write_text("from a\n")  # same file: rebase conflicts
        res = self.run_script(self.a, "echo regenerated > data/a.txt", "data/")
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        git(self.b, "pull", "-q", "--rebase", "origin", "main")
        self.assertEqual((self.b / "data" / "a.txt").read_text(), "regenerated\n")


if __name__ == "__main__":
    unittest.main()
