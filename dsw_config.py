"""dsw configuration: CLI flag > env DSW_* > config file > auto-discovery.

Config file: $DSW_CONFIG or ${XDG_CONFIG_HOME:-~/.config}/dsw/config.toml (TOML, all keys optional):

    dsh = "/path/to/node_modules/.bin/dsh"     # else: `dsh` on PATH, then the dsw-managed runtime
    node_bin = "/path/to/node24/bin"           # else: newest node >= 24.9 on PATH / nvm / fnm / volta
    key_file = "~/.config/dsw/api_key"         # DeepSeek key (one line); else env DEEPSEEK_API_KEY
    state_dir = "~/.local/state/dsw"
    dsh_home = "~/.dsh"                        # else $DSH_HOME
    profiles = ["headless"]                    # dsh profiles `dsw setup` manages ("web" for a dsh-crew hub)
    secret_guard = "/path/to/dsh-secret-guard" # plugin dir; else next to dsw (plugin/ or ../dsh-secret-guard)
    gate = "/path/to/gate.py"                  # optional: your own Claude Code-style gate; default: plugin's bundled one
    serve_port = 8791

    [defaults]   # tier, effort_flash, effort_pro, max_steps, max_steps_pro, timeout
    [net]        # allow = ["extra.host"]
    [sandbox]    # hide = ["~/extra-secret-dir"], home_allow = ["~/.sdkman"] (read-only toolchains)
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None

HERE = Path(__file__).resolve().parent
MIN_NODE = (24, 9)


def _xdg(name: str, fallback: str) -> Path:
    return Path(os.environ.get(name) or Path.home() / fallback)


CONFIG_PATH = Path(os.environ.get("DSW_CONFIG") or _xdg("XDG_CONFIG_HOME", ".config") / "dsw" / "config.toml")
MANAGED_RUNTIME = _xdg("XDG_DATA_HOME", ".local/share") / "dsw" / "dsh-runtime"
HIDDEN_PATHS_FILE = HERE / "sandbox" / "hidden-paths.txt"
HOME_ALLOW_FILE = HERE / "sandbox" / "home-allow.txt"
LOCAL_HOME_ALLOW_FILE = CONFIG_PATH.parent / "home-allow.local.txt"
LOCAL_HIDDEN_FILE = CONFIG_PATH.parent / "hidden-paths.local.txt"
NET_ALLOW_FILE = CONFIG_PATH.parent / "net-allow.txt"


def expand(path: str | Path) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(path))))


def node_version(bin_dir: Path) -> tuple[int, ...] | None:
    node = bin_dir / "node"
    if not os.access(node, os.X_OK):
        return None
    try:
        out = subprocess.run([str(node), "--version"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.match(r"v(\d+)\.(\d+)\.(\d+)", out.strip())
    return tuple(int(x) for x in match.groups()) if match else None


def discover_node() -> Path | None:
    """Newest node >= 24.9 from PATH, nvm, fnm or volta."""
    candidates: list[Path] = []
    on_path = shutil.which("node")
    if on_path:
        candidates.append(Path(on_path).resolve().parent)
    for pattern in (".nvm/versions/node/*/bin", ".local/share/fnm/node-versions/*/installation/bin", ".volta/bin"):
        candidates += sorted(Path.home().glob(pattern))
    best = None
    for bin_dir in candidates:
        version = node_version(bin_dir)
        if version and version[:2] >= MIN_NODE and (best is None or version > best[0]):
            best = (version, bin_dir)
    return best[1] if best else None


def discover_dsh() -> Path | None:
    on_path = shutil.which("dsh")
    if on_path:
        return Path(on_path)
    managed = MANAGED_RUNTIME / "node_modules" / ".bin" / "dsh"
    return managed if managed.exists() else None


def discover_secret_guard() -> Path | None:
    for candidate in (HERE / "plugin", HERE.parent / "dsh-secret-guard"):
        if (candidate / "index.js").is_file():
            return candidate
    return None


def hidden_paths(extra: list[str]) -> list[str]:
    """Credential paths hidden from workers: shipped list + local file + config (all `~`-relative allowed)."""
    paths: list[str] = []
    for source in (HIDDEN_PATHS_FILE, LOCAL_HIDDEN_FILE):
        if source.is_file():
            paths += [line.strip() for line in source.read_text().splitlines()
                      if line.strip() and not line.lstrip().startswith("#")]
    paths += extra
    return list(dict.fromkeys(paths))


def home_allow(extra: list[str]) -> list[str]:
    """Home paths visible read-only to workers: shipped list + local file + config."""
    paths: list[str] = []
    for source in (HOME_ALLOW_FILE, LOCAL_HOME_ALLOW_FILE):
        if source.is_file():
            paths += [line.strip() for line in source.read_text().splitlines()
                      if line.strip() and not line.lstrip().startswith("#")]
    return list(dict.fromkeys(paths + extra))


def runtime_dir() -> Path:
    """Socket directory shared with sandbox/dsw-bwrap (same formula there)."""
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    if not Path(base).is_dir():
        base = os.path.join(os.environ.get("TMPDIR") or "/tmp", f"dsw-{os.getuid()}")
    return Path(base) / "dsw"


@dataclass
class Config:
    dsh: Path | None
    node_bin: Path | None
    key_file: Path | None
    state_dir: Path
    dsh_home: Path
    profiles: list[str]
    secret_guard: Path | None
    gate: Path | None
    serve_port: int
    defaults: dict
    net_allow: list[str]
    hide: list[str]
    home_allow: list[str]
    sources: dict = field(default_factory=dict)
    path: Path = CONFIG_PATH
    file_found: bool = False


def load() -> Config:
    data: dict = {}
    found = CONFIG_PATH.is_file()
    if found:
        if tomllib is None:
            raise SystemExit("dsw: reading config.toml needs Python 3.11+ (tomllib)")
        data = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    sources: dict = {}

    def pick(key: str, env: str | None, discover=None, fallback=None, as_path: bool = True):
        if env and os.environ.get(env):
            sources[key] = f"env {env}"
            value = os.environ[env]
        elif key in data:
            sources[key] = "config"
            value = data[key]
        elif discover is not None and (found_value := discover()) is not None:
            sources[key] = "discovered"
            value = found_value
        elif fallback is not None:
            sources[key] = "default"
            value = fallback
        else:
            sources[key] = "missing"
            return None
        return expand(value) if as_path else value

    legacy_key = Path.home() / ".config" / "deepseek-worker" / "api_key"
    default_key = CONFIG_PATH.parent / "api_key"
    key_default = default_key if default_key.exists() or not legacy_key.exists() else legacy_key
    defaults = {"tier": "flash", "effort_flash": "high", "effort_pro": "high", "max_steps": 60, "max_steps_pro": 25, "timeout": 1800}
    defaults.update(data.get("defaults") or {})
    return Config(
        dsh=pick("dsh", "DSW_DSH", discover_dsh),
        node_bin=pick("node_bin", "DSW_NODE_BIN", discover_node),
        key_file=pick("key_file", "DSW_KEY_FILE", fallback=key_default),
        state_dir=pick("state_dir", "DSW_HOME", fallback=_xdg("XDG_STATE_HOME", ".local/state") / "dsw"),
        dsh_home=pick("dsh_home", "DSH_HOME", fallback=Path.home() / ".dsh"),
        profiles=pick("profiles", None, fallback=["headless"], as_path=False),
        secret_guard=pick("secret_guard", "DSW_SECRET_GUARD", discover_secret_guard),
        gate=pick("gate", "DSW_GATE"),  # unset: the plugin uses its bundled gate/secret_gate.py
        serve_port=int(pick("serve_port", "DSW_SERVE_PORT", fallback=8791, as_path=False)),
        defaults=defaults,
        net_allow=list((data.get("net") or {}).get("allow") or []),
        hide=hidden_paths(list((data.get("sandbox") or {}).get("hide") or [])),
        home_allow=home_allow(list((data.get("sandbox") or {}).get("home_allow") or [])),
        sources=sources,
        file_found=found,
    )
