"""Per-job egress proxy for `dsw run --net [allowlist|public]`.

Modes: `allowlist` (default) tunnels only to allowlisted hosts; `public` tunnels to any
host whose addresses are public and routed via the default route. Both refuse private,
VPN, cluster and office-routed destinations and anything but HTTPS (CONNECT :443).

The worker's tools stay in bwrap's empty network namespace. sandbox/dsw-bwrap bridges
127.0.0.1:3128 inside the sandbox to this proxy's unix socket and sets HTTPS_PROXY, so
pip/npm/git/curl can reach allowlisted hosts and nothing else. The proxy only tunnels
HTTPS (CONNECT to port 443), and refuses any destination that is not public or that the
host would route outside its default route (VPN / cluster / LAN routes).
"""
from __future__ import annotations

import errno
import hashlib
import ipaddress
import json
import os
import re
import select
import socket
import subprocess
import threading
import time
from pathlib import Path

DEFAULT_ALLOW = (
    "pypi.org", "files.pythonhosted.org",
    "registry.npmjs.org", "registry.yarnpkg.com", "repo.yarnpkg.com",
    "github.com", "codeload.github.com", "objects.githubusercontent.com", "raw.githubusercontent.com",
    "release-assets.githubusercontent.com",
    "proxy.golang.org", "sum.golang.org",
    "crates.io", "index.crates.io", "static.crates.io",
    "repo.maven.apache.org", "repo1.maven.org", "plugins.gradle.org",
)
import dsw_config  # noqa: E402

EXTRA_ALLOW_FILES = (dsw_config.NET_ALLOW_FILE, Path.home() / ".config/deepseek-worker/net-allow.txt")
MAX_TUNNELS = 32  # concurrent tunnels per job; more are refused, not queued
HOST_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")


def socket_path(workdir: str | Path) -> Path:
    """Shared with sandbox/dsw-bwrap: one socket per workspace (dsw allows one writer per workspace)."""
    digest = hashlib.sha256(str(Path(workdir).resolve()).encode()).hexdigest()[:16]
    return dsw_config.runtime_dir() / f"{digest}.sock"


def allowlist(extra: list[str] | None = None) -> list[str]:
    hosts = list(DEFAULT_ALLOW)
    for extra_file in EXTRA_ALLOW_FILES:
        if extra_file.is_file():
            hosts += [line.strip() for line in extra_file.read_text().splitlines()
                      if line.strip() and not line.startswith("#")]
    return hosts + list(extra or [])


def host_allowed(host: str, allowed: list[str]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == a or host.endswith("." + a) for a in (x.lower() for x in allowed))


def _default_dev() -> str | None:
    out = subprocess.run(["ip", "route", "show", "default"], capture_output=True, text=True).stdout
    match = re.search(r"\bdev (\S+)", out)
    return match.group(1) if match else None


def specific_routes() -> list:
    """Every non-default route prefix on this host (IPv4 and IPv6)."""
    networks = []
    for family in ("-4", "-6"):
        out = subprocess.run(["ip", family, "route"], capture_output=True, text=True).stdout
        for line in out.splitlines():
            prefix = line.split()[0] if line.split() else ""
            if prefix in ("default", "unreachable", "prohibit", "blackhole") or not re.match(r"[0-9a-fA-F:.]", prefix):
                continue
            try:
                networks.append(ipaddress.ip_network(prefix, strict=False))
            except ValueError:
                continue
    return networks


def route_class(ip: str, default_dev: str | None) -> str:
    """'ok' = public and routed via the default interface; 'bad' = private or routed elsewhere
    (VPN / cluster / LAN); 'none' = no route (e.g. IPv6 on an IPv4-only host)."""
    address = ipaddress.ip_address(ip)
    if not address.is_global:
        return "bad"
    if any(address in network for network in specific_routes() if network.version == address.version):
        return "bad"  # covered by a specific route (VPN, cluster, office network), even on the default device
    out = subprocess.run(["ip", "route", "get", ip], capture_output=True, text=True).stdout
    match = re.search(r"\bdev (\S+)", out)
    if not match:
        return "none"
    return "ok" if default_dev and match.group(1) == default_dev else "bad"


