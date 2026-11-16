#!/usr/bin/env python3
"""dsw: dispatch DeepSeek Harness (dsh headless) workers for a Claude Code session.

Claude stays the brain; each `dsw run` is one DeepSeek worker with its own tool loop,
started by Claude with `run_in_background` so the harness notifies on completion.

Mechanisms and where they come from:
  agy-review-loop-core/agy_round.py : per-task worktree + branch, usage totals, exit codes
  dsh-crew (ZSeven-W)               : jobs board, live progress, tier/effort, escalate
                                      flash->pro on failure, workspace lock, depth guard,
                                      orphan detection, statusline segment
  added here                        : process-tree kill, step cap, repeated-call breaker,
                                      --check, peak-aware cost metrics, balance, preflight

Exit codes (run/resume): 0 done (and check passed), 1 refused before start, 2 worker
failed or was stopped, 3 check failed, 4 worker needs input.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import selectors
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling modules when run via a symlink
import dsw_config  # noqa: E402

CFG = dsw_config.load()  # CLI flag > env DSW_* > ~/.config/dsw/config.toml > auto-discovery
HOME = CFG.state_dir
DSH = str(CFG.dsh) if CFG.dsh else ""
NODE_BIN = str(CFG.node_bin) if CFG.node_bin else ""
KEY_FILE = CFG.key_file
MAX_DEPTH = 1  # a worker may not dispatch further workers

MODELS = {"flash": "deepseek-flash", "pro": "deepseek-v4-pro"}
DEFAULT_EFFORT = {"flash": CFG.defaults["effort_flash"], "pro": CFG.defaults["effort_pro"]}
DEFAULT_MAX_STEPS = {"flash": int(CFG.defaults["max_steps"]), "pro": int(CFG.defaults["max_steps_pro"])}
# USD per 1M tokens at peak (cache hit, cache miss, output); off-peak is half.
PRICES = {"flash": (0.006, 0.30, 1.20), "pro": (0.044, 1.32, 3.96)}
PEAK_UTC_HOURS = set(range(1, 4)) | set(range(6, 10))  # Mon-Fri 01-04, 06-10 UTC

EXIT_OK, EXIT_REFUSED, EXIT_FAILED, EXIT_CHECK_FAILED, EXIT_NEEDS_INPUT = 0, 1, 2, 3, 4
LOOP_REPEAT = 3  # identical tool call this many times in a row -> stop


class Refused(Exception):
    """Dispatch refused before any worker started."""


# ----------------------------------------------------------------------------- state

def now() -> float:
    return time.time()


def iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_json(path: Path, data: dict) -> None:
    """Atomic write: temp file then rename, so readers never see half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def job_dir(job_id: str) -> Path:
    return HOME / "jobs" / job_id


