import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).with_name("promote.sh").resolve()


@unittest.skipUnless(os.name == "posix", "Linux release workflow")
class PromotionTests(unittest.TestCase):
    def git(self, *args, cwd=None):
        return subprocess.check_output(["git", *args], cwd=cwd, text=True, stderr=subprocess.DEVNULL).strip()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote.git"
        self.git("init", "--bare", "--initial-branch=main", str(self.remote))
        self.seed = self.root / "seed"
        self.git("clone", str(self.remote), str(self.seed))
        self.identity(self.seed)
        for app in ("backend", "frontend"):
            folder = self.seed / "apps" / app
            folder.mkdir(parents=True)
            (folder / "image.env").write_text("# first release\n")
        self.git("add", ".", cwd=self.seed)
        self.git("commit", "-m", "initial", cwd=self.seed)
        self.git("push", "origin", "main", cwd=self.seed)
        self.work = self.root / "work"
        self.work.mkdir()
        self.git("clone", str(self.remote), str(self.work / ".infra"))
        tools = self.root / "bin"
        tools.mkdir()
        (tools / "gh").write_text("#!/bin/sh\nprintf '%s\\n' \"$TEST_SOURCE_HEAD\"\n")
        (tools / "gh").chmod(0o755)
        self.sha = "a" * 40
        self.env = dict(os.environ, APP="backend", SOURCE_SHA=self.sha,
                        IMAGE="ghcr.io/womenup-d/backend@sha256:" + "b" * 64,
                        GH_TOKEN="test", TEST_SOURCE_HEAD=self.sha,
                        PATH=str(tools) + os.pathsep + os.environ["PATH"])

    def identity(self, repo):
        self.git("config", "user.name", "test", cwd=repo)
        self.git("config", "user.email", "test@example.invalid", cwd=repo)

    def promote(self, **env):
        result = subprocess.run(["bash", str(SCRIPT)], cwd=self.work,
                                env=self.env | env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_concurrent_other_app_commit_survives_rebase(self):
        other = self.seed / "apps/frontend/image.env"
        other.write_text("# a concurrent frontend release\n")
        self.git("add", ".", cwd=self.seed)
        self.git("commit", "-m", "frontend", cwd=self.seed)
        self.git("push", "origin", "main", cwd=self.seed)
        self.promote()
        self.assertIn("concurrent frontend", self.git("--git-dir", str(self.remote), "show", "main:apps/frontend/image.env"))
        self.assertIn(self.sha, self.git("--git-dir", str(self.remote), "show", "main:apps/backend/image.env"))

    def test_stale_build_does_not_promote(self):
        before = self.git("--git-dir", str(self.remote), "rev-parse", "main")
        self.promote(TEST_SOURCE_HEAD="c" * 40)
        self.assertEqual(before, self.git("--git-dir", str(self.remote), "rev-parse", "main"))

    def test_identical_release_is_noop(self):
        self.promote()
        before = self.git("--git-dir", str(self.remote), "rev-parse", "main")
        self.promote()
        self.assertEqual(before, self.git("--git-dir", str(self.remote), "rev-parse", "main"))


if __name__ == "__main__":
    unittest.main()