class AllowlistProxy:
    def __init__(self, workdir: str | Path, log_path: Path, extra: list[str] | None = None, mode: str = "allowlist"):
        if mode not in ("allowlist", "public"):
            raise ValueError(f"unknown net mode {mode!r}")
        self.mode = mode
        self.path = socket_path(workdir)
        self.log_path = log_path
        self.allowed = allowlist(extra)
        self.default_dev = _default_dev()
        self.stats = {"allowed": 0, "denied": 0, "denied_hosts": []}
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(MAX_TUNNELS)
        self.server: socket.socket | None = None

    def start(self) -> None:
        for directory in (self.path.parent.parent, self.path.parent):  # e.g. /tmp/dsw-<uid> and its dsw/
            if directory == Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"):
                continue
            directory.mkdir(mode=0o700, exist_ok=True)
            info = directory.lstat()
            if directory.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise RuntimeError(f"refusing proxy socket dir {directory}: not a private directory owned by this user")
        if self.path.exists():
            self.path.unlink()
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.path))
        os.chmod(self.path, 0o600)
        self.server.listen(64)
        threading.Thread(target=self._accept, daemon=True).start()

    def stop(self) -> None:
        if self.server:
            self.server.close()
            self.server = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def _log(self, host: str, port: int, verdict: str, reason: str = "") -> None:
        with self.lock:
            key = "allowed" if verdict == "allow" else "denied"
            self.stats[key] += 1
            if verdict != "allow" and host not in self.stats["denied_hosts"]:
                self.stats["denied_hosts"] = (self.stats["denied_hosts"] + [host])[-20:]
            with open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"t": time.time(), "host": host, "port": port, "verdict": verdict,
                                         "reason": reason}) + "\n")

    def _accept(self) -> None:
        while self.server:
            try:
                client, _ = self.server.accept()
            except OSError as error:
                if self.server is None:
                    return  # stopped
                if error.errno in (errno.EMFILE, errno.ENFILE, errno.ENOBUFS, errno.ENOMEM):
                    time.sleep(0.1)  # out of fds: back off, never give up serving
                    continue
                return
            if not self.slots.acquire(blocking=False):
                client.close()  # too many tunnels at once: refuse this one, keep the proxy alive
                continue
            threading.Thread(target=self._handle_slot, args=(client,), daemon=True).start()

    def _handle_slot(self, client: socket.socket) -> None:
        try:
            self._handle(client)
        finally:
            self.slots.release()

    def _handle(self, client: socket.socket) -> None:
        upstream = None
        try:
            client.settimeout(30)
            head = b""
            while b"\r\n\r\n" not in head and len(head) < 16384:
                chunk = client.recv(4096)
                if not chunk:
                    return
                head += chunk
            line = head.split(b"\r\n", 1)[0].decode("latin-1")
            parts = line.split()
            if len(parts) < 2 or parts[0].upper() != "CONNECT":
                self._log(parts[1] if len(parts) > 1 else "?", 0, "deny", "only HTTPS CONNECT is proxied")
                client.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                return
            host, _, port_text = parts[1].rpartition(":")
            port = int(port_text) if port_text.isascii() and port_text.isdigit() else 0
            host = host.strip("[]")
            reason = ""
            if port != 443:
                reason = "only port 443"
            elif not HOST_RE.match(host):
                reason = "invalid host"
            elif self.mode == "allowlist" and not host_allowed(host, self.allowed):
                reason = "host not in allowlist"
            ips: list[str] = []
            if not reason:
                try:
                    ips = sorted({info[4][0] for info in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)})
                except socket.gaierror:
                    reason = "DNS failure"
            classes = {ip: route_class(ip, self.default_dev) for ip in ips} if not reason else {}
            if not reason and any(c == "bad" for c in classes.values()):
                reason = "resolves to a private, VPN or cluster route"
            ips = [ip for ip, c in classes.items() if c == "ok"]
            if not reason and not ips:
                reason = "no routable public address"
            if reason:
                self._log(host, port, "deny", reason)
                client.sendall(f"HTTP/1.1 403 Forbidden\r\nX-Dsw-Reason: {reason}\r\nContent-Length: 0\r\n\r\n".encode())
                return
            upstream = socket.create_connection((ips[0], port), timeout=20)
            self._log(host, port, "allow")
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            rest = head.split(b"\r\n\r\n", 1)[1]
            if rest:
                upstream.sendall(rest)
            client.settimeout(None)
            upstream.settimeout(None)
            sockets = [client, upstream]
            while True:
                readable, _, _ = select.select(sockets, [], [], 300)
                if not readable:
                    return
                for source in readable:
                    data = source.recv(65536)
                    if not data:
                        return
                    (upstream if source is client else client).sendall(data)
        except OSError:
            return
        finally:
            for sock in (client, upstream):
                if sock:
                    try:
                        sock.close()
                    except OSError:
                        pass