def load_job(job_id: str) -> dict:
    path = job_dir(job_id) / "job.json"
    if not path.is_file():
        raise SystemExit(f"dsw: no job {job_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def all_jobs() -> list[dict]:
    jobs = []
    for path in sorted((HOME / "jobs").glob("*/job.json")):
        try:
            jobs.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(jobs, key=lambda job: job.get("created", 0))


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def effective_state(job: dict) -> str:
    """A running job whose runner died is an orphan, never silently 'running'."""
    if job.get("state") == "running" and not pid_alive(job.get("runner_pid")):
        return "orphaned"
    return job.get("state", "?")


# ----------------------------------------------------------------------------- cost

def is_peak(ts: float) -> bool:
    moment = dt.datetime.fromtimestamp(ts, dt.timezone.utc)
    return moment.weekday() < 5 and moment.hour in PEAK_UTC_HOURS


def cost_usd(tier: str, usage: dict, started: float) -> float:
    hit, miss, out = PRICES[tier]
    factor = 1.0 if is_peak(started) else 0.5
    return factor * (usage.get("cache_hit", 0) * hit + usage.get("miss", 0) * miss + usage.get("out", 0) * out) / 1e6


def read_key() -> str:
    """Key file first (config `key_file`), else env DEEPSEEK_API_KEY."""
    if KEY_FILE and KEY_FILE.is_file():
        try:
            key = KEY_FILE.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise Refused(f"DeepSeek key file unreadable: {KEY_FILE} ({error.strerror})")
        if key:
            return key
    if os.environ.get("DEEPSEEK_API_KEY", "").strip():
        return os.environ["DEEPSEEK_API_KEY"].strip()
    raise Refused(f"no DeepSeek key: put it in {KEY_FILE} (chmod 600) or set DEEPSEEK_API_KEY")


def balance() -> str:
    request = urllib.request.Request("https://api.deepseek.com/user/balance",
                                     headers={"Authorization": f"Bearer {read_key()}"})
    with urllib.request.urlopen(request, timeout=15) as response:
        data = json.load(response)
    infos = data.get("balance_infos") or [{}]
    return f"{infos[0].get('total_balance', '?')} {infos[0].get('currency', '')}".strip()


ENV_ALLOWLIST = ("HOME", "USER", "LANG", "LC_ALL", "TERM", "TMPDIR", "XDG_RUNTIME_DIR")


def clean_env(**extra: str) -> dict:
    """Allowlisted env for workers and checks: the parent (Claude Desktop) env carries its own
    auth and tokens, which must never reach a worker or code it wrote."""
    passthrough = [name for name in os.environ.get("DSW_ENV_PASS", "").split(",") if name]  # explicit opt-in, e.g. HTTPS_PROXY
    env = {key: os.environ[key] for key in (*ENV_ALLOWLIST, *passthrough) if key in os.environ}
    env["PATH"] = f"{NODE_BIN}:/usr/local/bin:/usr/bin:/bin"
    env.update(extra)
    return env


# ----------------------------------------------------------------------------- git

# Config a hostile repo could use to run commands when git touches it.
SAFE_GIT = ["-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-c", "core.pager=cat",
            "-c", "diff.external=", "-c", "core.sshCommand=false", "-c", "core.askPass=false"]


def git_env() -> dict:
    return clean_env(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null", GIT_OPTIONAL_LOCKS="0",
                     GIT_TERMINAL_PROMPT="0")


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    """Host git, for repos the worker has not touched (or only via their trusted common dir)."""
    return subprocess.run(["git", *SAFE_GIT, "-C", str(cwd), *args], capture_output=True, text=True, env=git_env())


def sandbox_run(argv: list[str], workdir: str | Path, timeout: int = 120, extra_env: dict | None = None
                ) -> subprocess.CompletedProcess:
    """Run a command on a worker-touched workspace inside dsw-bwrap (same jail as the worker's
    tools): worker-written test files, git config or hooks then cannot reach the host."""
    wd = str(Path(workdir).resolve())
    wrapper = str(dsw_config.HERE / "sandbox" / "dsw-bwrap")
    profile = ["--ro-bind", "/", "/", "--dev", "/dev", "--unshare-pid", "--proc", "/proc", "--die-with-parent",
               "--tmpfs", "/tmp", "--bind", wd, wd, "--chdir", wd]
    proc = subprocess.Popen([wrapper, *profile, "--", *argv], cwd=wd, env=clean_env(**(extra_env or {})),
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        out, err = proc.communicate()
        code = 124
    return subprocess.CompletedProcess(argv, code, out, err)


def sandbox_git(workdir: Path, *args: str) -> subprocess.CompletedProcess:
    env = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_OPTIONAL_LOCKS": "0",
           "GIT_TERMINAL_PROMPT": "0"}
    return sandbox_run(["git", *SAFE_GIT, *args], workdir, timeout=60, extra_env=env)


def ensure_worktree(repo: Path, job_id: str) -> tuple[Path, str]:
    """Create `<repo>/.worktrees/dsw-<id>` on branch `dsw/<id>` from HEAD (agy_round pattern)."""
    worktree, branch = repo / ".worktrees" / f"dsw-{job_id}", f"dsw/{job_id}"
    if worktree.is_dir():
        return worktree, branch
    common = git(repo, "rev-parse", "--git-common-dir").stdout.strip()
    if not common:
        raise Refused(f"{repo} is not a git repository")
    exclude = (Path(common) if Path(common).is_absolute() else repo / common) / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    if ".worktrees/" not in existing.split():
        exclude.write_text(existing + ("" if existing.endswith("\n") or not existing else "\n") + ".worktrees/\n",
                           encoding="utf-8")
    added = git(repo, "worktree", "add", "-b", branch, str(worktree), "HEAD")
    if added.returncode:
        raise Refused(f"git worktree add failed: {added.stderr.strip()}")
    return worktree, branch


def facts(workdir: Path, base: str | None) -> dict:
    """Observed repository changes: what the reviewer trusts, unlike the worker's claim.
    Runs git inside the sandbox: the worker may have planted git config, hooks or filters."""
    if sandbox_git(workdir, "rev-parse", "--is-inside-work-tree").returncode:
        return {"git": False}
    noise = re.compile(r"(^|/)(__pycache__|\.pytest_cache|node_modules)(/|$)|\.pyc$")
    status = [line for line in sandbox_git(workdir, "status", "--porcelain").stdout.splitlines() if not noise.search(line[3:])]
    diff_base = [base] if base else []
    stat = sandbox_git(workdir, "diff", "--stat", *diff_base).stdout.strip().splitlines()
    untracked = sum(1 for line in status if line.startswith("??"))
    summary = stat[-1].strip() if stat else "no tracked changes"
    if untracked:
        summary += f", {untracked} new untracked file(s)"
    numstat = {}
    for line in sandbox_git(workdir, "diff", "--numstat", *diff_base).stdout.splitlines():
        added, removed, path = (line.split("\t", 2) + ["", ""])[:3]
        numstat[path] = (added, removed)
    files = [{"status": line[:2].strip() or "?", "path": line[3:],
              "added": numstat.get(line[3:], ("", ""))[0], "removed": numstat.get(line[3:], ("", ""))[1]}
             for line in status]
    return {"git": True, "changed": [line[3:] for line in status], "files": files, "diffstat": summary}


# ----------------------------------------------------------------------------- locks

def lock_path(workdir: Path) -> Path:
    return HOME / "locks" / (hashlib.sha256(str(workdir.resolve()).encode()).hexdigest()[:16] + ".json")


def acquire_lock(workdir: Path, job_id: str) -> None:
    """One writer per workspace; a second dispatch is refused with the holder, never queued."""
    path = lock_path(workdir)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = json.dumps({"job": job_id, "pid": os.getpid(), "workdir": str(workdir)})
    for _ in range(2):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # atomic: two runs cannot both win
        except FileExistsError:
            try:
                holder = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                holder = {}
            if holder.get("job") == job_id:
                write_json(path, json.loads(record))
                return
            if pid_alive(holder.get("pid")):
                raise Refused(f"workspace {workdir} is held by job {holder.get('job')} (pid {holder.get('pid')}); "
                              f"wait, cancel it, or use --worktree")
            path.unlink(missing_ok=True)  # stale lock of a dead runner
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(record)
        return
    raise Refused(f"could not take the workspace lock for {workdir}")


def release_lock(workdir: Path, job_id: str) -> None:
    path = lock_path(workdir)
    try:
        if json.loads(path.read_text(encoding="utf-8")).get("job") == job_id:
            path.unlink()
    except (OSError, json.JSONDecodeError):
        pass


# ----------------------------------------------------------------------------- process tree

def descendants(root: int) -> list[int]:
    """All live descendants of root (snapshot of /proc), deepest last."""
    children: dict[int, list[int]] = {}
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = stat.read_text().rsplit(")", 1)[1].split()
            children.setdefault(int(fields[1]), []).append(int(stat.parent.name))
        except (OSError, IndexError, ValueError):
            continue
    found, queue = [], [root]
    while queue:
        for child in children.get(queue.pop(0), []):
            found.append(child)
            queue.append(child)
    return found


def kill_tree(proc: subprocess.Popen, grace: float = 3.0) -> None:
    """dsh's bash tool runs children in their own session; a process-group kill misses them."""
    pids = [proc.pid, *descendants(proc.pid)]
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in reversed(pids):
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                pass
        deadline = now() + grace
        while now() < deadline and any(pid_alive(pid) for pid in pids):
            try:
                proc.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                pass
            pids = [pid for pid in pids if pid_alive(pid)]
        if not pids:
            break
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


# ----------------------------------------------------------------------------- one attempt

WORKER_INSTRUCTIONS = "AGENTS.worker.md"


def overlay(job_id: str, attempt: int, tier: str, effort: str, workdir: Path | None = None) -> Path:
    path = job_dir(job_id) / f"overlay-{attempt}.yml"
    text = ("- id: agent-default-model\n  config:\n    provider: deepseek-official\n"
            f"    model: {MODELS[tier]}\n    reasoningEffort: {effort}\n")
    # dsh loads every candidate file it finds; a repo's lean worker file must replace AGENTS.md/CLAUDE.md, not join them.
    if workdir and (workdir / WORKER_INSTRUCTIONS).is_file():
        # A patch replaces the whole config object: restate the required maxBytes.
        text += (f"- id: agent-instructions\n  config:\n    maxBytes: 65536\n"
                 f"    instructionFileCandidates: [{WORKER_INSTRUCTIONS}]\n")
    path.write_text(text, encoding="utf-8")
    return path


def summarize_call(event: dict) -> str:
    data = event.get("input") or {}
    detail = data.get("command") or data.get("file_path") or data.get("pattern") or ""
    return f"{event.get('tool', '?')} {str(detail)[:80]}".strip()


class JobState:
    """job.json writer: live progress for status/statusline, written atomically."""

    def __init__(self, job: dict):
        self.job = job
        self.path = job_dir(job["id"]) / "job.json"
        self.last_write = 0.0

    def save(self, force: bool = False) -> None:
        if force or now() - self.last_write >= 1.0:
            self.job["updated"] = now()
            write_json(self.path, self.job)
            self.last_write = now()


def run_attempt(state: JobState, prompt: str, tier: str, effort: str, session_id: str | None,
                max_steps: int, deadline: float | None) -> dict:
    job = state.job
    number = len(job["attempts"]) + 1
    attempt = {"n": number, "tier": tier, "effort": effort, "started": now(), "steps": 0, "tool_calls": 0,
               "usage": {"miss": 0, "cache_hit": 0, "out": 0}, "current": None, "stop": None,
               "session_id": session_id, "final": "", "exit": None}
    job["attempts"].append(attempt)
    env = clean_env(DEEPSEEK_API_KEY=read_key(), DSW_DEPTH=str(job["depth"] + 1), DSW_JOB=job["id"])
    cmd = [DSH, "--patch", str(overlay(job["id"], number, tier, effort, Path(job["workdir"]))), "--profile", "headless", "--json",
           "--permission-mode", "workspace-write", "--approval-policy", "never"]
    if session_id:
        cmd += ["--session-id", session_id]
    cmd.append(prompt)
    events = open(job_dir(job["id"]) / "events.jsonl", "a", encoding="utf-8")
    stderr = open(job_dir(job["id"]) / "stderr.log", "a", encoding="utf-8")
    proc = subprocess.Popen(cmd, cwd=job["workdir"], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=stderr, text=True, start_new_session=True)
    job["worker_pid"] = proc.pid
    state.save(force=True)
    recent: list[str] = []
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    try:
        while True:
            if job.get("cancel_requested"):
                attempt["stop"] = "cancelled"
            elif deadline and now() > deadline:
                attempt["stop"] = "timeout"
            if attempt["stop"]:
                kill_tree(proc)
                break
            if not selector.select(timeout=1.0):
                state.save()  # flush progress throttled away by the last event (e.g. a long tool call)
                if proc.poll() is not None:
                    break
                continue
            line = proc.stdout.readline()
            if not line:
                break
            events.write(json.dumps({"attempt": number, **_parse(line)}, ensure_ascii=False) + "\n")
            events.flush()
            event = _parse(line)
            kind = event.get("type")
            if kind == "session":
                attempt["session_id"] = event.get("sessionId")
            elif kind == "tool_call":
                attempt["tool_calls"] += 1
                attempt["current"] = summarize_call(event)
                signature = json.dumps([event.get("tool"), event.get("input")], sort_keys=True)
                recent = (recent + [signature])[-LOOP_REPEAT:]
                if len(recent) == LOOP_REPEAT and len(set(recent)) == 1:
                    attempt["stop"] = "loop"
            elif kind == "tool_result":
                attempt["current"] = None
            elif kind == "status" and event.get("phase") == "step_end":
                attempt["steps"] += 1
                usage = event.get("usage") or {}
                attempt["usage"]["miss"] += usage.get("inputTokens", 0)
                attempt["usage"]["cache_hit"] += usage.get("cacheReadTokens", 0)
                attempt["usage"]["out"] += usage.get("outputTokens", 0)
                if max_steps and attempt["steps"] >= max_steps:
                    attempt["stop"] = "max_steps"
            elif kind == "final":
                attempt["final"] = event.get("text", "")
                attempt["reason"] = (event.get("reason") or {}).get("kind")
            elif kind == "error":
                attempt["final"] = event.get("message", "")
                attempt["reason"] = "error"
            attempt["cost"] = round(cost_usd(tier, attempt["usage"], attempt["started"]), 6)
            state.save()
    finally:
        selector.close()
        if proc.poll() is None:
            kill_tree(proc)
        events.close()
        stderr.close()
    attempt["exit"] = proc.returncode
    attempt["ended"] = now()
    attempt["current"] = None
    attempt["cost"] = round(cost_usd(tier, attempt["usage"], attempt["started"]), 6)
    if not attempt["stop"] and attempt.get("reason") != "completed":
        attempt["stop"] = "worker_error"
    state.save(force=True)
    return attempt


def _parse(line: str) -> dict:
    try:
        value = json.loads(line)
        return value if isinstance(value, dict) else {"type": "raw", "text": line.strip()}
    except json.JSONDecodeError:
        return {"type": "raw", "text": line.strip()}


def run_check(workdir: str, command: str, timeout: int = 900) -> dict:
    """Run the check inside the worker's sandbox: it executes worker-written code (tests,
    conftest.py, Makefile, package scripts), which must never run on the host."""
    done = sandbox_run(["/bin/sh", "-c", command], workdir, timeout=timeout)
    output = "check timed out" if done.returncode == 124 else (done.stdout + done.stderr)
    return {"command": command, "exit": done.returncode, "passed": done.returncode == 0,
            "sandboxed": True, "tail": "\n".join(output.strip().splitlines()[-25:])}


def looks_like_question(text: str) -> bool:
    return text.strip().rstrip("`*_ ").endswith("?")


# ----------------------------------------------------------------------------- commands

def preflight() -> list[str]:
    """Local, free checks: dsh runs, the secret guard is mounted and fail-closed, key readable."""
    problems = []
    if int(os.environ.get("DSW_DEPTH", "0")) >= MAX_DEPTH:
        problems.append(f"depth guard: this process is already a worker (DSW_DEPTH={os.environ['DSW_DEPTH']})")
    try:
        read_key()
    except Refused as error:
        problems.append(str(error))
    if not DSH:
        problems.append("dsh not found: set `dsh` in ~/.config/dsw/config.toml or put dsh on PATH (see `dsw doctor`)")
    if not NODE_BIN:
        problems.append("no node >= 24.9 found: set `node_bin` in config.toml (see `dsw doctor`)")
    if problems or os.environ.get("DSW_SKIP_GUARD_CHECK") == "1":
        return problems
    env = dict(os.environ, PATH=f"{NODE_BIN}:{os.environ.get('PATH', '')}")
    dump = ""
    for attempt in range(3):  # concurrent dsh startups occasionally print nothing; retry before refusing
        try:
            dump = subprocess.run([DSH, "--profile", "headless", "--dump-config"], capture_output=True, text=True,
                                  env=env, timeout=60).stdout
        except (OSError, subprocess.TimeoutExpired) as error:
            return problems + [f"dsh not runnable: {error}"]
        if dump.strip():
            break
        time.sleep(1 + attempt)
    if not dump.strip():
        return problems + ["dsh --dump-config printed nothing (run `dsw doctor`)"]
    if "dsw-bwrap" not in dump:
        problems.append("sandbox runner dsw-bwrap not configured in the headless profile (run `dsw setup`)")
    if "name: dsh-secret-guard" not in dump:
        problems.append("dsh-secret-guard is not mounted in the headless profile")
    if not re.search(r"- id: agent-loop\n(?:  .*\n)*?  inject:\n(?:    - .*\n)*?    - secretGuard", dump):
        problems.append("agent-loop does not inject secretGuard (guard would fail open)")
    return problems


def finish(state: JobState, check: str | None, workdir: str) -> int:
    job = state.job
    last = job["attempts"][-1]
    job["facts"] = facts(Path(workdir), job.get("base"))
    if check and "check" not in job and not last["stop"] and last.get("reason") == "completed":
        job["check"] = run_check(workdir, check)
    totals = {"miss": 0, "cache_hit": 0, "out": 0}
    for attempt in job["attempts"]:
        for key in totals:
            totals[key] += attempt["usage"][key]
    job["usage"], job["cost"] = totals, round(sum(a.get("cost", 0) for a in job["attempts"]), 6)
    if last["stop"] and last["stop"] != "worker_error":
        job["state"], code = last["stop"], EXIT_FAILED
    elif last.get("reason") != "completed":
        job["state"], code = "failed", EXIT_FAILED
    elif job.get("check") and not job["check"]["passed"]:
        job["state"], code = "check_failed", EXIT_CHECK_FAILED
    elif not job["facts"].get("changed") and looks_like_question(last["final"]):
        job["state"], code = "needs_input", EXIT_NEEDS_INPUT
    else:
        job["state"], code = "done", EXIT_OK
    job["ended"] = now()
    state.save(force=True)
    metrics = {k: job.get(k) for k in ("id", "repo", "state", "cost", "usage", "escalated", "created", "ended")}
    metrics["tiers"] = [a["tier"] for a in job["attempts"]]
    metrics["efforts"] = [a["effort"] for a in job["attempts"]]
    metrics["steps"] = sum(a["steps"] for a in job["attempts"])
    metrics["tool_calls"] = sum(a["tool_calls"] for a in job["attempts"])
    metrics["peak"] = is_peak(job["attempts"][0]["started"])
    metrics["check_passed"] = (job.get("check") or {}).get("passed")
    with open(HOME / "metrics.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(metrics, ensure_ascii=False) + "\n")
    return code


def execute(state: JobState, prompt: str, args, session_id: str | None) -> int:
    """Wrap the attempts in the job's egress proxy when --net is set."""
    if not getattr(args, "net", False):
        state.job.pop("net", None)
        return _execute(state, prompt, args, session_id)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import dsw_net
    proxy = dsw_net.AllowlistProxy(state.job["workdir"], job_dir(state.job["id"]) / "proxy.log", CFG.net_allow + args.allow_host,
                                   mode=args.net)
    proxy.start()
    state.job["net"] = {"mode": args.net, "hosts": proxy.allowed if args.net == "allowlist" else [], "stats": proxy.stats}
    try:
        return _execute(state, prompt, args, session_id)
    finally:
        proxy.stop()
        state.job["net"]["stats"] = proxy.stats
        state.save(force=True)


def worker_brief(prompt: str, check: str | None) -> str:
    """The brief plus dsw's standing notes: cut repeat test runs, gate workarounds and long deliberation."""
    notes = ["Keep reasoning short: decide and act; do not re-derive what a tool result already shows.",
             "Every step resends the whole context: batch independent reads and edits as parallel tool calls "
             "in one message instead of one call per step."]
    if check:
        notes.append(f"dsw runs `{check}` after you finish. Run only the tests for the code you changed; "
                     "do not run the full suite yourself.")
    notes.append("A tool error starting with [SECURITY GATE] is intentional: do not retry it another way "
                 "(other tool, path, script or encoding). Skip that step and name it in your final answer.")
    return prompt.rstrip() + "\n\n## dsw notes\n" + "\n".join(f"- {note}" for note in notes) + "\n"


def _execute(state: JobState, prompt: str, args, session_id: str | None) -> int:
    """Run one attempt, then optionally escalate flash -> pro on the same session and worktree."""
    job = state.job
    deadline = now() + args.timeout if args.timeout else None
    attempt = run_attempt(state, worker_brief(prompt, args.check), args.tier, args.effort or DEFAULT_EFFORT[args.tier], session_id,
                          args.max_steps or DEFAULT_MAX_STEPS[args.tier], deadline)
    failed = attempt["stop"] in ("loop", "max_steps", "worker_error") or attempt.get("reason") != "completed"
    check = None
    if not failed and args.check:
        check = run_check(job["workdir"], args.check)
        failed = not check["passed"]
    escalate = failed and args.escalate and args.tier == "flash" and attempt["stop"] not in ("cancelled", "timeout")
    if check and not escalate:
        job["check"] = check  # final; finish() re-runs the check only after an escalation
    if escalate:
        job["escalated"] = True
        reason = f"stopped: {attempt['stop']}" if attempt["stop"] else "did not complete"
        if check:
            reason = f"check `{check['command']}` failed (exit {check['exit']}):\n{check['tail']}"
        follow_up = (f"Your previous attempt on this task failed — {reason}\n"
                     "The worktree still holds your earlier edits. Fix the problem so the task is fully done.")
        run_attempt(state, follow_up, "pro", args.effort or DEFAULT_EFFORT["pro"],
                    attempt.get("session_id"), args.max_steps or DEFAULT_MAX_STEPS["pro"], deadline)
    return finish(state, args.check, job["workdir"])


def task_head(prompt: str) -> str:
    """First meaningful brief line: skip 'Repository:'/'Repo:' headers and a 'Task:' label."""
    for line in prompt.strip().splitlines():
        line = line.strip()
        if not line or re.match(r"(?i)(repository|repo|workdir)\s*:", line):
            continue
        return re.sub(r"(?i)^task\s*:\s*", "", line)[:120]
    return ""


def new_job(args, prompt: str) -> JobState:
    repo = Path(args.repo).resolve()
    job_id = args.id or dt.datetime.now().strftime("%m%d-%H%M%S") + "-" + os.urandom(2).hex()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", job_id) or job_id in (".", ".."):
        raise Refused(f"invalid job id {job_id!r}")
    if (job_dir(job_id) / "job.json").exists():
        raise Refused(f"job {job_id} already exists; use `dsw resume {job_id}`")
    workdir, branch = (ensure_worktree(repo, job_id) if args.worktree else (repo, None))
    base = git(workdir, "rev-parse", "HEAD").stdout.strip() or None
    job_dir(job_id).mkdir(parents=True, exist_ok=True)
    (job_dir(job_id) / "task.txt").write_text(prompt, encoding="utf-8")
    job = {"id": job_id, "repo": str(repo), "workdir": str(workdir), "branch": branch, "base": base,
           "created": now(), "state": "running", "runner_pid": os.getpid(), "depth": int(os.environ.get("DSW_DEPTH", "0")),
           "task_head": task_head(prompt), "check_cmd": args.check,
           "attempts": [], "escalated": False}
    return JobState(job)


def cmd_run(args) -> int:
    prompt = Path(args.task_file).read_text(encoding="utf-8") if args.task_file else args.task
    if not prompt or not prompt.strip():
        print("dsw: empty task", file=sys.stderr)
        return EXIT_REFUSED
    try:
        problems = preflight()
        if problems:
            raise Refused("; ".join(problems))
        state = new_job(args, prompt)
        acquire_lock(Path(state.job["workdir"]), state.job["id"])
    except Refused as error:
        print(f"dsw: refused: {error}", file=sys.stderr)
        return EXIT_REFUSED
    state.save(force=True)
    print(f"dsw job {state.job['id']} started in {state.job['workdir']}", flush=True)
    return _supervised(state, prompt, args, None)


def cmd_resume(args) -> int:
    job = load_job(args.id)
    if effective_state(job) == "running":
        print(f"dsw: job {args.id} is still running", file=sys.stderr)
        return EXIT_REFUSED
    session_id = next((a.get("session_id") for a in reversed(job["attempts"]) if a.get("session_id")), None)
    prompt = Path(args.task_file).read_text(encoding="utf-8") if args.task_file else args.task
    if not prompt:
        print("dsw: resume needs --task or --task-file", file=sys.stderr)
        return EXIT_REFUSED
    try:
        problems = preflight()
        if problems:
            raise Refused("; ".join(problems))
        acquire_lock(Path(job["workdir"]), job["id"])
    except Refused as error:
        print(f"dsw: refused: {error}", file=sys.stderr)
        return EXIT_REFUSED
    job.update(state="running", runner_pid=os.getpid(), cancel_requested=False)
    job.pop("check", None)
    args.check = args.check or job.get("check_cmd")
    state = JobState(job)
    state.save(force=True)
    print(f"dsw job {job['id']} resumed (session {session_id})", flush=True)
    return _supervised(state, prompt, args, session_id)


def _supervised(state: JobState, prompt: str, args, session_id: str | None) -> int:
    def on_term(signum, frame):  # cancel from `dsw cancel` or the harness
        state.job["cancel_requested"] = True
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    try:
        code = execute(state, prompt, args, session_id)
    except Exception as error:  # record, never leave a job "running"
        state.job.update(state="failed", error=f"{type(error).__name__}: {error}", ended=now())
        state.save(force=True)
        code = EXIT_FAILED
    finally:
        release_lock(Path(state.job["workdir"]), state.job["id"])
    print(render_result(state.job))
    return code


def cmd_cancel(args) -> int:
    job = load_job(args.id)
    if effective_state(job) != "running":
        print(f"dsw: job {args.id} is {effective_state(job)}, nothing to cancel")
        return 0
    os.kill(job["runner_pid"], signal.SIGTERM)
    for _ in range(40):
        time.sleep(0.5)
        if effective_state(load_job(args.id)) != "running":
            break
    print(f"dsw: job {args.id} -> {effective_state(load_job(args.id))}")
    return 0


def fmt_tokens(value: int) -> str:
    return f"{value / 1000:.1f}k" if value >= 1000 else str(value)


def elapsed(job: dict) -> str:
    seconds = int((job.get("ended") or now()) - job.get("created", now()))
    return f"{seconds // 60}m{seconds % 60:02d}s"


def board_row(job: dict) -> str:
    last = job["attempts"][-1] if job.get("attempts") else {}
    usage = {k: sum(a["usage"][k] for a in job.get("attempts", [])) for k in ("miss", "cache_hit", "out")}
    cost = sum(a.get("cost", 0) for a in job.get("attempts", []))
    tiers = "→".join(a["tier"] for a in job.get("attempts", [])) or "-"
    doing = last.get("current") or ""
    return (f"{job['id']:<20} {effective_state(job):<12} {tiers:<10} {elapsed(job):>7} "
            f"step {last.get('steps', 0):>3} tools {sum(a['tool_calls'] for a in job.get('attempts', [])):>3} "
            f"in {fmt_tokens(usage['miss'])}+{fmt_tokens(usage['cache_hit'])}c out {fmt_tokens(usage['out'])} "
            f"${cost:.4f}  {doing}")


def cmd_status(args) -> int:
    jobs = [load_job(args.id)] if args.id else all_jobs()
    if not args.all and not args.id:
        cutoff = now() - 86400
        jobs = [j for j in jobs if effective_state(j) == "running" or j.get("created", 0) > cutoff]
    if args.json:
        print(json.dumps([dict(j, state=effective_state(j)) for j in jobs], ensure_ascii=False, indent=1))
        return 0
    if not jobs:
        print("dsw: no jobs")
    for job in jobs:
        print(board_row(job))
    return 0


def render_result(job: dict) -> str:
    """Fact-first: observed changes and check output before the worker's own claim."""
    out = [f"job {job['id']}  state={effective_state(job)}  cost=${job.get('cost', 0):.4f}  elapsed={elapsed(job)}"
           + ("  escalated" if job.get("escalated") else "")]
    out.append(f"workdir {job['workdir']}" + (f"  branch {job['branch']}" if job.get("branch") else ""))
    net = (job.get("net") or {}).get("stats")
    mode = (job.get("net") or {}).get("mode", "allowlist")
    out.append("net: none (tools offline)" if not net else
               f"net: {mode} proxy, {net['allowed']} allowed, {net['denied']} denied"
               + (f" ({', '.join(net['denied_hosts'][-5:])})" if net["denied_hosts"] else ""))
    f = job.get("facts") or {}
    if f.get("git"):
        out.append(f"diff: {f['diffstat']}")
        out += [f"  {path}" for path in f.get("changed", [])[:30]]
    check = job.get("check")
    if check:
        out.append(f"check `{check['command']}` exit {check['exit']} -> {'PASS' if check['passed'] else 'FAIL'}")
        out += ["  " + line for line in check["tail"].splitlines()[-10:]]
    for a in job.get("attempts", []):
        out.append(f"attempt {a['n']}: {a['tier']}/{a['effort']} steps {a['steps']} tools {a['tool_calls']} "
                   f"stop={a.get('stop') or a.get('reason')} miss {a['usage']['miss']} hit {a['usage']['cache_hit']} "
                   f"out {a['usage']['out']} ${a.get('cost', 0):.4f}")
    final = (job["attempts"][-1].get("final") if job.get("attempts") else "") or job.get("error", "")
    out.append("worker claim (unverified):")
    out += ["  " + line for line in final.strip().splitlines()[:40]]
    return "\n".join(out)


def cmd_result(args) -> int:
    job = load_job(args.id)
    print(json.dumps(job, ensure_ascii=False, indent=1) if args.json else render_result(job))
    return 0


def tail_lines(job_id: str, n: int = 30) -> list[str]:
    path = job_dir(job_id) / "events.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()[-n:] if path.is_file() else []
    out = []
    for line in lines:
        event = _parse(line)
        kind = event.get("type")
        if kind == "tool_call":
            out.append(f"→ {summarize_call(event)}")
        elif kind == "tool_result":
            out.append(f"← {event.get('status')} {str(event.get('result'))[:120]!r}")
        elif kind == "text":
            out.append(f"  {event.get('text', '')[:160]!r}")
        elif kind in ("final", "error"):
            out.append(f"■ {kind}: {(event.get('text') or event.get('message') or '')[:200]!r}")
    return out


def cmd_tail(args) -> int:
    print("\n".join(tail_lines(args.id, args.n)))
    return 0


def cmd_stats(args) -> int:
    path = HOME / "metrics.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.is_file() else []
    cutoff = now() - args.days * 86400
    latest = {row["id"]: row for row in rows}  # a resumed job appends again; its last row has all attempts
    rows = [r for r in latest.values() if (r.get("created") or 0) >= cutoff]
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault("→".join(row.get("tiers", [])), []).append(row)
    print(f"last {args.days} day(s): {len(rows)} jobs, ${sum(r.get('cost') or 0 for r in rows):.4f}")
    for tiers, items in sorted(groups.items()):
        done = sum(1 for r in items if r["state"] == "done")
        checked = [r for r in items if r.get("check_passed") is not None]
        hit = sum(r["usage"]["cache_hit"] for r in items)
        total_in = hit + sum(r["usage"]["miss"] for r in items)
        cache = f"cache hit {hit / total_in:.0%}" if total_in else "cache hit -"
        print(f"  {tiers:<12} jobs {len(items):>3}  done {done:>3}  "
              f"check pass {sum(1 for r in checked if r['check_passed'])}/{len(checked)}  "
              f"avg ${sum(r['cost'] or 0 for r in items) / len(items):.4f}  "
              f"avg steps {sum(r['steps'] for r in items) / len(items):.1f}  {cache}")
    peak = sum(1 for r in rows if r.get("peak"))
    print(f"  peak-hour jobs: {peak}/{len(rows)} (peak costs 2x; 08-11h and 13-17h ICT, Mon-Fri)")
    if not args.offline:
        try:
            print(f"  balance: {balance()}")
        except Exception as error:  # balance is informative only
            print(f"  balance: unavailable ({type(error).__name__})")
    return 0


def cmd_statusline(args) -> int:
    jobs = all_jobs()
    running = [j for j in jobs if effective_state(j) == "running"]
    today = dt.datetime.now().strftime("%Y-%m-%d")
    todays = [j for j in jobs if dt.datetime.fromtimestamp(j.get("created", 0)).strftime("%Y-%m-%d") == today]
    ok = sum(1 for j in todays if j.get("state") == "done")
    bad = sum(1 for j in todays if j.get("state") not in ("done", "running"))
    cost = sum(a.get("cost", 0) for j in todays for a in j.get("attempts", []))
    parts = [f"⚙dsw {len(running)}▶"]
    if running:
        oldest = min(running, key=lambda j: j["created"])
        tiers = ",".join(sorted({j["attempts"][-1]["tier"] for j in running if j.get("attempts")}))
        parts.append(f"{tiers} {elapsed(oldest)}")
    parts.append(f"✓{ok} ✗{bad} ${cost:.3f}")
    print(" ".join(parts))
    return 0


def cmd_preflight(args) -> int:
    problems = preflight()
    for problem in problems:
        print(f"✗ {problem}")
    if not problems:
        print("✓ dsh runs; secret guard mounted and fail-closed; key readable")
    return EXIT_REFUSED if problems else 0


def cmd_clean(args) -> int:
    job = load_job(args.id)
    if effective_state(job) == "running":
        print(f"dsw: job {args.id} is running; cancel it first", file=sys.stderr)
        return EXIT_REFUSED
    if not job.get("branch"):
        print("dsw: job ran in the repo itself; nothing to clean")
        return 0
    worktree = Path(job["workdir"])
    if worktree.is_dir():
        # The worker may have replaced .git with a hostile repo: only remove what is still
        # the worktree dsw created, and inspect it from inside the sandbox.
        common = git(Path(job["repo"]), "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
        expected = Path(common) / "worktrees" / worktree.name
        pointer = worktree / ".git"
        actual = pointer.read_text(encoding="utf-8").strip() if pointer.is_file() and not pointer.is_symlink() else ""
        if not common or actual != f"gitdir: {expected}":
            print(f"dsw: {worktree}/.git no longer points at {expected}; not touching it (inspect manually)",
                  file=sys.stderr)
            return EXIT_REFUSED
        dirty = sandbox_git(worktree, "status", "--porcelain").stdout.strip()
        if dirty and not args.force:
            print(f"dsw: {worktree} has uncommitted changes; review/commit them or pass --force", file=sys.stderr)
            return EXIT_REFUSED
        removed = git(Path(job["repo"]), "worktree", "remove", *(["--force"] if args.force else []), str(worktree))
        if removed.returncode:
            print(f"dsw: {removed.stderr.strip()}", file=sys.stderr)
            return EXIT_REFUSED
    print(f"dsw: removed worktree {worktree}; branch {job['branch']} kept")
    return 0


def cmd_serve(args) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.modules.setdefault("dsw", sys.modules[__name__])  # dsw_web uses this module's state and helpers
    import dsw_web
    return dsw_web.serve(args.port)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dsw", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    def worker_opts(p):
        p.add_argument("--tier", choices=list(MODELS), default=CFG.defaults["tier"])
        p.add_argument("--effort", choices=["off", "low", "high", "max"])
        p.add_argument("--check", help="shell command run in the workdir after the worker; exit 0 = pass")
        p.add_argument("--escalate", action="store_true", help="on failure, retry once on pro in the same session")
        p.add_argument("--max-steps", type=int, help="step cap; default per tier (config max_steps / max_steps_pro)")
        p.add_argument("--timeout", type=int, default=int(CFG.defaults["timeout"]), help="wall-clock seconds for the whole job")
        p.add_argument("--task")
        p.add_argument("--task-file")
        p.add_argument("--net", nargs="?", const="allowlist", choices=["allowlist", "public"], default=None,
                       help="HTTPS egress for tools via a per-job proxy: `allowlist` (default: pypi, npm, github, ...) "
                            "or `public` (any public host); private/VPN/cluster routes always blocked; omit: no network")
        p.add_argument("--allow-host", action="append", default=[], help="extra allowlisted host for --net (repeatable)")

    run = sub.add_parser("run", help="run one worker (start it with run_in_background)")
    run.add_argument("--repo", default=".")
    run.add_argument("--worktree", action="store_true", help="isolate in <repo>/.worktrees/dsw-<id>")
    run.add_argument("--id")
    worker_opts(run)
    run.set_defaults(func=cmd_run)

    resume = sub.add_parser("resume", help="send a follow-up to a finished job's session")
    resume.add_argument("id")
    worker_opts(resume)
    resume.set_defaults(func=cmd_resume)

    status = sub.add_parser("status", help="jobs board (last 24h unless --all)")
    status.add_argument("id", nargs="?")
    status.add_argument("--all", action="store_true")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    result = sub.add_parser("result", help="fact-first result of a job")
    result.add_argument("id")
    result.add_argument("--json", action="store_true")
    result.set_defaults(func=cmd_result)

    tail = sub.add_parser("tail", help="recent worker events")
    tail.add_argument("id")
    tail.add_argument("-n", type=int, default=30)
    tail.set_defaults(func=cmd_tail)

    cancel = sub.add_parser("cancel", help="stop a running job and its whole process tree")
    cancel.add_argument("id")
    cancel.set_defaults(func=cmd_cancel)

    stats = sub.add_parser("stats", help="cost, pass rate, cache hit by tier; DeepSeek balance")
    stats.add_argument("--days", type=int, default=7)
    stats.add_argument("--offline", action="store_true")
    stats.set_defaults(func=cmd_stats)

    sub.add_parser("statusline", help="one-line segment for a statusline").set_defaults(func=cmd_statusline)

    serve = sub.add_parser("serve", help="read-only local web dashboard (127.0.0.1)")
    serve.add_argument("--port", type=int, default=CFG.serve_port)
    serve.set_defaults(func=cmd_serve)
    sub.add_parser("preflight", help="check dsh, secret guard, key").set_defaults(func=cmd_preflight)

    setup = sub.add_parser("setup", help="wire dsh profiles: link dsh-secret-guard, write the managed patch block")
    setup.add_argument("--profile", action="append", help="dsh profile (repeatable; default: config `profiles`)")
    setup.add_argument("--write-config", action="store_true", help="create config.toml from discovered values")
    setup.add_argument("--dry-run", action="store_true")
    setup.set_defaults(func=lambda a: __import__("dsw_setup").cmd_setup(a))
    sub.add_parser("doctor", help="show resolved config and where each value came from; check the machine") \
        .set_defaults(func=lambda a: __import__("dsw_setup").cmd_doctor(a))

    clean = sub.add_parser("clean", help="remove a job's worktree (branch kept)")
    clean.add_argument("id")
    clean.add_argument("--force", action="store_true")
    clean.set_defaults(func=cmd_clean)

    args = parser.parse_args(argv)
    HOME.mkdir(parents=True, exist_ok=True)
    os.chmod(HOME, 0o700)  # job state holds tasks, tool output and paths: owner only
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
