"""`dsw setup` (wire dsh profiles) and `dsw doctor` (explain config, check the machine)."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import dsw_config

BEGIN = "# >>> dsw managed (written by `dsw setup`; edits inside this block are overwritten)"
END = "# <<< dsw managed"
LEGACY_MARKERS = ("dsh-secret-guard", "dsw-bwrap")


def _env(cfg) -> dict:
    env = dict(os.environ, COREPACK_ENABLE_DOWNLOAD_PROMPT="0")
    if cfg.node_bin:
        env["PATH"] = f"{cfg.node_bin}:{env.get('PATH', '')}"
    if cfg.dsh_home:
        env["DSH_HOME"] = str(cfg.dsh_home)
    return env


def dump_config(cfg, profile: str) -> str:
    if not cfg.dsh:
        return ""
    try:
        return subprocess.run([str(cfg.dsh), "--profile", profile, "--dump-config"], capture_output=True, text=True,
                              env=_env(cfg), timeout=90).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def profile_checks(cfg, profile: str) -> list[tuple[bool, str]]:
    dump = dump_config(cfg, profile)
    wrapper = str(dsw_config.HERE / "sandbox" / "dsw-bwrap")
    return [
        (bool(dump), f"[{profile}] dsh --dump-config runs"),
        ("name: dsh-secret-guard" in dump, f"[{profile}] dsh-secret-guard mounted"),
        ("    - secretGuard" in dump, f"[{profile}] agent-loop injects secretGuard (fail-closed)"),
        (wrapper in dump, f"[{profile}] sandbox.runnerCommand = {wrapper}"),
    ]


def managed_block(cfg) -> str:
    plugin_config = {"sensitivePaths": cfg.hide, "homeAllow": cfg.home_allow}
    if cfg.gate:
        plugin_config["gatePath"] = str(cfg.gate)
    wrapper = str(dsw_config.HERE / "sandbox" / "dsw-bwrap")
    lines = [
        BEGIN,
        "- insert:",
        "    - id: secret-guard",
        "      name: 'dsh-secret-guard'",
        f"      config: {json.dumps(plugin_config)}",
        "- id: agent-loop",
        "  inject: [secretGuard]",
        "- id: sandbox",
        "  config:",
        f"    runnerCommand: [{json.dumps(wrapper)}]",
        "    runnerFailureSignatures: ['bwrap:']",
        END,
    ]
    return "\n".join(lines) + "\n"


def write_patch(path: Path, block: str, dry_run: bool) -> str:
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    if BEGIN in text and END in text:
        start, stop = text.index(BEGIN), text.index(END) + len(END)
        new = text[:start] + block.rstrip("\n") + text[stop:]
        action = "updated managed block"
    else:
        header = [line for line in text.splitlines() if line.startswith("#")]
        body = [line for line in text.splitlines() if line.strip() and not line.startswith("#")]
        legacy = any(marker in text for marker in LEGACY_MARKERS)
        if body in ([], ["[]"]) or legacy:
            if legacy and not dry_run:
                backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d%H%M%S')}")
                shutil.copy2(path, backup)
                action = f"replaced pre-setup dsw entries (backup {backup.name})"
            else:
                action = "wrote managed block"
            new = "\n".join(h for h in header if "dsw" not in h.lower() and "secret-guard" not in h.lower()
                            and "agent-loop" not in h.lower() and "refuses to start" not in h.lower()) + "\n" + block
        else:
            new = text.rstrip("\n") + "\n" + block
            action = "appended managed block after your own entries"
    if new != text and not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new, encoding="utf-8")
    return action if new != text else "already up to date"


def cmd_setup(args) -> int:
    cfg = dsw_config.load()
    problems = [msg for ok, msg in (
        (cfg.dsh is not None, "dsh not found: set `dsh` in config.toml or put dsh on PATH"),
        (cfg.node_bin is not None, "node >= 24.9 not found: set `node_bin` in config.toml"),
        (cfg.secret_guard is not None, "dsh-secret-guard plugin dir not found: set `secret_guard` in config.toml"),
    ) if not ok]
    if problems:
        for problem in problems:
            print(f"✗ {problem}")
        return 1
    if args.write_config and not cfg.file_found:
        cfg.path.parent.mkdir(parents=True, exist_ok=True)
        cfg.path.write_text(
            f'dsh = "{cfg.dsh}"\nnode_bin = "{cfg.node_bin}"\nprofiles = {json.dumps(args.profile or cfg.profiles)}\n'
            f'secret_guard = "{cfg.secret_guard}"\n' + (f'gate = "{cfg.gate}"\n' if cfg.gate else "")
            + f"serve_port = {cfg.serve_port}\n", encoding="utf-8")
        print(f"✓ wrote {cfg.path}")
    profiles = args.profile or cfg.profiles
    generated = cfg.path.parent / "hidden-paths.generated.txt"
    config_hide = [p for p in cfg.hide if p not in dsw_config.hidden_paths([])]
    config_allow = [p for p in cfg.home_allow if p not in dsw_config.home_allow([])]
    if not args.dry_run:
        generated.parent.mkdir(parents=True, exist_ok=True)
        generated.write_text("# generated by `dsw setup` from config.toml [sandbox] hide\n" + "\n".join(config_hide) + "\n")
        (generated.parent / "home-allow.generated.txt").write_text(
            "# generated by `dsw setup` from config.toml [sandbox] home_allow\n" + "\n".join(config_allow) + "\n")
    block = managed_block(cfg)
    code = 0
    for profile in profiles:
        profile_dir = cfg.dsh_home / "profiles" / profile
        package = profile_dir / "package.json"
        linked = package.is_file() and "dsh-secret-guard" in package.read_text(encoding="utf-8")
        if not linked:
            if args.dry_run:
                print(f"· [{profile}] would link dsh-secret-guard from {cfg.secret_guard}")
            else:
                run = subprocess.run([str(cfg.dsh), "plugin", "--profile", profile, "add", f"link:{cfg.secret_guard}"],
                                     capture_output=True, text=True, env=_env(cfg), timeout=300)
                print(("✓" if run.returncode == 0 else "✗") + f" [{profile}] link dsh-secret-guard")
                if run.returncode:
                    print("  " + (run.stderr or run.stdout).strip()[-400:])
                    code = 1
                    continue
        action = write_patch(profile_dir / "cordis.patch.yml", block, args.dry_run)
        print(f"✓ [{profile}] cordis.patch.yml: {action}")
        if not args.dry_run:
            for ok, message in profile_checks(cfg, profile):
                print(("✓ " if ok else "✗ ") + message)
                code |= 0 if ok else 1
    if "web" in profiles and not args.dry_run:
        print("· restart `dsh web` so the hub loads the new patch")
    return code


def _port_free(port: int) -> bool:
    with socket.socket() as sock:
        return sock.connect_ex(("127.0.0.1", port)) != 0


def cmd_doctor(args) -> int:
    cfg = dsw_config.load()
    print(f"config file: {cfg.path} ({'found' if cfg.file_found else 'not found — using env/discovery/defaults'})")
    for key in ("dsh", "node_bin", "key_file", "state_dir", "dsh_home", "profiles", "secret_guard", "gate", "serve_port"):
        print(f"  {key:<13} {str(getattr(cfg, key)):<70} [{cfg.sources.get(key, '?')}]")
    print(f"  hidden paths  {len(cfg.hide)} (shipped + local + config)   net allow extra: {cfg.net_allow or '-'}")
    checks: list[tuple[bool, str]] = [(sys.version_info >= (3, 11), f"python {sys.version.split()[0]} (>= 3.11 for config.toml)")]
    version = dsw_config.node_version(cfg.node_bin) if cfg.node_bin else None
    checks.append((bool(version and version[:2] >= dsw_config.MIN_NODE),
                   f"node {'.'.join(map(str, version)) if version else 'missing'} (>= 24.9)"))
    dsh_version = ""
    if cfg.dsh:
        try:
            dsh_version = subprocess.run([str(cfg.dsh), "--version"], capture_output=True, text=True, env=_env(cfg),
                                         timeout=60).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
    checks.append((bool(dsh_version), f"dsh {dsh_version or 'not runnable'}"))
    for tool in ("bwrap", "socat", "ip", "git"):
        checks.append((shutil.which(tool) is not None, f"{tool} on PATH"))
    key_ok = bool(cfg.key_file and cfg.key_file.is_file() and cfg.key_file.stat().st_size > 0) or bool(
        os.environ.get("DEEPSEEK_API_KEY"))
    checks.append((key_ok, "DeepSeek key present (key_file or DEEPSEEK_API_KEY)"))
    if cfg.key_file and cfg.key_file.is_file():
        mode = cfg.key_file.stat().st_mode & 0o777
        checks.append((mode & 0o077 == 0, f"key_file permissions {oct(mode)} (no group/other access)"))
    checks.append((dsw_config.HIDDEN_PATHS_FILE.is_file(), "sandbox/hidden-paths.txt present"))
    for profile in cfg.profiles:
        checks += profile_checks(cfg, profile)
    port_free = _port_free(cfg.serve_port)
    checks.append((True, f"serve port {cfg.serve_port} " + ("free" if port_free else "in use (dsw serve already running?)")))
    for ok, message in checks:
        print(("✓ " if ok else "✗ ") + message)
    failed = [m for ok, m in checks if not ok]
    if failed:
        print(f"\n{len(failed)} problem(s). Fix config.toml, then `dsw setup`.")
    return 1 if failed else 0
