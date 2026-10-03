import importlib.util
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("vpn_sync", ROOT / "scripts" / "vpn_sync.py")
vpn = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(vpn)
LOG = logging.getLogger("vpn_sync_test")
LOG.addHandler(logging.NullHandler())


def config():
    cfg = json.loads((ROOT / "examples" / "vpn-sync.clash.example.json").read_text(encoding="utf-8"))
    cfg["enabled"] = True
    return cfg


class FakeAdapter:
    def __init__(self):
        self.campus = self.healthy = False
        self.command_ok = self.connect_ok = True
        self.becomes_healthy = False
        self.events = []

    def probe(self, name):
        self.events.append(name)
        return self.campus if name == "campus_probe" else self.healthy

    def command(self, name):
        self.events.append(name)
        return self.command_ok

    def connect(self):
        self.events.append("connect")
        if self.becomes_healthy:
            self.healthy = True
        return self.connect_ok


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config()
        self.adapter = FakeAdapter()
        self.now = 0
        self.coordinator = vpn.Coordinator(self.cfg, self.adapter, LOG, lambda: self.now)

    def test_disabled_never_probes_or_runs_commands(self):
        self.cfg["enabled"] = False
        self.assertEqual(self.coordinator.tick(), "disabled")
        self.assertEqual(self.adapter.events, [])

    def test_offline_threshold_and_prepare_only_once(self):
        for _ in range(4):
            self.assertEqual(self.coordinator.tick(), "waiting_for_campus")
        self.assertEqual(self.adapter.events.count("before_auth"), 1)
        self.assertNotIn("connect", self.adapter.events)
        self.assertNotIn("vpn_probe", self.adapter.events)

    def test_transient_campus_failure_does_not_disconnect(self):
        self.coordinator.tick()
        self.adapter.campus = self.adapter.healthy = True
        self.assertEqual(self.coordinator.tick(), "ready")
        self.assertNotIn("before_auth", self.adapter.events)

    def test_failed_prepare_retries_only_after_cooldown(self):
        self.adapter.command_ok = False
        for _ in range(4):
            self.coordinator.tick()
        self.assertEqual(self.adapter.events.count("before_auth"), 1)
        self.now = 60
        self.coordinator.tick()
        self.assertEqual(self.adapter.events.count("before_auth"), 2)

    def test_recovered_campus_connects_and_verifies_immediately(self):
        self.coordinator.tick()
        self.coordinator.tick()
        self.adapter.campus = self.adapter.becomes_healthy = True
        self.assertEqual(self.coordinator.tick(), "ready")
        self.assertEqual(self.adapter.events[-3:], ["vpn_probe", "connect", "vpn_probe"])
        self.assertFalse(self.coordinator.prepared)

    def test_healthy_vpn_is_left_running(self):
        self.adapter.campus = self.adapter.healthy = True
        for _ in range(3):
            self.assertEqual(self.coordinator.tick(), "ready")
        self.assertNotIn("connect", self.adapter.events)

    def test_vpn_failure_does_not_prepare_campus_authentication(self):
        self.adapter.campus = True
        for _ in range(4):
            self.assertEqual(self.coordinator.tick(), "vpn_unavailable")
        self.assertNotIn("before_auth", self.adapter.events)
        self.assertEqual(self.adapter.events.count("connect"), 1)
        self.now = 60
        self.coordinator.tick()
        self.assertEqual(self.adapter.events.count("connect"), 2)

    def test_successful_command_does_not_imply_healthy_vpn(self):
        self.adapter.campus = True
        self.coordinator.tick()
        self.assertEqual(self.coordinator.tick(), "vpn_unavailable")
        self.assertEqual(self.adapter.events.count("connect"), 1)

    def test_failed_connection_is_not_ready(self):
        self.adapter.campus = True
        self.adapter.connect_ok = False
        self.coordinator.tick()
        self.assertEqual(self.coordinator.tick(), "vpn_unavailable")


