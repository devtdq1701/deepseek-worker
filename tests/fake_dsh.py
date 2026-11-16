#!/usr/bin/env python3
"""Fake `dsh` for offline dsw tests: emits canned headless JSONL per FAKE_SCENARIO."""
import json
import os
import subprocess
import sys
import time

args = sys.argv[1:]
if "--dump-config" in args:
    if os.environ.get("FAKE_NO_GUARD") == "1":
        print("- id: agent-loop\n  name: '@deepseek-ai/dsh-agent-loop'\n  config:\n    agents: []")
    else:
        print("- id: agent-loop\n  name: '@deepseek-ai/dsh-agent-loop'\n  config:\n    agents: []\n"
              "  inject:\n    - secretGuard\n- id: secret-guard\n  name: dsh-secret-guard\n"
              "- id: sandbox\n  config:\n    runnerCommand:\n      - /x/sandbox/dsw-bwrap")
    sys.exit(0)

overlay = args[args.index("--patch") + 1] if "--patch" in args else None
model = next((line.split(":", 1)[1].strip() for line in open(overlay) if "model:" in line), "?") if overlay else "?"
resumed = "--session-id" in args
session = args[args.index("--session-id") + 1] if resumed else f"session-fake-{os.getpid()}"
log = os.environ.get("FAKE_LOG")
if log:
    with open(log, "a") as handle:
        handle.write(json.dumps({"model": model, "resumed": resumed, "depth": os.environ.get("DSW_DEPTH"),
                                 "key": bool(os.environ.get("DEEPSEEK_API_KEY")), "prompt": args[-1],
                                 "env_keys": sorted(os.environ)}) + "\n")


def emit(**event):
    print(json.dumps(event), flush=True)


def step(n, usage=(1000, 5000, 100)):
    emit(type="status", phase="step_start", turn=1, step=n)
    emit(type="status", phase="step_end", turn=1, step=n,
         usage={"inputTokens": usage[0], "cacheReadTokens": usage[1], "outputTokens": usage[2]})


def final(text, kind="completed"):
    emit(type="final", sessionId=session, reason={"kind": kind}, text=text)


scenario = os.environ.get("FAKE_SCENARIO", "ok")
if args[-1].startswith("[dsw handoff]"):  # the step-cap handoff: one text step, or a tool call if asked
    step(1, (10, 900, 50))
    if os.environ.get("FAKE_HANDOFF_TOOL") == "1":
        emit(type="tool_call", callId="h1", tool="write", input={"path": "x"})
        step(2)
    final("Done: parsed config.\nLeft: wire the CLI flag.\nStuck: none.")
    sys.exit(0)
emit(type="session", sessionId=session, cwd=os.getcwd())
if scenario == "ok":
    emit(type="tool_call", callId="c1", tool="write", input={"file_path": "done.txt"})
    open("done.txt", "w").write("ok\n")
    emit(type="tool_result", callId="c1", status="completed", result="written")
    step(1)
    final("Done: wrote done.txt")
elif scenario == "escalate":  # flash writes a wrong answer; the resumed (pro) session fixes it
    open("done.txt", "w").write("good\n" if resumed else "bad\n")
    step(1)
    final("fixed" if resumed else "I think it is done")
elif scenario == "loop":
    for n in range(1, 10):
        emit(type="tool_call", callId=f"c{n}", tool="bash", input={"command": "ls"})
        emit(type="tool_result", callId=f"c{n}", status="completed", result="x")
        step(n)
    final("never reached")
elif scenario == "steps":
    for n in range(1, 200):
        step(n, (10, 10, 1))
    final("never reached")
elif scenario == "question":
    step(1)
    final("Which file should I edit?")
elif scenario == "crash":
    sys.exit(1)
elif scenario == "slow":  # a tool child in its own session, like dsh's bash tool
    child = subprocess.Popen(["sleep", "300"], start_new_session=True)
    if log:
        with open(log, "a") as handle:
            handle.write(json.dumps({"child": child.pid}) + "\n")
    emit(type="tool_call", callId="c1", tool="bash", input={"command": "sleep 300"})
    time.sleep(300)
    final("never reached")
