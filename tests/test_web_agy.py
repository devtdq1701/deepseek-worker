"""Dashboard `/api/agy`: relays `agy-pane jobs`, degrades when agy-pane is missing or broken."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

DSW = Path(__file__).resolve().parents[1] / "dsw.py"


class WebAgyTest(unittest.TestCase):
    def serve(self, **env):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        home = tempfile.mkdtemp(prefix="dsw-web-agy-")
        server = subprocess.Popen([sys.executable, str(DSW), "serve", "--port", str(port)],
                                  env=dict(os.environ, DSW_HOME=home, PATH="/usr/bin:/bin", **env),
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(server.wait, 10)
        self.addCleanup(server.terminate)
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        return port

    def get(self, port, path="/api/agy"):
        return json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10).read())

    def fake(self, body):
        tool = Path(tempfile.mkdtemp(prefix="dsw-fake-agy-pane-")) / "agy-pane"
        tool.write_text("#!/bin/sh\n" + body)
        tool.chmod(0o755)
        return str(tool)

    def test_relays_agy_pane_jobs(self):
        data = {"running": [{"job": "job-1", "out_tail": "x"}], "recent": [], "slots": {"max": 3, "used": 1},
                "quota": None}
        tool = self.fake(f"[ \"$1 $2 $3\" = 'jobs --tail 60' ] || exit 3\necho '{json.dumps(data)}'\n")
        got = self.get(self.serve(DSW_AGY_PANE=tool))
        self.assertTrue(got.pop("available"))
        self.assertEqual(got, data)

    def test_missing_or_broken_agy_pane_is_reported(self):
        got = self.get(self.serve(DSW_AGY_PANE=""))
        self.assertEqual(got["available"], False)
        self.assertIn("not installed", got["error"])
        got = self.get(self.serve(DSW_AGY_PANE=self.fake("echo not-json\n")))
        self.assertEqual(got["available"], False)

    def test_transcript_route_validates_ref_and_relays(self):
        tool = self.fake('[ "$1 $2" = "transcript job-123456-AbCd" ] && echo \'{"events": [], "steps": 0}\' && exit 0\n'
                         'echo \'{"error": "x"}\'; exit 1\n')
        port = self.serve(DSW_AGY_PANE=tool)
        self.assertEqual(self.get(port, "/api/agy/transcript/job-123456-AbCd"), {"events": [], "steps": 0})
        for ref in ("job-999999-zzzz", "..%2F..%2Fetc", "x;rm"):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.get(port, f"/api/agy/transcript/{ref}")
            self.assertEqual(caught.exception.code, 404, ref)

    def test_page_has_agy_view(self):
        port = self.serve(DSW_AGY_PANE="")
        page = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10).read().decode()
        self.assertIn('id="v-agy"', page)
        self.assertIn("api/agy", page)


if __name__ == "__main__":
    unittest.main()