class ConfigTests(unittest.TestCase):
    def test_examples_are_disabled_and_valid(self):
        for path in (ROOT / "examples").glob("vpn-sync.*.example.json"):
            with self.subTest(path=path.name):
                cfg = vpn.validate_config(json.loads(path.read_text(encoding="utf-8")))
                self.assertFalse(cfg["enabled"])

    def test_invalid_options_are_rejected(self):
        mutations = [
            lambda c: c.update(enabled="yes"),
            lambda c: c.update(interval_sec=0),
            lambda c: c.update(cooldown_sec=float("nan")),
            lambda c: c.update(offline_threshold=1.5),
            lambda c: c["campus_probe"].update(proxy="http://localhost:7890"),
            lambda c: c["campus_probe"].pop("expected_body"),
            lambda c: c["vpn_probe"].update(proxy="socks5://localhost:7890"),
            lambda c: c["vpn_probe"].update(expected_status=302),
            lambda c: c["vpn_probe"].update(url="https://user:password@example.com/"),
            lambda c: c["commands"]["connect"].update(argv="client --connect"),
            lambda c: c["commands"]["connect"].update(argv=["client", "a\0b"]),
            lambda c: c["commands"]["before_auth"].update(wait=False),
            lambda c: c["clash"].update(enabled=True, controller="http://192.168.1.1:9090"),
            lambda c: c["clash"].update(enabled=True, controller="http://localhost:9090/path"),
            lambda c: c["clash"].update(enabled=True, group=""),
        ]
        for mutate in mutations:
            cfg = config()
            mutate(cfg)
            with self.subTest(mutation=mutate), self.assertRaises(vpn.ConfigError):
                vpn.validate_config(cfg)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.seen.append(("GET", self.path, dict(self.headers)))
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.end_headers()
            return
        if self.path == "/204":
            self.send_response(204)
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Login to campus" if self.path == "/portal" else b"Microsoft Connect Test\n")

    def do_PUT(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.seen.append(("PUT", self.path, dict(self.headers), json.loads(body)))
        self.send_response(204)
        self.end_headers()


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.seen = []
        self.origin = "http://127.0.0.1:" + str(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def probe(self, path, **kwargs):
        return {"url": self.origin + path, "expected_status": 200,
                "expected_body": "Microsoft Connect Test", "timeout_sec": 2, **kwargs}

    def test_direct_probe_ignores_environment_proxy(self):
        with patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:1"}):
            self.assertTrue(vpn.http_probe(self.probe("/ok")))

    def test_portal_page_is_not_healthy(self):
        self.assertFalse(vpn.http_probe(self.probe("/portal")))

    def test_redirect_is_not_followed(self):
        self.assertFalse(vpn.http_probe(self.probe("/redirect")))
        self.assertEqual(len(self.server.seen), 1)

    def test_expected_status_is_checked(self):
        self.assertFalse(vpn.http_probe(self.probe("/ok", expected_status=204)))
        self.assertTrue(vpn.http_probe({"url": self.origin + "/204", "expected_status": 204}))

    def test_explicit_proxy_is_used_even_with_no_proxy(self):
        probe = self.probe("/unused", url="http://localhost:1/through-proxy", proxy=self.origin)
        with patch.dict(os.environ, {"no_proxy": "*", "NO_PROXY": "*"}):
            self.assertTrue(vpn.http_probe(probe))
        self.assertEqual(self.server.seen[0][1], "http://localhost:1/through-proxy")

    def test_https_proxy_preserves_origin_host_inside_tunnel(self):
        seen = []
        response = MagicMock()
        response.code = 204
        response.__enter__.return_value.status = 204

        def capture(handler, request):
            seen.append((request.host, request._tunnel_host, request.get_header("Host")))
            return response

        with patch.object(vpn.urllib.request.HTTPSHandler,
                          "https_open", autospec=True, side_effect=capture):
            self.assertTrue(vpn.http_probe({"url": "https://example.com:8443/check",
                                           "proxy": self.origin, "expected_status": 204}))
        self.assertEqual(seen, [(self.origin.split("//")[1], "example.com:8443", "example.com:8443")])

    def test_clash_request_encodes_group_and_uses_env_secret(self):
        cfg = config()
        cfg["clash"].update(enabled=True, controller=self.origin, group="\u4ee3\u7406/a b", node="test-node")
        with patch.dict(os.environ, {"AUTOSZUWEB_CLASH_SECRET": "test-secret"}):
            self.assertTrue(vpn.Adapter(cfg, LOG).select_clash_node())
        method, path, headers, body = self.server.seen[0]
        self.assertEqual((method, path), ("PUT", "/proxies/%E4%BB%A3%E7%90%86%2Fa%20b"))
        self.assertEqual(headers["Authorization"], "Bearer test-secret")
        self.assertEqual(body, {"name": "test-node"})

    def test_missing_clash_secret_does_not_send_request(self):
        cfg = config()
        cfg["clash"].update(enabled=True, controller=self.origin, secret_env="AUTOSZUWEB_MISSING_TEST_SECRET")
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(vpn.Adapter(cfg, LOG).select_clash_node())
        self.assertEqual(self.server.seen, [])

    def test_probe_only_does_not_run_hooks_or_select_node(self):
        cfg = config()
        cfg["campus_probe"] = self.probe("/ok")
        cfg["vpn_probe"] = {"url": self.origin + "/204", "expected_status": 204}
        cfg["commands"]["connect"]["argv"] = ["THIS_COMMAND_MUST_NOT_RUN"]
        cfg["clash"].update(enabled=True, controller=self.origin)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(cfg), encoding="utf-8")
            result = subprocess.run([sys.executable, str(ROOT / "scripts/vpn_sync.py"),
                                     "--config", str(path), "--probe-only"], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {"campus_probe": True, "vpn_probe": True})
        self.assertTrue(all(entry[0] == "GET" for entry in self.server.seen))


class ProcessAndLockTests(unittest.TestCase):
    def test_shell_metacharacters_remain_literal_arguments(self):
        cfg = config()
        cfg["commands"]["connect"] = {"argv": [sys.executable, "-c",
            "import sys; sys.exit(0 if sys.argv[1] == 'a & b; $(x)' else 1)", "a & b; $(x)"]}
        self.assertTrue(vpn.Adapter(cfg, LOG).command("connect"))

    def test_nonzero_exit_and_timeout_are_failures(self):
        cfg = config()
        cfg["commands"]["connect"] = {"argv": [sys.executable, "-c", "raise SystemExit(7)"]}
        adapter = vpn.Adapter(cfg, LOG)
        self.assertFalse(adapter.command("connect"))
        cfg["commands"]["connect"] = {"argv": [sys.executable, "-c", "import time; time.sleep(10)"],
                                       "timeout_sec": 1}
        self.assertFalse(adapter.command("connect"))

    def test_long_running_child_is_not_started_twice(self):
        cfg = config()
        cfg["commands"]["connect"] = {"argv": [sys.executable, "-c", "import time; time.sleep(20)"],
                                       "wait": False}
        adapter = vpn.Adapter(cfg, LOG)
        try:
            self.assertTrue(adapter.command("connect"))
            self.assertTrue(adapter.command("connect"))
            self.assertEqual(len(adapter.children), 1)
        finally:
            for child in adapter.children:
                child.terminate()
                child.wait(timeout=5)

    def test_lock_prevents_duplicate_instance_and_releases_after_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sync.lock"
            with self.assertRaises(RuntimeError):
                with vpn.instance_lock(path):
                    with self.assertRaises(vpn.ConfigError):
                        with vpn.instance_lock(path):
                            self.fail("duplicate lock acquired")
                    raise RuntimeError("test cleanup")
            with vpn.instance_lock(path):
                self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
