"""Offline tests for the --net allowlist proxy (no outbound connection is made)."""
import socket
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dsw_net  # noqa: E402


class NetTest(unittest.TestCase):
    def test_host_allowlist_suffix_match(self):
        allowed = ["pypi.org", "github.com"]
        self.assertTrue(dsw_net.host_allowed("pypi.org", allowed))
        self.assertTrue(dsw_net.host_allowed("files.github.com", allowed))
        self.assertFalse(dsw_net.host_allowed("evilpypi.org", allowed))
        self.assertFalse(dsw_net.host_allowed("pypi.org.evil.com", allowed))

    def test_private_and_specific_routes_are_bad(self):
        dev = dsw_net._default_dev()
        for ip in ("10.0.0.1", "127.0.0.1", "192.168.1.1", "169.254.1.1", "100.64.0.1", "::1", "fd00::1"):
            self.assertEqual(dsw_net.route_class(ip, dev), "bad", ip)
        for network in dsw_net.specific_routes():
            if network.version == 4 and network.prefixlen < 32:
                self.assertEqual(dsw_net.route_class(str(network.network_address + 1), dev), "bad", str(network))

    def test_socket_path_matches_wrapper_formula(self):
        path = dsw_net.socket_path("/tmp")
        self.assertEqual(path.parent.name, "dsw")
        self.assertEqual(len(path.stem), 16)

    def proxy_request(self, request: bytes, mode: str = "allowlist") -> bytes:
        workspace = tempfile.mkdtemp(prefix="dsw-net-")
        log = Path(workspace) / "proxy.log"
        proxy = dsw_net.AllowlistProxy(workspace, log, extra=[], mode=mode)
        proxy.start()
        try:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(str(proxy.path))
            client.sendall(request)
            reply = client.recv(4096)
            client.close()
            return reply
        finally:
            proxy.stop()
            self.assertFalse(proxy.path.exists(), "socket left behind")

    def test_proxy_denies_unlisted_host_wrong_port_and_plain_http(self):
        self.assertIn(b"403", self.proxy_request(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com\r\n\r\n"))
        self.assertIn(b"only port 443", self.proxy_request(b"CONNECT pypi.org:22 HTTP/1.1\r\n\r\n"))
        self.assertIn(b"403", self.proxy_request(b"GET http://pypi.org/ HTTP/1.1\r\nHost: pypi.org\r\n\r\n"))


    def test_public_mode_still_blocks_internal_ports_and_plain_http(self):
        for target in (b"10.0.0.1:443", b"127.0.0.1:443", b"localhost:443", b"192.168.1.1:443"):
            reply = self.proxy_request(b"CONNECT " + target + b" HTTP/1.1\r\n\r\n", mode="public")
            self.assertIn(b"403", reply, target)
        self.assertIn(b"only port 443", self.proxy_request(b"CONNECT example.com:22 HTTP/1.1\r\n\r\n", mode="public"))
        self.assertIn(b"403", self.proxy_request(b"GET http://example.com/ HTTP/1.1\r\n\r\n", mode="public"))
        with self.assertRaises(ValueError):
            dsw_net.AllowlistProxy("/tmp", Path("/dev/null"), mode="open")


if __name__ == "__main__":
    unittest.main()
