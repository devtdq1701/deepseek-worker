"""Regression tests for the 2026-11-16 security review (no API calls; needs bwrap)."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import dsw  # noqa: E402

DSW = ROOT / "dsw.py"


def make_repo() -> Path:
    repo = Path(tempfile.mkdtemp(prefix="dsw-sec-")) / "repo"
    repo.mkdir()
    for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "base"]):
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True)
    return repo


class SecurityTest(unittest.TestCase):
    def test_check_runs_in_sandbox_not_on_host(self):
        repo = make_repo()
        marker = Path.home() / f".dsw-sec-marker-{os.getpid()}"
        (repo / "conftest.py").write_text(f"open({str(marker)!r}, 'w').write('host')\n")
        result = dsw.run_check(str(repo), "python3 -c 'import conftest'; ls -A ~/.ssh 2>/dev/null | wc -l", timeout=30)
        self.assertTrue(result["sandboxed"])
        self.assertFalse(marker.exists(), "check wrote to the real home directory")
        self.assertEqual(result["tail"].strip().splitlines()[-1].strip(), "0", "check could list ~/.ssh")

    def test_hostile_git_config_does_not_run_on_host(self):
        repo = make_repo()
        marker = Path(tempfile.mkdtemp(prefix="dsw-sec-fsmonitor-")) / "ran"
        subprocess.run(["git", "-C", str(repo), "config", "core.fsmonitor", f"touch {marker}; false"], check=True)
        (repo / "x.txt").write_text("x")
        facts = dsw.facts(repo, None)
        self.assertTrue(facts["git"])
        self.assertIn("x.txt", facts["changed"])
        self.assertFalse(marker.exists(), "core.fsmonitor ran on the host")

    def test_clean_refuses_a_replaced_git_pointer(self):
        repo = make_repo()
        home = Path(tempfile.mkdtemp(prefix="dsw-sec-state-"))
        env = dict(os.environ, DSW_HOME=str(home), DSW_SKIP_GUARD_CHECK="1")
        worktree, branch = dsw.ensure_worktree(repo, "wt")
        (home / "jobs" / "wt").mkdir(parents=True)
        (home / "jobs" / "wt" / "job.json").write_text(json.dumps(
            {"id": "wt", "state": "done", "repo": str(repo), "workdir": str(worktree), "branch": branch,
             "created": time.time(), "attempts": []}))
        (worktree / ".git").unlink()
        subprocess.run(["git", "init", "-q", str(worktree)], check=True)  # worker-made repo in place of the pointer
        out = subprocess.run([sys.executable, str(DSW), "clean", "wt", "--force"], env=env, capture_output=True, text=True)
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertIn("no longer points", out.stderr)
        self.assertTrue(worktree.exists())

    def test_dashboard_rejects_foreign_host_and_origin(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        home = Path(tempfile.mkdtemp(prefix="dsw-sec-web-"))
        env = dict(os.environ, DSW_HOME=str(home))
        server = subprocess.Popen([sys.executable, str(DSW), "serve", "--port", str(port)], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            for _ in range(50):
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                    break
                except OSError:
                    time.sleep(0.1)

            def get(headers):
                request = urllib.request.Request(f"http://127.0.0.1:{port}/api/jobs", headers=headers)
                try:
                    return urllib.request.urlopen(request, timeout=5).status
                except urllib.error.HTTPError as error:
                    return error.code
            self.assertEqual(get({}), 200)
            self.assertEqual(get({"Host": "evil.example"}), 403)
            self.assertEqual(get({"Host": f"evil.example:{port}"}), 403)
            self.assertEqual(get({"Origin": "http://evil.example"}), 403)
            self.assertEqual(get({"Sec-Fetch-Site": "cross-site"}), 403)
        finally:
            server.terminate()
            server.wait(timeout=10)

    def test_dot_job_ids_are_refused(self):
        repo = make_repo()
        env = dict(os.environ, DSW_HOME=tempfile.mkdtemp(prefix="dsw-sec-id-"), DSW_SKIP_GUARD_CHECK="1",
                   DSW_KEY_FILE=str(repo / "nokey"), DEEPSEEK_API_KEY="sk-fake")
        for job_id in (".", ".."):
            out = subprocess.run([sys.executable, str(DSW), "run", "--repo", str(repo), "--id", job_id, "--task", "x"],
                                 env=env, capture_output=True, text=True)
            self.assertEqual(out.returncode, 1, job_id)
            self.assertIn("invalid job id", out.stderr)


if __name__ == "__main__":
    unittest.main()
