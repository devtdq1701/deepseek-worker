# deepseek-worker (`dsw`)

A dispatcher that lets Claude Code drive DeepSeek workers through `dsh --profile headless --json`. Each run is one worker with its own tool loop. dsw adds the control and visibility that dsh-crew offers, without a long-running hub. The Claude skill that uses it is `~/.claude/skills/deepseek-worker/SKILL.md`.

## Where each mechanism comes from

| Mechanism | Source |
|---|---|
| Worktree `<repo>/.worktrees/dsw-<id>` on branch `dsw/<id>`, `.git/info/exclude` entry, usage totals, exit-code contract | `agy-review-loop-core/scripts/agy_round.py` |
| Jobs board, live progress (step, current tool, tokens, cost) | dsh-crew jobs board |
| Tier flash/pro and effort, set by a per-job `--patch` overlay on `agent-default-model` | dsh-crew tier policy |
| Escalate flash→pro on failure, resuming the same session in the same worktree | dsh-crew `escalate_on_failure` |
| One writer per workspace; a second is refused with the holder's info | dsh-crew workspace lock |
| Depth guard (`DSW_DEPTH`) | dsh-crew origin guard |
| Orphan detection (runner pid dead → `orphaned`) | dsh-crew orphan ghost |
| Statusline segment | dsh-crew HUD |
| Process-tree kill on cancel or timeout, step cap, repeated-call breaker, `--check`, peak-aware cost, balance, preflight, fact-first result | added here |

## Probed dsh facts (2026-11-16, dsh 0.2.2-rc.3)

- 3 concurrent headless runs share `~/.dsh` without conflict.
- `--patch` is a launcher flag and must come before `--profile`. Per-job model and effort take effect, and the session log records `deepseek-v4-pro` with `reasoningEffort: low`.
- `--session-id` resumes after a SIGTERM, even with a stale `session.lock`. A resume with a Pro overlay switches the model mid-session.
- A process-group kill of dsh misses tool children. The bash tool runs under `bwrap` in its own session, so dsw kills the descendant tree it snapshots from `/proc`.
- Headless has no ask-user tool. A worker that needs input ends with a question, which dsw marks `needs_input`.

## Test

```bash
python3 -m unittest tests/test_security.py tests/test_dsw.py tests/test_net.py   # healthy: "Ran 25 tests" ... "OK"
```

The suite runs offline, with `tests/fake_dsh.py` emitting canned JSONL. It covers: ok, worktree + clean, escalation, check failure, loop breaker, step cap, question, crash, cancel (tree kill + lock), timeout, orphan, depth and missing-guard refusal, resume, stats and statusline.

## Live results (2026-11-16, Flash, off-peak)

| Job | Result |
|---|---|
| live-1, live-2 (parallel, separate worktrees) | done in 16–17 s, $0.0026 each; visible and hidden test checks passed |
| live-3 | cancelled while its 90 s bash tool ran; no `sleep` left behind; resume on the same session answered for $0.0001 |
| `dsw stats` | cache hit 91% |

## State

- Jobs: `~/.local/state/dsw/jobs/<id>/` (`job.json`, `events.jsonl`, `stderr.log`, `task.txt`, `overlay-N.yml`).
- Metrics: `~/.local/state/dsw/metrics.jsonl`.
- Locks: `~/.local/state/dsw/locks/`.
- Override paths with `DSW_HOME`, `DSW_DSH`, `DSW_NODE_BIN` and `DSW_KEY_FILE`.

## Sandbox, dashboard, agents (2026-11-16)

