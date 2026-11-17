"""Read-only local dashboard for dsw (`dsw serve`): KPIs, 7-day spend, jobs board, job detail with events.
An "agy" view shows agy-pane jobs (sandboxed agy runs) from `agy-pane jobs`, when agy-pane is installed."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

import dsw

JOB_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
_BALANCE: dict = {"at": 0.0, "text": "…"}
_AGY: dict = {"at": 0.0, "body": ""}
_AGY_REF = re.compile(r"job-\d{6}-[A-Za-z0-9]{4}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def agy_tool() -> str | None:
    return os.environ.get("DSW_AGY_PANE") or shutil.which("agy-pane")


def agy_transcript(ref: str) -> tuple[int, str]:
    """`agy-pane transcript <job|conversation>`: agy's thinking, tool calls and results (no prompts)."""
    tool = agy_tool()
    if not tool or not _AGY_REF.fullmatch(ref):
        return 404, "{}"
    try:
        done = subprocess.run([tool, "transcript", ref, "--limit", "150"], capture_output=True, text=True, timeout=10)
        json.loads(done.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 502, "{}"
    return (200 if done.returncode == 0 else 404), done.stdout


def agy_jobs() -> str:
    """`agy-pane jobs` (running jobs with output tails, recent jobs, slots, cached quota), at most every 2s."""
    if dsw.now() - _AGY["at"] < 2 and _AGY["body"]:
        return _AGY["body"]
    tool = agy_tool()
    if not tool:
        body = {"available": False, "error": "agy-pane is not installed (not on PATH; set DSW_AGY_PANE)"}
    else:
        try:
            done = subprocess.run([tool, "jobs", "--tail", "60"], capture_output=True, text=True, timeout=5)
            body = dict(json.loads(done.stdout), available=True)
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            body = {"available": False, "error": f"agy-pane jobs failed ({type(error).__name__})"}
    _AGY.update(at=dsw.now(), body=json.dumps(body, ensure_ascii=False))
    return _AGY["body"]


def cached_balance() -> str:
    """At most one authenticated balance call per minute, whatever the request rate."""
    if dsw.now() - _BALANCE["at"] > 60:
        try:
            _BALANCE["text"] = dsw.balance()
        except Exception as error:  # informative only
            _BALANCE["text"] = f"unavailable ({type(error).__name__})"
        _BALANCE["at"] = dsw.now()
    return _BALANCE["text"]
ICT = dt.timezone(dt.timedelta(hours=7))


def structured_events(job_id: str, limit: int = 120) -> list[dict]:
    path = dsw.job_dir(job_id) / "events.jsonl"
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        e = dsw._parse(line)
        kind, attempt = e.get("type"), e.get("attempt")
        if kind == "tool_call":
            data = e.get("input") or {}
            detail = data.get("command") or data.get("file_path") or data.get("pattern") or json.dumps(data)[:200]
            out.append({"a": attempt, "k": "call", "tool": e.get("tool"), "text": str(detail)[:400]})
        elif kind == "tool_result":
            out.append({"a": attempt, "k": "result", "status": e.get("status"), "text": str(e.get("result"))[:400]})
        elif kind == "text" and (e.get("text") or "").strip():
            out.append({"a": attempt, "k": "text", "text": e["text"][:400]})
        elif kind == "status" and e.get("phase") == "step_end":
            u = e.get("usage") or {}
            out.append({"a": attempt, "k": "step", "step": e.get("step"), "miss": u.get("inputTokens", 0),
                        "hit": u.get("cacheReadTokens", 0), "out": u.get("outputTokens", 0)})
        elif kind in ("final", "error"):
            out.append({"a": attempt, "k": kind, "text": (e.get("text") or e.get("message") or "")[:600]})
    return out[-limit:]


def next_price_switch(start: float) -> dict:
    peak = dsw.is_peak(start)
    t = start - start % 60
    for _ in range(7 * 24 * 60):
        t += 60
        if dsw.is_peak(t) != peak:
            break
    return {"peak": peak, "switch_at": dt.datetime.fromtimestamp(t, ICT).strftime("%a %H:%M")}


def stats() -> dict:
    path = dsw.HOME / "metrics.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.is_file() else []
    latest = list({row["id"]: row for row in rows}.values())
    today = dt.datetime.now(ICT).date()
    days = []
    for back in range(6, -1, -1):
        day = today - dt.timedelta(days=back)
        items = [r for r in latest if dt.datetime.fromtimestamp(r.get("created") or 0, ICT).date() == day]
        days.append({"date": day.strftime("%d/%m"), "cost": round(sum(r.get("cost") or 0 for r in items), 5),
                     "jobs": len(items), "done": sum(1 for r in items if r.get("state") == "done")})
    week = [r for r in latest if (r.get("created") or 0) >= dsw.now() - 7 * 86400]
    checked = [r for r in week if r.get("check_passed") is not None]
    hit = sum(r["usage"]["cache_hit"] for r in week)
    miss = sum(r["usage"]["miss"] for r in week)
    return {
        "days": days,
        "week_cost": round(sum(r.get("cost") or 0 for r in week), 5),
        "week_jobs": len(week),
        "pass": [sum(1 for r in checked if r["check_passed"]), len(checked)],
        "cache_hit": hit / (hit + miss) if hit + miss else None,
        "escalated": sum(1 for r in week if r.get("escalated")),
        "price": next_price_switch(dsw.now()),
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the terminal quiet
        pass

    def send(self, code: int, body: str, kind: str = "application/json") -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", f"{kind}; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def trusted(self, page: bool = False) -> bool:
        """Only same-machine, same-origin requests: a DNS-rebinding page sends a foreign Host.

        `page`: the dashboard HTML holds no data, so a link from another site may open it
        (the browser then sends Sec-Fetch-Site: cross-site); framing is refused in send()."""
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host", "") not in allowed:
            return False
        if page:
            return True
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://{h}" for h in allowed}:
            return False
        return self.headers.get("Sec-Fetch-Site", "same-origin") in ("same-origin", "none")

    def do_GET(self):  # noqa: N802 (http.server API)
        path = self.path.split("?", 1)[0]
        if not self.trusted(page=path == "/"):
            return self.send(403, "{}")
        if path == "/":
            return self.send(200, DASHBOARD_HTML, "text/html")
        if path == "/api/jobs":
            jobs = [dict(j, state=dsw.effective_state(j), elapsed=dsw.elapsed(j)) for j in dsw.all_jobs()]
            return self.send(200, json.dumps(list(reversed(jobs)), ensure_ascii=False))
        if path == "/api/stats":
            return self.send(200, json.dumps(stats(), ensure_ascii=False))
        if path.startswith("/api/job/"):
            job_id = unquote(path[len("/api/job/"):])
            if not JOB_ID.fullmatch(job_id) or not (dsw.job_dir(job_id) / "job.json").is_file():
                return self.send(404, "{}")
            job = dsw.load_job(job_id)
            job.update(state=dsw.effective_state(job), elapsed=dsw.elapsed(job))
            return self.send(200, json.dumps({"job": job, "events": structured_events(job_id)}, ensure_ascii=False))
        if path == "/api/agy":
            return self.send(200, agy_jobs())
        if path.startswith("/api/agy/transcript/"):
            return self.send(*agy_transcript(unquote(path[len("/api/agy/transcript/"):])))
        if path == "/api/balance":
            return self.send(200, cached_balance(), "text/plain")
        return self.send(404, "{}")


def serve(port: int) -> int:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"dsw dashboard: http://127.0.0.1:{port}/  (read-only; Ctrl-C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


DASHBOARD_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>dsw · DeepSeek workers</title>
<style>
:root{--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;--grid:#e1e0d9;--axis:#c3c2b7;
--hover:#f0efe9;--sel:#e9eef8;--selbar:#2a78d6;
--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--good:#0ca30c;--goodtext:#006300;--warn:#fab219;--warntext:#8a5a00;--serious:#ec835a;--crit:#d03b3b;--run:#2a78d6;
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;--sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--page:#0d0d0d;--surface:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;--muted:#898781;--grid:#2c2c2a;--axis:#383835;
--hover:#22221f;--sel:#1d2633;--selbar:#3987e5;--s1:#3987e5;--s2:#d95926;--s3:#199e70;--good:#0ca30c;--goodtext:#0ca30c;--warntext:#fab219;--run:#3987e5}}
:root[data-theme=dark]{--page:#0d0d0d;--surface:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;--muted:#898781;--grid:#2c2c2a;--axis:#383835;
--hover:#22221f;--sel:#1d2633;--selbar:#3987e5;--s1:#3987e5;--s2:#d95926;--s3:#199e70;--good:#0ca30c;--goodtext:#0ca30c;--warntext:#fab219;--run:#3987e5}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);font:13px/1.45 var(--sans);height:100vh;display:flex;flex-direction:column}
.mono{font-family:var(--mono);font-size:12px}
header{display:flex;align-items:center;gap:14px;padding:6px 14px;border-bottom:1px solid var(--grid);background:var(--surface)}
header .brand{font-weight:650;letter-spacing:.2px}header .brand small{color:var(--muted);font-weight:400;margin-left:6px}
.live{display:inline-flex;align-items:center;gap:6px;color:var(--muted);font-size:12px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--muted)}.dot.on{background:var(--good);animation:pulse 1.6s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(12,163,12,.5)}70%{box-shadow:0 0 0 6px rgba(12,163,12,0)}100%{box-shadow:0 0 0 0 rgba(12,163,12,0)}}
.spacer{flex:1}.chip{display:inline-flex;align-items:center;gap:5px;padding:1px 8px;border:1px solid var(--grid);border-radius:999px;font-size:12px;color:var(--ink2);white-space:nowrap}
button.ghost{background:none;border:1px solid var(--grid);border-radius:6px;color:var(--ink2);padding:2px 8px;font:inherit;font-size:12px;cursor:pointer}
button.ghost:hover{background:var(--hover)}
.kpis{display:flex;gap:0;border-bottom:1px solid var(--grid);background:var(--surface);overflow-x:auto}
.kpi{padding:7px 16px;border-right:1px solid var(--grid);min-width:118px}
.kpi .l{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.4px}
.kpi .v{font-size:18px;font-weight:600;font-variant-numeric:tabular-nums;line-height:1.25}
.kpi .s{color:var(--ink2);font-size:11px}
.kpi.chart{flex:1;min-width:260px;display:flex;flex-direction:column}
main{flex:1;min-height:0;display:grid;grid-template-columns:minmax(0,2fr) minmax(360px,1fr)}
@media (max-width:980px){main{grid-template-columns:1fr}#detail,#a-detail{border-left:0;border-top:1px solid var(--grid)}}
#left,#a-left{display:flex;flex-direction:column;min-height:0}
.toolbar{display:flex;gap:6px;align-items:center;padding:6px 10px;border-bottom:1px solid var(--grid)}
.tab{border:0;background:none;color:var(--ink2);padding:3px 9px;border-radius:6px;font:inherit;font-size:12px;cursor:pointer}
.tab.on{background:var(--sel);color:var(--ink);font-weight:600}.tab .n{color:var(--muted);margin-left:3px;font-weight:400}
[hidden]{display:none!important}
.view{flex:1;min-height:0;display:flex;flex-direction:column}
.views{display:inline-flex;gap:2px;border:1px solid var(--grid);border-radius:8px;padding:2px}
.ag-tail h3 .na{text-transform:none;font-weight:400}.ag-tail pre{max-height:320px;border:1px solid var(--grid);border-radius:8px}
.toolbar input{margin-left:auto;background:var(--surface);border:1px solid var(--grid);border-radius:6px;color:var(--ink);padding:3px 8px;font:inherit;font-size:12px;width:190px}
.legend{display:flex;gap:10px;color:var(--muted);font-size:11px;padding:4px 10px;border-bottom:1px solid var(--grid)}
.sw{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:4px;vertical-align:-1px}
#list,#a-list{overflow:auto;flex:1}
table{border-collapse:collapse;width:100%}
th{position:sticky;top:0;background:var(--page);color:var(--muted);font-weight:500;font-size:11px;text-transform:uppercase;letter-spacing:.3px;text-align:left;padding:5px 8px;border-bottom:1px solid var(--grid);z-index:1}
td{padding:5px 7px;border-bottom:1px solid var(--grid);white-space:nowrap;vertical-align:middle}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
tbody tr{cursor:pointer}tbody tr:hover{background:var(--hover)}tbody tr.sel{background:var(--sel);box-shadow:inset 3px 0 0 var(--selbar)}
.task{max-width:200px;overflow:hidden;text-overflow:ellipsis;color:var(--ink2)}
.pill{display:inline-flex;align-items:center;gap:5px;font-size:12px}
.pill i{width:8px;height:8px;border-radius:50%;display:inline-block}
.st-done i{background:var(--good)}.st-running i{background:var(--run);animation:pulse 1.6s infinite}
.st-check_failed i,.st-needs_input i,.st-orphaned i{background:var(--warn)}
.st-failed i,.st-loop i,.st-max_steps i,.st-timeout i{background:var(--crit)}.st-cancelled i{background:var(--muted)}
.tier{display:inline-block;padding:0 6px;border-radius:4px;font-size:11px;border:1px solid var(--grid);color:var(--ink2)}
.tier.pro{border-color:var(--s2);color:var(--s2)}.arrow{color:var(--muted);margin:0 2px}
.prog{display:inline-block;width:40px;height:6px;background:var(--grid);border-radius:3px;vertical-align:middle;overflow:hidden;margin-right:6px}
.prog b{display:block;height:100%;background:var(--s1);border-radius:3px}
.tok{display:flex;gap:2px;width:84px;height:8px}.tok span{display:block;height:100%}
.tok span:first-child{border-radius:3px 0 0 3px}.tok span:last-child{border-radius:0 3px 3px 0}
.ok{color:var(--goodtext)}.bad{color:var(--crit)}.na{color:var(--muted)}
.doing{display:block;color:var(--run);max-width:230px;overflow:hidden;text-overflow:ellipsis;font-family:var(--mono);font-size:11.5px}
#detail,#a-detail{border-left:1px solid var(--grid);overflow:auto;background:var(--surface);padding:12px 16px}
.empty{color:var(--muted);padding:40px 0;text-align:center}
.dh{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.dh h2{margin:0;font-size:16px;font-family:var(--mono)}
.taskfull{color:var(--ink2);margin:6px 0 10px}
.meta{display:grid;grid-template-columns:max-content 1fr;gap:3px 12px;font-size:12px;margin-bottom:6px}
.meta dt{color:var(--muted)}.meta dd{margin:0;font-family:var(--mono);font-size:11.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
section{margin-top:14px}section h3{margin:0 0 6px;font-size:11px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.5px;display:flex;align-items:center;gap:8px}
.files{width:100%}.files td{padding:2px 6px;border:0;font-family:var(--mono);font-size:12px}
.fs{display:inline-block;width:22px;text-align:center;border-radius:3px;font-size:11px}
.fs-M{background:color-mix(in srgb,var(--s1) 18%,transparent);color:var(--s1)}.fs-A,.fs-q{background:color-mix(in srgb,var(--good) 18%,transparent);color:var(--goodtext)}
.fs-D{background:color-mix(in srgb,var(--crit) 18%,transparent);color:var(--crit)}
.add{color:var(--goodtext)}.del{color:var(--crit)}
.check{border:1px solid var(--grid);border-radius:8px;overflow:hidden}
.check .cb{display:flex;gap:8px;align-items:center;padding:6px 10px;border-bottom:1px solid var(--grid);font-size:12px}
.badge{font-weight:700;font-size:11px;padding:1px 7px;border-radius:4px;color:#fff}.badge.pass{background:var(--good)}.badge.fail{background:var(--crit)}
pre{margin:0;padding:8px 10px;font-family:var(--mono);font-size:11.5px;white-space:pre-wrap;word-break:break-word;color:var(--ink2);max-height:220px;overflow:auto}
.att{width:100%}.att th{position:static;background:none}.att td{font-size:12px}
.stop{font-size:11px;padding:0 6px;border-radius:4px;border:1px solid var(--grid)}
.claim{border:1px dashed var(--axis);border-radius:8px}.claim summary{cursor:pointer;padding:6px 10px;color:var(--ink2);font-size:12px}
.tl{list-style:none;margin:0;padding:0;font-size:12px}
.tl li{display:grid;grid-template-columns:18px 1fr;gap:6px;padding:3px 0;border-bottom:1px dotted var(--grid)}
.tl .ic{color:var(--muted);text-align:center}.tl .call .ic{color:var(--s1)}.tl .result .ic{color:var(--goodtext)}.tl .result.err .ic{color:var(--crit)}
.tl .final .ic{color:var(--ink)}.tl .step{color:var(--muted);font-size:11px}
.tl code{font-family:var(--mono);font-size:11.5px;color:var(--ink);word-break:break-all}.tl .t{color:var(--ink2);word-break:break-word}
.att-sep{color:var(--muted);font-size:11px;margin:6px 0 2px;text-transform:uppercase;letter-spacing:.4px}
.cmds{display:flex;gap:6px;flex-wrap:wrap}.cmds button{font-family:var(--mono)}
#tip{position:fixed;pointer-events:none;background:var(--ink);color:var(--surface);padding:4px 8px;border-radius:5px;font-size:11.5px;display:none;z-index:9;white-space:nowrap}
</style></head><body>
<header>
  <span class="brand">dsw<small id="brand-s">DeepSeek workers · read-only</small></span>
  <span class="views"><button class="tab on" data-v="dsw">DeepSeek</button><button class="tab" data-v="agy">agy<span class=n id="agy-n"></span></button></span>
  <span class="live"><span class="dot" id="livedot"></span><span id="livetxt">idle</span></span>
  <span class="spacer"></span>
  <span class="chip dsw-only" id="price"></span><span class="chip dsw-only" id="bal">balance …</span>
  <button class="ghost" id="theme" title="Toggle theme">◐</button>
</header>
<div class="view" id="v-dsw">
<div class="kpis">
  <div class="kpi"><div class="l">Running</div><div class="v" id="k-run">0</div><div class="s" id="k-run-s">&nbsp;</div></div>
  <div class="kpi"><div class="l">Today</div><div class="v" id="k-today">0</div><div class="s" id="k-today-s">&nbsp;</div></div>
  <div class="kpi"><div class="l">Check pass · 7d</div><div class="v" id="k-pass">–</div><div class="s" id="k-pass-s">&nbsp;</div></div>
  <div class="kpi"><div class="l">Spend · 7d</div><div class="v" id="k-spend">$0</div><div class="s" id="k-spend-s">&nbsp;</div></div>
  <div class="kpi"><div class="l">Cache hit · 7d</div><div class="v" id="k-cache">–</div><div class="s">of input tokens</div></div>
  <div class="kpi chart"><div class="l">Daily spend · last 7 days</div><svg id="spend" height="46" width="100%" role="img" aria-label="Daily spend, last 7 days"></svg></div>
</div>
<main>
  <div id="left">
    <div class="toolbar" id="tabs"></div>
    <div class="legend"><span>tokens:</span><span><i class="sw" style="background:var(--s1)"></i>cache hit</span><span><i class="sw" style="background:var(--s2)"></i>miss</span><span><i class="sw" style="background:var(--s3)"></i>output</span><span style="margin-left:auto">click a row for detail · refresh 3s</span></div>
    <div id="list"><table><thead><tr><th>Job</th><th>State</th><th>Task</th><th>Tier</th><th>Steps</th><th class=num>Tools</th><th>Tokens</th><th class=num>Cost</th><th>Check</th><th class=num>Elapsed</th></tr></thead><tbody id="rows"></tbody></table></div>
  </div>
  <div id="detail"><div class="empty">Select a job to see its facts, check, attempts and events.</div></div>
</main>
</div>
<div class="view" id="v-agy" hidden>
<div class="kpis">
  <div class="kpi"><div class="l">Running</div><div class="v" id="a-run">0</div><div class="s" id="a-run-s">&nbsp;</div></div>
  <div class="kpi"><div class="l">Today</div><div class="v" id="a-today">0</div><div class="s" id="a-today-s">&nbsp;</div></div>
  <div class="kpi"><div class="l">agy time · today</div><div class="v" id="a-time">0m</div><div class="s" id="a-time-s">&nbsp;</div></div>
  <div class="kpi"><div class="l">Gemini quota</div><div class="v" id="a-gem">–</div><div class="s" id="a-gem-s">&nbsp;</div></div>
  <div class="kpi"><div class="l">Claude/GPT quota</div><div class="v" id="a-cl">–</div><div class="s" id="a-cl-s">&nbsp;</div></div>
</div>
<main>
  <div id="a-left">
    <div class="toolbar" id="a-tabs"></div>
    <div class="legend"><span>agy-pane jobs: sandboxed agy in tmux <code class=mono>agy-worker</code> · prompts are never logged</span><span style="margin-left:auto">refresh 3s</span></div>
    <div id="a-list"><table><thead><tr><th>Job</th><th>State</th><th>Model</th><th>Cwd</th><th>Started</th><th class=num>Duration</th><th class=num>Exit</th></tr></thead><tbody id="a-rows"></tbody></table></div>
  </div>
  <div id="a-detail"><div class="empty">Select a job. A running job shows its live output.</div></div>
</main>
</div>
<div id="tip"></div>
<script>
const S={jobs:[],sel:null,filter:'all',q:'',stats:null};
const $=id=>document.getElementById(id);
const esc=t=>String(t??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const kfmt=n=>n>=1e6?(n/1e6).toFixed(1)+'M':n>=1000?(n/1000).toFixed(1)+'k':String(n||0);
const usd=(c,d=4)=>'$'+(c||0).toFixed(d);
const FAIL=['failed','loop','max_steps','timeout','cancelled','check_failed','orphaned','needs_input'];
const TABS=[['all','All'],['running','Running'],['done','Done'],['failed','Failed'],['needs','Needs attention']];
function sums(j){const a=j.attempts||[];const u={m:0,h:0,o:0};let c=0,t=0;a.forEach(x=>{u.m+=x.usage.miss;u.h+=x.usage.cache_hit;u.o+=x.usage.out;c+=x.cost||0;t+=x.tool_calls});return{u,c,t,a,last:a[a.length-1]||{}}}
function match(j){const f=S.filter;if(f==='running'&&j.state!=='running')return false;if(f==='done'&&j.state!=='done')return false;
 if(f==='failed'&&!['failed','loop','max_steps','timeout','cancelled','orphaned'].includes(j.state))return false;
 if(f==='needs'&&!['check_failed','needs_input','orphaned'].includes(j.state))return false;
 const q=S.q.toLowerCase();return !q||(j.id+' '+(j.task_head||'')+' '+(j.repo||'')).toLowerCase().includes(q)}
function tierHtml(a){return a.length?a.map(x=>`<span class="tier ${x.tier}">${x.tier}</span>`).join('<span class=arrow>→</span>'):'–'}
function tokHtml(u){const tot=u.h+u.m+u.o||1;const seg=(v,c,l)=>v?`<span style="width:${Math.max(2,v/tot*100)}%;background:var(${c})" data-tip="${l}: ${kfmt(v)}"></span>`:'';
 return `<div class="tok" data-tip="cache ${kfmt(u.h)} · miss ${kfmt(u.m)} · out ${kfmt(u.o)}">${seg(u.h,'--s1','cache hit')}${seg(u.m,'--s2','miss')}${seg(u.o,'--s3','output')}</div>`}
function checkHtml(j){if(!j.check)return j.check_cmd&&j.state==='running'?'<span class=na>…</span>':'<span class=na>–</span>';return j.check.passed?'<span class=ok>✓ pass</span>':'<span class=bad>✗ fail</span>'}
function renderTabs(){const n=k=>S.jobs.filter(j=>{const s=S.filter;S.filter=k;const r=match(j);S.filter=s;return r}).length;
 $('tabs').innerHTML=TABS.map(([k,l])=>`<button class="tab ${S.filter===k?'on':''}" data-f="${k}">${l}<span class=n>${n(k)}</span></button>`).join('')+`<input id=q placeholder="filter id / task / repo" value="${esc(S.q)}">`;
 document.querySelectorAll('#tabs .tab').forEach(b=>b.onclick=()=>{S.filter=b.dataset.f;render()});
 const q=$('q');q.oninput=()=>{S.q=q.value;renderRows()};}
function renderRows(){const rows=S.jobs.filter(match);
 $('rows').innerHTML=rows.length?rows.map(j=>{const s=sums(j);const steps=s.last.steps||0;const max=60;
 return `<tr data-id="${esc(j.id)}" class="${j.id===S.sel?'sel':''}"><td class=mono>${esc(j.id)}</td>
 <td><span class="pill st-${esc(j.state)}"><i></i>${esc(j.state.replace('_',' '))}</span>${j.escalated?' <span class=tier title="escalated">↑pro</span>':''}</td>
 <td class=task title="${esc(j.task_head)}">${esc(j.task_head)}${j.state==='running'?`<span class=doing>▸ ${esc(s.last.current||'thinking…')}</span>`:''}</td><td>${tierHtml(s.a)}</td>
 <td><span class=prog><b style="width:${Math.min(100,steps/max*100)}%"></b></span><span class=mono>${steps}</span></td>
 <td class=num>${s.t}</td><td>${tokHtml(s.u)}</td><td class="num mono">${usd(s.c)}</td><td>${checkHtml(j)}</td>
 <td class="num mono">${esc(j.elapsed)}</td></tr>`}).join('')
 :`<tr><td colspan=10 class=empty>No jobs match.</td></tr>`;
 document.querySelectorAll('#rows tr[data-id]').forEach(tr=>tr.onclick=()=>{S.sel=tr.dataset.id;renderRows();detail()});}
function renderKpis(){const run=S.jobs.filter(j=>j.state==='running');const today=new Date().toDateString();
 const td=S.jobs.filter(j=>new Date(j.created*1000).toDateString()===today);
 $('k-run').textContent=run.length;$('k-run-s').textContent=run.length?run.map(j=>sums(j).last.tier||'').join(', '):'no workers';
 $('k-today').textContent=td.length;const ok=td.filter(j=>j.state==='done').length,bad=td.filter(j=>FAIL.includes(j.state)).length;
 $('k-today-s').innerHTML=`<span class=ok>✓ ${ok}</span> · <span class=bad>✗ ${bad}</span> · ${usd(td.reduce((a,j)=>a+sums(j).c,0),3)}`;
 if(S.view==='dsw'){$('livedot').className='dot'+(run.length?' on':'');$('livetxt').textContent=run.length?`${run.length} running`:'idle';}
 const st=S.stats;if(!st)return;
 $('k-pass').textContent=st.pass[1]?Math.round(st.pass[0]/st.pass[1]*100)+'%':'–';$('k-pass-s').textContent=`${st.pass[0]}/${st.pass[1]} checked · ${st.escalated} escalated`;
 $('k-spend').textContent=usd(st.week_cost,3);$('k-spend-s').textContent=`${st.week_jobs} jobs · avg ${usd(st.week_jobs?st.week_cost/st.week_jobs:0)}`;
 $('k-cache').textContent=st.cache_hit==null?'–':Math.round(st.cache_hit*100)+'%';
 $('price').innerHTML=st.price.peak?`<span class=bad>● peak · 2× price</span> until ${esc(st.price.switch_at)}`:`<span class=ok>● off-peak · ½ price</span> until ${esc(st.price.switch_at)}`;
 drawSpend(st.days)}
function drawSpend(days){const svg=$('spend');const W=svg.clientWidth||300,H=46,pad=14,bw=Math.min(28,(W-pad*2)/days.length-6);
 const max=Math.max(...days.map(d=>d.cost),0.0001);const step=(W-pad*2)/days.length;let h='';
 h+=`<line x1="0" x2="${W}" y1="${H-12.5}" y2="${H-12.5}" stroke="var(--axis)" stroke-width="1"/>`;
 days.forEach((d,i)=>{const x=pad+i*step+(step-bw)/2,bh=d.cost?Math.max(2,(H-16)*d.cost/max):0,y=H-13-bh,r=Math.min(4,bh,bw/2);
  if(bh)h+=`<path d="M${x},${H-13}V${y+r}Q${x},${y} ${x+r},${y}H${x+bw-r}Q${x+bw},${y} ${x+bw},${y+r}V${H-13}Z" fill="var(--s1)"/>`;
  h+=`<rect x="${pad+i*step}" y="0" width="${step}" height="${H}" fill="transparent" data-tip="${d.date}: ${usd(d.cost)} · ${d.jobs} jobs (${d.done} done)"/>`;
  h+=`<text x="${x+bw/2}" y="${H-2}" text-anchor="middle" font-size="9" fill="var(--muted)">${d.date}</text>`});
 svg.innerHTML=h}
async function detail(){if(!S.sel)return;const r=await fetch('api/job/'+encodeURIComponent(S.sel));if(!r.ok)return;const {job:j,events:ev}=await r.json();
 const s=sums(j);const f=j.facts||{};const ts=t=>t?new Date(t*1000).toLocaleString():'–';
 const files=(f.files||(f.changed||[]).map(p=>({status:'M',path:p,added:'',removed:''}))).map(x=>{const st=x.status==='??'?'q':x.status[0];return `<tr><td><span class="fs fs-${esc(st)}">${esc(x.status==='??'?'new':x.status)}</span></td><td>${esc(x.path)}</td><td class=num>${x.added?`<span class=add>+${esc(x.added)}</span> <span class=del>−${esc(x.removed)}</span>`:''}</td></tr>`}).join('');
 const chk=j.check?`<div class=check><div class=cb><span class="badge ${j.check.passed?'pass':'fail'}">${j.check.passed?'PASS':'FAIL'}</span><code class=mono>${esc(j.check.command)}</code><span class=spacer></span><span class=na>exit ${j.check.exit}</span></div><pre>${esc(j.check.tail)}</pre></div>`:`<div class=na>${j.check_cmd?'pending':'no check configured — only the worker\'s claim is evidence'}</div>`;
 const att=(j.attempts||[]).map(a=>`<tr><td class=mono>#${a.n}</td><td><span class="tier ${a.tier}">${a.tier}</span> <span class=na>${esc(a.effort)}</span></td><td class=num>${a.steps}</td><td class=num>${a.tool_calls}</td>
 <td>${tokHtml({h:a.usage.cache_hit,m:a.usage.miss,o:a.usage.out})}</td><td class="num mono">${usd(a.cost)}</td><td><span class=stop>${esc(a.stop||a.reason||'running')}</span></td></tr>`).join('');
 let lastA=null;const tl=ev.map(e=>{let sep='';if(e.a!==lastA){lastA=e.a;sep=`<li class=att-sep style="display:block;border:0">attempt ${e.a}</li>`}
  if(e.k==='call')return sep+`<li class=call><span class=ic>→</span><span><b>${esc(e.tool)}</b> <code>${esc(e.text)}</code></span></li>`;
  if(e.k==='result')return sep+`<li class="result ${e.status!=='completed'?'err':''}"><span class=ic>${e.status==='completed'?'←':'✗'}</span><span class=t>${esc(e.text)}</span></li>`;
  if(e.k==='step')return sep+`<li class=step><span class=ic>·</span><span>step ${e.step} · miss ${kfmt(e.miss)} · cache ${kfmt(e.hit)} · out ${kfmt(e.out)}</span></li>`;
  if(e.k==='text')return sep+`<li><span class=ic>”</span><span class=t>${esc(e.text)}</span></li>`;
  return sep+`<li class=final><span class=ic>■</span><span class=t><b>${esc(e.k)}</b> ${esc(e.text)}</span></li>`}).join('');
 const claim=(s.last.final||j.error||'').trim();
 const dp=$('detail'),dtop=S.shownId===j.id?dp.scrollTop:0;S.shownId=j.id;
 $('detail').innerHTML=`<div class=dh><h2>${esc(j.id)}</h2><span class="pill st-${esc(j.state)}"><i></i>${esc(j.state)}</span>${j.escalated?'<span class="tier pro">escalated</span>':''}<span class=spacer></span><span class="mono">${usd(j.cost??s.c)} · ${esc(j.elapsed)}</span></div>
 <div class=taskfull>${esc(j.task_head)}</div>
 <dl class=meta><dt>repo</dt><dd title="${esc(j.repo)}">${esc(j.repo)}</dd><dt>workdir</dt><dd title="${esc(j.workdir)}">${esc(j.workdir)}</dd>
 <dt>network</dt><dd>${j.net?`${esc(j.net.mode||'allowlist')} proxy · ${j.net.stats.allowed} allowed · ${j.net.stats.denied} denied${j.net.stats.denied_hosts.length?' ('+esc(j.net.stats.denied_hosts.slice(-4).join(', '))+')':''}`:'none (tools offline)'}</dd><dt>branch</dt><dd>${esc(j.branch||'— (ran in repo)')}</dd><dt>base</dt><dd>${esc((j.base||'').slice(0,12))}</dd><dt>started</dt><dd>${ts(j.created)}</dd><dt>ended</dt><dd>${ts(j.ended)}</dd></dl>
 <div class=cmds><button class=ghost data-copy="dsw result ${esc(j.id)}">copy: dsw result</button><button class=ghost data-copy="dsw tail ${esc(j.id)}">copy: dsw tail</button>${j.state==='running'?`<button class=ghost data-copy="dsw cancel ${esc(j.id)}">copy: dsw cancel</button>`:`<button class=ghost data-copy="dsw resume ${esc(j.id)} --task &quot;…&quot;">copy: dsw resume</button>`}</div>
 <section><h3>Observed changes <span class=na style="text-transform:none;font-weight:400">${esc(f.diffstat||'')}</span></h3>${files?`<table class=files>${files}</table>`:'<div class=na>no file changes</div>'}</section>
 <section><h3>Check</h3>${chk}</section>
 <section><h3>Attempts</h3><table class=att><tr><th>#</th><th>Tier</th><th class=num>Steps</th><th class=num>Tools</th><th>Tokens</th><th class=num>Cost</th><th>Stop</th></tr>${att}</table></section>
 <section><details class=claim ${claim.length<400?'open':''}><summary>Worker claim — unverified (${claim.length} chars)</summary><pre>${esc(claim)}</pre></details></section>
 <section><h3>Events <span class=na style="text-transform:none;font-weight:400">last ${ev.length}</span></h3><ul class=tl>${tl||'<li class=na>no events yet</li>'}</ul></section>`;
 dp.scrollTop=dtop;
 document.querySelectorAll('[data-copy]').forEach(b=>b.onclick=()=>{navigator.clipboard?.writeText(b.dataset.copy);b.textContent='copied';setTimeout(detail,900)});}
function render(){renderTabs();renderRows();renderKpis()}
const A={d:null,sel:null,filter:'all',q:''};
const STOP_PILL={running:'running',ok:'done',error:'failed',timeout:'timeout',cancelled:'cancelled',quota:'needs_input',queue:'needs_input',lost:'orphaned'};
const dur=s=>s==null?'–':s>=3600?`${Math.floor(s/3600)}h${String(Math.floor(s%3600/60)).padStart(2,'0')}m`:`${Math.floor(s/60)}m${String(s%60).padStart(2,'0')}s`;
const when=iso=>{if(!iso)return'–';const d=new Date(iso);return d.toDateString()===new Date().toDateString()?d.toLocaleTimeString():d.toLocaleString()};
const base=p=>String(p||'').split('/').filter(Boolean).pop()||p||'–';
function agyJobs(){const d=A.d;if(!d||!d.available)return[];
 return [...d.running.map(j=>({...j,stop:'running',duration_s:j.elapsed_s,rc:null})),...d.recent]}
function agyMatch(j){const f=A.filter;if(f==='running'&&j.stop!=='running')return false;if(f==='ok'&&j.stop!=='ok')return false;
 if(f==='failed'&&['running','ok'].includes(j.stop))return false;const q=A.q.toLowerCase();
 return !q||`${j.job} ${j.cwd} ${j.model}`.toLowerCase().includes(q)}
function renderAgy(){const d=A.d;$('agy-n').textContent=d&&d.available&&d.running.length?d.running.length:'';
 if(S.view!=='agy')return;const jobs=agyJobs();
 if(!d||!d.available){$('a-rows').innerHTML=`<tr><td colspan=7 class=empty>${esc(d?d.error:'loading…')}</td></tr>`;return}
 const tabs=[['all','All'],['running','Running'],['ok','OK'],['failed','Failed']];const cnt=k=>jobs.filter(j=>{const f=A.filter;A.filter=k;const r=agyMatch(j);A.filter=f;return r}).length;
 $('a-tabs').innerHTML=tabs.map(([k,l])=>`<button class="tab ${A.filter===k?'on':''}" data-f="${k}">${l}<span class=n>${cnt(k)}</span></button>`).join('')+`<input id=aq placeholder="filter job / cwd / model" value="${esc(A.q)}">`;
 document.querySelectorAll('#a-tabs .tab').forEach(b=>b.onclick=()=>{A.filter=b.dataset.f;renderAgy()});
 const aq=$('aq');aq.oninput=()=>{A.q=aq.value;const pos=aq.selectionStart;renderAgy();$('aq').focus();$('aq').setSelectionRange(pos,pos)};
 const rows=jobs.filter(agyMatch);
 $('a-rows').innerHTML=rows.length?rows.map(j=>`<tr data-id="${esc(j.job)}" class="${j.job===A.sel?'sel':''}"><td class=mono>${esc(j.job)}</td>
  <td><span class="pill st-${STOP_PILL[j.stop]||'failed'}"><i></i>${esc(j.stop)}</span></td><td class=mono>${esc(j.model_actual||j.model||'default')}</td>
  <td class=task title="${esc(j.cwd)}">${esc(base(j.cwd))}</td><td class=mono>${esc(when(j.start))}</td>
  <td class="num mono">${dur(j.duration_s)}</td><td class="num mono">${j.rc==null?'–':esc(j.rc)}</td></tr>`).join('')
  :`<tr><td colspan=7 class=empty>No jobs match.</td></tr>`;
 document.querySelectorAll('#a-rows tr[data-id]').forEach(tr=>tr.onclick=()=>{A.sel=tr.dataset.id;renderAgy()});
 const today=new Date().toDateString(),td=d.recent.filter(j=>new Date(j.start).toDateString()===today);
 $('livedot').className='dot'+(d.running.length?' on':'');$('livetxt').textContent=d.running.length?`${d.running.length} running`:'idle';
 $('a-run').textContent=d.running.length;$('a-run-s').textContent=`${d.slots.used}/${d.slots.max} slots`;
 $('a-today').textContent=td.length;$('a-today-s').innerHTML=`<span class=ok>✓ ${td.filter(j=>j.stop==='ok').length}</span> · <span class=bad>✗ ${td.filter(j=>j.stop!=='ok').length}</span>`;
 const secs=td.reduce((a,j)=>a+(j.duration_s||0),0);$('a-time').textContent=dur(secs);$('a-time-s').textContent=td.length?`avg ${dur(Math.round(secs/td.length))}`:' ';
 const grp=p=>(d.quota?.groups||[]).find(g=>g.name.startsWith(p));
 [['a-gem','Gemini'],['a-cl','Claude']].forEach(([id,p])=>{const g=grp(p);$(id).textContent=g?Math.round(g.remaining*100)+'%':'–';
  $(id).className='v'+(g&&g.remaining<0.1?' bad':'');$(id+'-s').textContent=g?`reset ${when(g.reset)}`:'not checked yet'});
 agyDetail(jobs)}
// The agy detail pane is built once per selected job, then only the parts whose HTML changed are
// replaced, keeping the pane's scroll position (and following the bottom when already there).
const H={};
function setHtml(id,html){if(H[id]===html)return;const e=$(id);if(!e)return;const p=$('a-detail');
 const follow=p.scrollTop>0&&p.scrollTop+p.clientHeight>=p.scrollHeight-24,top=p.scrollTop;
 H[id]=html;e.innerHTML=html;p.scrollTop=follow?p.scrollHeight:top}
function agyDetail(jobs){const j=jobs.find(x=>x.job===A.sel);const pane=$('a-detail');
 if(!j){if(A.sel&&A.shown!==''){A.shown='';pane.innerHTML='<div class=empty>Job no longer listed.</div>'}return}
 const live=j.stop==='running';
 if(A.shown!==j.job){A.shown=j.job;A.txDone=false;Object.keys(H).forEach(k=>delete H[k]);
  pane.innerHTML=`<div class=dh id=a-d-head></div><dl class=meta id=a-d-meta></dl>
  <div class=cmds><button class=ghost data-copy="tmux -L agy-worker attach">copy: tmux attach</button><button class=ghost data-copy="agy-pane transcript ${esc(j.job)}">copy: agy-pane transcript</button></div>
  <section><h3>Transcript <span class=na id=a-tx-s style="text-transform:none;font-weight:400"></span></h3><ul class=tl id=a-tx><li class=na>loading…</li></ul></section>
  <div id=a-d-io></div>`;pane.scrollTop=0;
  pane.querySelectorAll('[data-copy]').forEach(b=>b.onclick=()=>{navigator.clipboard?.writeText(b.dataset.copy);b.textContent='copied'})}
 setHtml('a-d-head',`<h2>${esc(j.job)}</h2><span class="pill st-${STOP_PILL[j.stop]||'failed'}"><i></i>${esc(j.stop)}</span><span class=spacer></span><span class=mono>${dur(j.duration_s)}</span>`);
 setHtml('a-d-meta',`<dt>cwd</dt><dd title="${esc(j.cwd)}">${esc(j.cwd)}</dd><dt>model</dt><dd>${esc(j.model_actual||j.model||'default')}</dd><dt>conversation</dt><dd>${esc(j.conversation||'not found yet')}</dd><dt>slot</dt><dd>${esc(j.slot)}</dd>
 <dt>started</dt><dd>${esc(when(j.start))}</dd><dt>${live?'hard timeout':'ended'}</dt><dd>${live?dur(j.timeout_s):esc(when(j.end))}</dd><dt>exit</dt><dd>${j.rc==null?'–':esc(j.rc)}</dd>`);
 setHtml('a-d-io',live?`<section class=ag-tail><h3>stdout <span class=na>last lines, live</span></h3><pre>${esc(j.out_tail)||'<span class=na>no output yet</span>'}</pre></section>
 <section class=ag-tail><h3>stderr <span class=na>last lines</span></h3><pre>${esc(j.err_tail)||'<span class=na>empty</span>'}</pre></section>`
 :`<section><div class=na>stdout/stderr are not kept after a job ends (agy-pane removes the job dir); the caller got them.</div></section>`);
 if(!j.conversation){setHtml('a-tx','<li class=na>no agy conversation matched yet</li>');return}
 if(live||!A.txDone)agyTranscript(j.job,live)}
async function agyTranscript(job,live){if(A.txBusy)return;A.txBusy=true;
 try{const r=await fetch('api/agy/transcript/'+encodeURIComponent(job));if(!r.ok||A.shown!==job)return;const t=await r.json();
  setHtml('a-tx-s',`${esc(t.model||'')} · ${t.steps} events · in ${kfmt(t.usage.in)} · cache ${kfmt(t.usage.cache)} · out ${kfmt(t.usage.out)}${t.events.length<t.steps?` · last ${t.events.length}`:''}${live?' · live':''}`);
  setHtml('a-tx',t.events.map(e=>e.k==='think'?`<li><span class=ic>◇</span><span class=t style="font-style:italic">${esc(e.text)}</span></li>`
   :e.k==='text'?`<li class=final><span class=ic>”</span><span class=t>${esc(e.text)}</span></li>`
   :e.k==='call'?`<li class=call><span class=ic>→</span><span><b>${esc(e.tool)}</b> <code>${esc(e.text)}</code></span></li>`
   :`<li class="result ${/exited with code [1-9]/.test(e.text)?'err':''}"><span class=ic>←</span><span class=t>${esc(e.text)}</span></li>`).join('')||'<li class=na>no steps yet</li>');
  if(!live)A.txDone=true}
 catch(e){}finally{A.txBusy=false}}
async function refreshAgy(){try{A.d=await (await fetch('api/agy')).json();renderAgy()}catch(e){}}
function setView(v){S.view=v;try{localStorage.setItem('dsw-view',v)}catch(e){}
 document.querySelectorAll('.views .tab').forEach(b=>b.classList.toggle('on',b.dataset.v===v));
 $('v-dsw').hidden=v!=='dsw';$('v-agy').hidden=v!=='agy';document.querySelectorAll('.dsw-only').forEach(e=>e.hidden=v!=='dsw');
 $('brand-s').textContent=v==='agy'?'agy-pane jobs · read-only':'DeepSeek workers · read-only';
 if(v==='dsw'){S.stats&&drawSpend(S.stats.days);renderKpis()}else renderAgy()}
document.querySelectorAll('.views .tab').forEach(b=>b.onclick=()=>setView(b.dataset.v));
async function refresh(){try{S.jobs=await (await fetch('api/jobs')).json();S.stats=await (await fetch('api/stats')).json();render();if(S.sel&&S.jobs.some(j=>j.id===S.sel&&j.state==='running'))detail()}catch(e){$('livetxt').textContent='offline'}}
async function bal(){try{$('bal').textContent='balance '+(await (await fetch('api/balance')).text())}catch(e){}}
const tip=$('tip');document.addEventListener('mousemove',e=>{const t=e.target.closest('[data-tip]');if(!t){tip.style.display='none';return}
 tip.textContent=t.dataset.tip;tip.style.display='block';tip.style.left=Math.min(e.clientX+12,innerWidth-tip.offsetWidth-8)+'px';tip.style.top=(e.clientY+14)+'px'});
function setTheme(t){try{localStorage.setItem('dsw-theme',t)}catch(e){};if(t==='auto')document.documentElement.removeAttribute('data-theme');else document.documentElement.dataset.theme=t;$('theme').textContent=t==='dark'?'☾':t==='light'?'☀':'◐'}
let theme='auto';try{theme=localStorage.getItem('dsw-theme')||'auto'}catch(e){};setTheme(theme);
$('theme').onclick=()=>{theme=theme==='auto'?'dark':theme==='dark'?'light':'auto';setTheme(theme);drawSpend(S.stats?.days||[])};
addEventListener('resize',()=>S.stats&&drawSpend(S.stats.days));
let view='dsw';try{view=localStorage.getItem('dsw-view')||'dsw'}catch(e){};setView(view);
refresh();bal();refreshAgy();setInterval(refresh,3000);setInterval(refreshAgy,3000);setInterval(bal,60000);
</script></body></html>"""
