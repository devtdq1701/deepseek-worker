---
name: deepseek-worker
description: Dispatch cheap DeepSeek workers (dsh headless, Flash/Pro) that run their own tool loop while Claude plans and reviews. Use when the user asks to hand work to DeepSeek / dsh / cheap workers, or for routine coding, bulk edits, test-writing or exploration with a mechanical check, especially several independent pieces in parallel. Do not use for work whose output cannot be checked mechanically, steps that touch secrets, remote hosts or production, or tasks smaller than writing the brief.
---

# DeepSeek worker (`dsw`)

Claude is the brain: split the work, write each brief, review results. Each `dsw run` is one DeepSeek worker with its own tool loop (dsh headless) in its own git worktree.

Before the first dispatch in a session, run `dsw preflight`. On a new machine, or after changing config or upgrading dsh, run `dsw doctor` (resolved config plus checks), then `dsw setup` (wires the dsh profiles). Healthy output: `✓ dsh runs; secret guard mounted and fail-closed; key readable`. Any `✗` line means stop and fix it; never dispatch without the guard.

## Dispatch

1. Write a self-contained brief to a temp file. The worker has no conversation context, so include:
   - the goal and acceptance criteria;
   - the files to touch and the files not to touch (e.g. `tests/`);
   - the commands to run;
   - "do not commit".
2. Start the worker with Bash `run_in_background: true`. The harness notifies when it ends; do not poll.
   ```bash
   dsw run --repo <repo> --worktree --task-file <brief> --check "<test cmd>" [--tier pro] [--escalate]
   ```
   - `--worktree` is required for parallel workers. A second writer on the same workdir is refused, not queued.
   - `--check` is required for code tasks: exit 0 = pass. Without it the only evidence is the worker's claim.
   - `--escalate` makes a flash failure (check fail, loop, step cap, error) resume the same session once on Pro.
   - Defaults: `--tier flash`, `--effort high`, `--max-steps 60`, `--timeout 1800`. Use Pro for multi-file changes or debugging.
   - `--net` / `--net public`: tools reach the internet only through a per-job HTTPS proxy.
     - `--net` (allowlist): package registries and code hosts only. Use it for installs and clones.
     - `--net public`: any public HTTPS host. Use it when the worker must call public APIs or download from sites outside the allowlist.
     - Allowed by default: pypi, npm/yarn, github, go proxy, crates, maven.
     - Add a host with `--allow-host <host>`, or permanently in `~/.config/dsw/net-allow.txt`.
     - Always blocked, in both modes: private IPs, anything covered by a VPN, cluster or office route, and non-HTTPS.
     - Without `--net`, tools have no network. `web_search` and `web_fetch` still work; `web_fetch` is denied for internal or VPN hosts.
3. Watch only when needed:
   - `dsw status` shows the jobs board.
   - `dsw tail <id>` shows the recent tool calls.
   - `dsw statusline` prints a one-line summary.
   - `dsw serve --port 8791` runs a read-only dashboard on 127.0.0.1 (jobs, results, events, balance) for the user to watch.
4. Native subagent alternative: the `dsw-flash` and `dsw-pro` agents (`claude/agents/` in the repo, linked into `~/.claude/agents/`) run Haiku and only call `dsw run --worktree` and relay the result. The job then shows in the task panel. They cap a job at 540 s, under the 10-minute Bash limit; use the direct background `dsw run` for longer jobs.

## Review

- `dsw result <id>` is fact-first: diff stat, changed files and check output, then the "worker claim (unverified)". Trust the facts, not the claim.
- Read the diff in the worktree (`branch dsw/<id>`) and rerun the check yourself before telling the user it is done.
- To merge, cherry-pick or merge `dsw/<id>` only when the user asks. Workers never commit.
- Follow-up in the same session: `dsw resume <id> --task "<fix this>"`. Stop: `dsw cancel <id>` kills the whole process tree. Cleanup: `dsw clean <id>` removes the worktree, refuses while it is dirty, and keeps the branch. `--force` discards uncommitted work, so run it only after the user's explicit yes.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | done (check passed) |
| 1 | refused before start |
| 2 | failed, cancelled, timed out, loop or step cap |
| 3 | check failed |
| 4 | needs input (the worker ended with a question and changed nothing) |

## Cost

- `dsw stats` shows cost, pass rate and cache hit per tier, plus the DeepSeek balance.
- Peak hours cost 2x: 08–11h and 13–17h ICT, Mon–Fri. Queue non-urgent batches outside them.
- A small coding task on Flash costs about $0.003.

## Secrets: what is enforced

- **Redaction:** dsh-secret-guard redacts known secret formats in tool output and denies what its gate denies (bundled `gate/secret_gate.py`, or your own via `gate` in config).
- **Taint:** a file whose content held a redacted secret cannot be touched again. This blocks printing slices of a token from a file already seen redacted.
- **Home default-deny:** worker tools see an empty `$HOME`, plus the workspace and read-only toolchains. File tools are confined the same way: no reads outside the workspace or toolchains, and no writes outside the workspace.
- **Checks and git run sandboxed:** `--check`, `dsw result` facts and `dsw clean` run inside the sandbox, so worker-written tests, hooks or git config cannot touch the host.
- **Sandbox:** `sandbox/dsw-bwrap` hides the same paths from bash tools and gives them an empty network namespace. The only way out is the `--net` allowlist proxy.
- **web_fetch:** the plugin denies URLs that resolve to private, VPN, cluster or office-routed addresses.
- **Env:** dsw gives workers and checks an allowlisted env only.
- **Still not covered:**
  - A secret of an unknown format, or one copied and transformed inside the workspace, is not redacted.
  - With `--net`, allowlisted hosts (e.g. github) can carry data out; `--net public` reaches any public host.
  - Code and data go to DeepSeek's servers in China.

Install, configuration and threat model: the deepseek-worker README.