- **`sandbox/dsw-bwrap`** is set as `sandbox.runnerCommand` in the `headless` and `web` profiles. dsh calls `runner + <bwrap profile> -- <cmd>`. The wrapper keeps that profile and appends tmpfs/`/dev/null` mounts over credential paths, plus `--unshare-net`. Opt-outs are `DSW_HIDE_EXTRA` and `DSW_ALLOW_NET=1`. Live result: a worker's `curl` got `Could not resolve host`.
- **`dsw serve [--port 8791]`** runs `dsw_web.py`, a read-only dashboard on 127.0.0.1. It shows the jobs board, a fact-first result, the last 40 events and the balance, and refreshes every 3 s.
- **`~/.claude/agents/dsw-flash.md` / `dsw-pro.md`** are Haiku relays that run `dsw run --worktree` so a job shows as a native subagent. They load in a new Claude Code session and were not yet exercised in one.
- **Escalation live (`esc-1`):** a flash attempt failed the check ($0.0014); the Pro resume fixed it ($0.0095); the job is `done` and marked `escalated`.

## Network policy (`--net [allowlist|public]`, 2026-11-16)

- **Default:** tools run in bwrap's empty network namespace. `web_search` (DeepSeek server side) and `web_fetch` (dsh process) still work. dsh-secret-guard denies `web_fetch` to non-public addresses and to anything covered by a specific (non-default) route, which means VPN, cluster and office networks.
- **`--net`:** `dsw_net.AllowlistProxy` serves a unix socket at `/run/user/<uid>/dsw/<sha16(workspace)>.sock` for the job's lifetime, including escalation. `dsw-bwrap` binds the socket into the sandbox, starts `socat` on 127.0.0.1:3128 and sets `HTTPS_PROXY`/`HTTP_PROXY`/npm proxy vars.
- **What the proxy allows:** only `CONNECT host:443` to allowlisted hosts (defaults in `dsw_net.DEFAULT_ALLOW`, extras via `--allow-host` or `~/.config/deepseek-worker/net-allow.txt`). It refuses a host if any address is non-public or covered by a specific route. Unroutable addresses, such as IPv6 on this IPv4-only host, are skipped.
- **Logging:** every decision goes to `jobs/<id>/proxy.log`; counts and denied hosts go into `job.json` → `dsw result` and the dashboard.
- **Live run** of the same task ("venv + pip install six"):
  - `--net`: 2 allowed CONNECTs (pypi.org, files.pythonhosted.org), check PASS, $0.0033.
  - Without it: `check_failed`.
- **`--net public`:** skips the host allowlist but keeps the HTTPS-only and route checks. Live run `net-public`: example.com, wikipedia.org and pypi.org returned 200; 10.0.0.1 was denied.
- **Tests:** `tests/test_net.py` (5 tests).

## Configuration (no hardcoded paths)

Each value is resolved in this order: CLI flag > env `DSW_*` > `~/.config/dsw/config.toml` (or `$DSW_CONFIG`) > auto-discovery > default. `dsw doctor` prints every value with its source and checks the machine.

| Key | Env | Discovery / default |
|---|---|---|
| `dsh` | `DSW_DSH` | `dsh` on PATH, then `~/.local/share/dsw/dsh-runtime` |
| `node_bin` | `DSW_NODE_BIN` | newest node >= 24.9 on PATH / nvm / fnm / volta |
| `key_file` | `DSW_KEY_FILE` | `~/.config/dsw/api_key`; else env `DEEPSEEK_API_KEY` |
| `state_dir` | `DSW_HOME` | `${XDG_STATE_HOME:-~/.local/state}/dsw` |
| `dsh_home` | `DSH_HOME` | `~/.dsh` |
| `profiles` | — | `["headless"]` (add `"web"` for a dsh-crew hub) |
| `secret_guard` | `DSW_SECRET_GUARD` | `plugin/` or `../dsh-secret-guard` next to dsw |
| `gate` | `DSW_GATE` | `~/.claude/hooks/security-gate.py` if present |
| `serve_port` | `DSW_SERVE_PORT` | 8791 |
| `[defaults]` | — | tier flash, effort high/high, max_steps 60, timeout 1800 |
| `[net] allow` | — | extra hosts for `--net` (also `~/.config/dsw/net-allow.txt`) |
| `[sandbox] hide` | — | extra hidden paths (also `~/.config/dsw/hidden-paths.local.txt`) |

