"""Offline tests for dsw with a fake dsh (no API calls). Run: python3 -m unittest -v tests/test_dsw.py"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DSW = ROOT / "dsw.py"
FAKE = ROOT / "tests" / "fake_dsh.py"


class DswTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dsw-test-"))
        self.home = self.tmp / "state"
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "base"]):
            subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True)
        key = self.tmp / "key"
        key.write_text("sk-fake")
        self.log = self.tmp / "fake.log"
        os.chmod(FAKE, 0o755)
        self.env = dict(os.environ, DSW_HOME=str(self.home), DSW_DSH=str(FAKE), DSW_KEY_FILE=str(key),
                        FAKE_LOG=str(self.log))
        self.env.pop("DSW_DEPTH", None)
        self.env["DSW_CANARY_PARENT_SECRET"] = "must-not-reach-worker"
        self.env["DSW_ENV_PASS"] = "FAKE_SCENARIO,FAKE_LOG,FAKE_NO_GUARD"

    def dsw(self, *args, scenario="ok", env=None, **kw):
        merged = dict(self.env, FAKE_SCENARIO=scenario, **(env or {}))
        return subprocess.run([sys.executable, str(DSW), *args], env=merged, capture_output=True, text=True, **kw)

    def job(self, job_id):
        return json.loads((self.home / "jobs" / job_id / "job.json").read_text())

    def fake_log(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_ok_run_records_facts_usage_metrics(self):
        r = self.dsw("run", "--repo", str(self.repo), "--id", "j1", "--task", "do it", "--check", "test -f done.txt")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        job = self.job("j1")
        self.assertEqual(job["state"], "done")
        self.assertIn("done.txt", job["facts"]["changed"])
        self.assertTrue(job["check"]["passed"])
        self.assertEqual(job["usage"], {"miss": 1000, "cache_hit": 5000, "out": 100})
        self.assertGreater(job["cost"], 0)
        self.assertIn("worker claim (unverified)", r.stdout)
        self.assertLess(r.stdout.index("diff:"), r.stdout.index("worker claim"))
        metrics = [json.loads(l) for l in (self.home / "metrics.jsonl").read_text().splitlines()]
        self.assertEqual(metrics[0]["tiers"], ["flash"])
        log = self.fake_log()[0]
        self.assertEqual(log["model"], "deepseek-flash")
        self.assertEqual(log["depth"], "1")
        self.assertTrue(log["key"])
        allowed = {"HOME", "USER", "LANG", "LC_ALL", "TERM", "TMPDIR", "XDG_RUNTIME_DIR", "PATH", "DEEPSEEK_API_KEY", "DSW_DEPTH",
                   "DSW_JOB", "FAKE_SCENARIO", "FAKE_LOG", "FAKE_NO_GUARD", "PWD", "SHLVL", "_", "OLDPWD", "LC_CTYPE"}
        self.assertLessEqual(set(log["env_keys"]), allowed, "worker env leaked parent variables")

    def test_worker_brief_gets_dsw_notes(self):
        r = self.dsw("run", "--repo", str(self.repo), "--id", "nt", "--task", "do it", "--check", "test -f done.txt")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        prompt = self.fake_log()[0]["prompt"]
        self.assertTrue(prompt.startswith("do it"))
        self.assertIn("test -f done.txt", prompt)
        self.assertIn("[SECURITY GATE]", prompt)
        self.assertIn("parallel tool calls", prompt)
        self.assertEqual(self.job("nt")["task_head"], "do it")
        r = self.dsw("run", "--repo", str(self.repo), "--id", "nt2", "--task", "x")
        self.assertNotIn("dsw runs", self.fake_log()[1]["prompt"])

    def test_worker_diet_only_in_headless_patch(self):
        sys.path.insert(0, str(DSW.parent))
        import dsw_config, dsw_setup
        cfg = dsw_config.load()
        headless, web = dsw_setup.managed_block(cfg, "headless"), dsw_setup.managed_block(cfg, "web")
        for text in (headless, web):
            self.assertIn("secret-guard", text)
        self.assertIn("- id: tool-skill\n  disabled: true", headless)
        self.assertNotIn("disabled: true", web)
        self.assertNotIn("AGENTS.worker.md", web)

    def test_worker_instructions_file_replaces_agents_md(self):
        r = self.dsw("run", "--repo", str(self.repo), "--id", "ai1", "--task", "x")
        self.assertNotIn("agent-instructions", (self.home / "jobs" / "ai1" / "overlay-1.yml").read_text())
        (self.repo / "AGENTS.worker.md").write_text("lean\n")
        r = self.dsw("run", "--repo", str(self.repo), "--id", "ai2", "--task", "x")
        self.assertEqual(r.returncode, 0, r.stderr)
        overlay = (self.home / "jobs" / "ai2" / "overlay-1.yml").read_text()
        self.assertIn("instructionFileCandidates: [AGENTS.worker.md]", overlay)
        self.assertIn("maxBytes: 65536", overlay, "dsh drops the plugin when the required maxBytes is missing")

    def test_worktree_isolation(self):
        r = self.dsw("run", "--repo", str(self.repo), "--id", "wt", "--worktree", "--task", "x")
        self.assertEqual(r.returncode, 0, r.stderr)
        job = self.job("wt")
        self.assertEqual(Path(job["workdir"]), self.repo / ".worktrees" / "dsw-wt")
        self.assertTrue((self.repo / ".worktrees" / "dsw-wt" / "done.txt").exists())
        self.assertFalse((self.repo / "done.txt").exists())
        self.assertIn(".worktrees/", (self.repo / ".git" / "info" / "exclude").read_text())
        clean = self.dsw("clean", "wt")
        self.assertEqual(clean.returncode, 1)  # dirty worktree refused
        self.assertEqual(self.dsw("clean", "wt", "--force").returncode, 0)

    def test_escalation_resumes_session_on_pro_and_rechecks(self):
        r = self.dsw("run", "--repo", str(self.repo), "--id", "esc", "--task", "x", "--escalate",
                     "--check", "grep -q good done.txt", scenario="escalate")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        job = self.job("esc")
        self.assertTrue(job["escalated"])
        self.assertEqual([a["tier"] for a in job["attempts"]], ["flash", "pro"])
        self.assertTrue(job["check"]["passed"])
        calls = self.fake_log()
        self.assertEqual([c["model"] for c in calls], ["deepseek-flash", "deepseek-v4-pro"])
        self.assertTrue(calls[1]["resumed"])

    def test_check_failure_without_escalation(self):
        r = self.dsw("run", "--repo", str(self.repo), "--id", "cf", "--task", "x",
                     "--check", "grep -q good done.txt", scenario="escalate")
        self.assertEqual(r.returncode, 3)
        self.assertEqual(self.job("cf")["state"], "check_failed")

    def test_loop_breaker(self):
        r = self.dsw("run", "--repo", str(self.repo), "--id", "lp", "--task", "x", scenario="loop")
        self.assertEqual(r.returncode, 2)
        job = self.job("lp")
        self.assertEqual(job["state"], "loop")
        self.assertEqual(job["attempts"][0]["tool_calls"], 3)

    def test_step_cap(self):
        r = self.dsw("run", "--repo", str(self.repo), "--id", "st", "--task", "x", "--max-steps", "5", scenario="steps")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.job("st")["state"], "max_steps")
        self.assertEqual(self.job("st")["attempts"][0]["steps"], 5)

    def test_question_is_needs_input(self):
        r = self.dsw("run", "--repo", str(self.repo), "--id", "q", "--task", "x", scenario="question")
        self.assertEqual(r.returncode, 4)
        self.assertEqual(self.job("q")["state"], "needs_input")

    def test_crash_is_failed(self):
        r = self.dsw("run", "--repo", str(self.repo), "--id", "cr", "--task", "x", scenario="crash")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.job("cr")["state"], "failed")

    def test_cancel_kills_whole_tree_and_releases_lock(self):
        proc = subprocess.Popen([sys.executable, str(DSW), "run", "--repo", str(self.repo), "--id", "sl", "--task", "x"],
                                env=dict(self.env, FAKE_SCENARIO="slow"), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        child = None
        for _ in range(100):
            child = next((c["child"] for c in self.fake_log() if "child" in c), None)
            if child:
                break
            time.sleep(0.1)
        self.assertIsNotNone(child)
        # a second writer on the same workspace is refused, not queued
        second = self.dsw("run", "--repo", str(self.repo), "--id", "sl2", "--task", "x")
        self.assertEqual(second.returncode, 1)
        self.assertIn("held by job sl", second.stderr)
        status = self.dsw("status")
        self.assertIn("running", status.stdout)
        self.assertEqual(self.dsw("cancel", "sl").returncode, 0)
        self.assertEqual(proc.wait(timeout=30), 2)
        self.assertEqual(self.job("sl")["state"], "cancelled")
        time.sleep(0.5)
        self.assertFalse(Path(f"/proc/{child}").exists(), "tool child survived the cancel")
        self.assertEqual(self.dsw("run", "--repo", str(self.repo), "--id", "after", "--task", "x").returncode, 0)

    def test_timeout(self):
        r = self.dsw("run", "--repo", str(self.repo), "--id", "to", "--task", "x", "--timeout", "2", scenario="slow")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.job("to")["state"], "timeout")

    def test_orphan_detection(self):
        jobs = self.home / "jobs" / "ghost"
        jobs.mkdir(parents=True)
        (jobs / "job.json").write_text(json.dumps({"id": "ghost", "state": "running", "runner_pid": 999999,
                                                   "created": time.time(), "attempts": [], "workdir": str(self.repo)}))
        self.assertIn("orphaned", self.dsw("status").stdout)

    def test_depth_guard_and_missing_guard_refuse(self):
        r = self.dsw("run", "--repo", str(self.repo), "--task", "x", env={"DSW_DEPTH": "1"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("depth guard", r.stderr)
        r = self.dsw("run", "--repo", str(self.repo), "--task", "x", env={"FAKE_NO_GUARD": "1"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("secret-guard", r.stderr)

    def test_resume_follow_up_uses_same_session(self):
        self.assertEqual(self.dsw("run", "--repo", str(self.repo), "--id", "rs", "--task", "x").returncode, 0)
        r = self.dsw("resume", "rs", "--task", "also do y")
        self.assertEqual(r.returncode, 0, r.stderr)
        job = self.job("rs")
        self.assertEqual(len(job["attempts"]), 2)
        self.assertTrue(self.fake_log()[1]["resumed"])

    def test_stats_and_statusline(self):
        self.dsw("run", "--repo", str(self.repo), "--id", "s1", "--task", "x")
        stats = self.dsw("stats", "--offline")
        self.assertIn("1 jobs", stats.stdout)
        self.assertIn("flash", stats.stdout)
        self.assertRegex(self.dsw("statusline").stdout, r"⚙dsw 0▶ ✓1 ✗0 \$")


if __name__ == "__main__":
    unittest.main()
