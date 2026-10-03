import logging
import os
from pathlib import Path
import queue
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import vpn_setup as setup


class DetectionTests(unittest.TestCase):
    def test_rejects_remote_socks_credentials_and_bad_endpoints(self):
        for value in ("http://example.com:7890", "socks5://127.0.0.1:7890",
                      "http://u:p@127.0.0.1:7890", "http://127.0.0.1:7890/api",
                      "http://127.0.0.1:7890?token=x", "127.0.0.1:0",
                      "127.0.0.1:99999", "127.0.0.1", None):
            with self.subTest(value=value):
                self.assertIsNone(setup.local_proxy(value))

    def test_normalizes_local_and_ipv6_endpoints(self):
        self.assertEqual(setup.local_proxy("localhost:7892"), "http://127.0.0.1:7892")
        self.assertEqual(setup.local_proxy("http://[::1]:7890"), "http://[::1]:7890")

    def test_accepts_only_known_client_listeners(self):
        records = [
            {"Name": "fcclientCore", "Address": "::", "Port": 7892},
            {"Name": "verge-mihomo", "Address": "::1", "Port": 17890},
            {"Name": "randomserver", "Address": "127.0.0.1", "Port": 9000},
            {"Name": "mihomo", "Address": "192.168.1.1", "Port": 7890},
            {"Name": "mihomo", "Address": "127.0.0.1", "Port": True},
            None,
        ]
        self.assertEqual([c.proxy for c in setup.listener_candidates(records)],
                         ["http://127.0.0.1:7892", "http://[::1]:17890"])

    def test_deduplicates_validates_and_preserves_preferred_label(self):
        seen = []
        def checker(candidate):
            seen.append(candidate.proxy)
            return candidate.proxy.endswith(":7892")
        candidates = [setup.Candidate("localhost:7892", "preferred"),
                      setup.Candidate("http://127.0.0.1:7892", "duplicate"),
                      setup.Candidate("http://127.0.0.1:9090", "controller"),
                      setup.Candidate("http://example.com:7892", "remote")]
        result = setup.detect_proxies(candidates, checker)
        self.assertEqual(result, [setup.Candidate("http://127.0.0.1:7892", "preferred")])
        self.assertCountEqual(seen, ["http://127.0.0.1:7892", "http://127.0.0.1:9090"])

    def test_closed_port_does_not_make_http_request(self):
        with patch.object(setup.socket, "create_connection", side_effect=OSError), \
                patch.object(setup, "http_probe") as probe:
            self.assertFalse(setup.candidate_works(setup.Candidate("http://127.0.0.1:7890", "local")))
            probe.assert_not_called()

    def test_listening_but_failed_http_is_not_accepted(self):
        with patch.object(setup.socket, "create_connection"), \
                patch.object(setup, "http_probe", return_value=False) as probe:
            self.assertFalse(setup.candidate_works(setup.Candidate("http://127.0.0.1:9090", "API")))
            self.assertEqual(probe.call_args.args[0]["expected_status"], 204)


class MonitoringTests(unittest.TestCase):
    def test_generated_configuration_does_not_issue_connect_commands(self):
        config = setup.make_config(setup.Candidate("http://127.0.0.1:7892", "local"))
        self.assertNotIn("proxy", config["campus_probe"])
        self.assertFalse(config["clash"]["enabled"])
        self.assertEqual(config["commands"]["connect"]["argv"], [])
        self.assertEqual(config["commands"]["before_auth"]["argv"], [])

    def test_stop_releases_lock_and_handlers(self):
        events, stop = queue.Queue(), threading.Event()
        def tick():
            stop.set()
            return "ready"
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(setup.Coordinator, "tick", side_effect=tick):
            path = Path(folder)
            setup.monitor(setup.Candidate("http://127.0.0.1:7892", "local"), path, stop, events)
            self.assertEqual(events.get_nowait(), ("status", "ready"))
            self.assertEqual(events.get_nowait(), ("stopped", None))
            with setup.instance_lock(path / "logs" / "vpn_sync.lock"):
                pass
            self.assertEqual(logging.getLogger("vpn_setup").handlers, [])

    def test_locked_directory_is_not_overwritten(self):
        events = queue.Queue()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            settings = path / "vpn-sync.local.json"
            settings.write_text("existing", encoding="utf-8")
            with setup.instance_lock(path / "logs" / "vpn_sync.lock"):
                setup.monitor(setup.Candidate("http://127.0.0.1:7892", "local"),
                              path, threading.Event(), events)
            self.assertEqual(events.get_nowait()[0], "error")
            self.assertEqual(settings.read_text(encoding="utf-8"), "existing")


@unittest.skipUnless(os.name == "nt", "Windows beginner interface")
class InterfaceTests(unittest.TestCase):
    def test_selection_and_buttons_follow_detection_result(self):
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        try:
            window = setup.SetupWindow(root)
            self.assertIn("disabled", window.start_button.state())
            window.events.put(("detected", [setup.Candidate("http://127.0.0.1:7892", "client")]))
            window.poll()
            self.assertEqual(window.selection.current(), 0)
            self.assertNotIn("disabled", window.start_button.state())
            window.events.put(("detected", []))
            window.poll()
            self.assertEqual(window.candidates, [])
            self.assertIn("disabled", window.start_button.state())
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