- **Hidden paths:** the single list is `sandbox/hidden-paths.txt`. The wrapper reads it, plus the local file and the file `dsw setup` generates. `dsw setup` also passes the merged list to the plugin as `sensitivePaths`.
- **`dsw setup`** links dsh-secret-guard into each profile, then writes a `# >>> dsw managed` block into `cordis.patch.yml` containing:
  - the plugin and its config;
  - the `agent-loop` inject;
  - `sandbox.runnerCommand`.

  It then verifies the result with `--dump-config`. It is idempotent, keeps your own entries, and backs up the file before replacing pre-setup dsw entries. Use `--dry-run` to preview.
- **The proxy socket dir** is `${XDG_RUNTIME_DIR:-/run/user/<uid>}/dsw` (fallback `${TMPDIR:-/tmp}/dsw-<uid>/dsw`), computed the same way in `dsw_config.py` and `sandbox/dsw-bwrap`.

## Security review (2026-11-16) and threat model

The worker (the model plus anything that can prompt-inject it from repo content or fetched pages) is treated as adversarial. Two independent reviews found host-escape paths. All of them are fixed and covered by `tests/test_security.py`, plus the plugin's tests.

| Finding | Fix |
|---|---|
| Host unix sockets (D-Bus, systemd --user, docker.sock, agents) reachable from the sandbox, giving command execution outside it | `--tmpfs /run`, `--new-session`, `--unshare-ipc`; the job's own proxy socket is bound back in |
| Another job's `--net` proxy socket was reachable | same `/run` tmpfs |
| `--check` ran worker-written code (conftest.py, Makefile…) on the host | check runs inside `dsw-bwrap` via `sandbox_run` |
| Host `git status`/`diff` ran hostile repo config (`core.fsmonitor`…) | facts and `clean` use sandboxed git; host git uses `SAFE_GIT` overrides, no global/system config; `clean` verifies the `.git` pointer first |
| Denylist of credential paths missed many secrets | home default-deny: tmpfs over `$HOME`, re-bind the workspace and read-only toolchains (`sandbox/home-allow.txt`); a worktree's common `.git` is bound read-only only when its back-pointer, outside the workspace, names this workspace |
| Plugin path checks bypassable (`~//`, `~/./`, symlinks, `/proc/self/root`, `..`, grep/glob roots, `working_directory`) | async `tools/pre-execute` policy: realpath against the session cwd, home default-deny, no `..`/`~user`/`/proc`/`/sys`/`/dev`, search roots may not contain `$HOME`, `cd`/writes confined to the workspace |
| `web_fetch` SSRF via IPv4-mapped/compatible, NAT64, 6to4 and Teredo IPv6, trailing-dot names, unresolvable hosts | full IPv6 parsing, only `2000::/3` treated as public, fail closed |
| Gate errors made redaction fail open | a bare `systemMessage` counts as gate failure, and the output is withheld |
| Proxy killable by a connection flood (EMFILE ended the accept loop) | 32-tunnel cap; back off on EMFILE |
| Dashboard readable through DNS rebinding | `Host`/`Origin`/`Sec-Fetch-Site` checks, balance cached for 60 s, state dir mode 700 |
| Lock race, `.`/`..` job ids, shared socket dir in `/tmp` | `O_EXCL` lock, ids rejected, private owned dirs enforced |

Not covered (accepted, documented):

- **Redaction is best effort.** A secret inside the workspace in an unknown format, or deliberately transformed (base64 of a copied file), is not caught. Keep live credentials out of worker workspaces.
- **Allowlisted hosts can carry data out.** Under `--net` the worker can push to github, and `--net public` reaches any public host. The code itself already goes to DeepSeek.
- **`web_fetch` DNS rebinding.** Its check and the fetch resolve separately.
- **Hub mode.** The workspace root is the session's first-seen cwd.

## Known limits

- The secret guard redacts known formats only. live-2 showed a slice-the-token bypass; taint in dsh-secret-guard now blocks it (live-2c). Other repos under ~/Workspace stay readable.
- `--max-budget`-style spend caps are not implemented. Use `--max-steps`, `--timeout` and the account balance.
- Resume reuses the stored `--check`. Elapsed time counts from the job's creation.
