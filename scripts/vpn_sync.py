#!/usr/bin/env python3
"""Optional VPN companion; campus authentication remains in AutoSZUWeb.

Python 3.9+, standard library only. Commands are argv arrays, never shell strings.
"""
import argparse
import contextlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


class ConfigError(ValueError):
    pass


def number(value, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(name + " must be a number")
    if not minimum <= value <= maximum:
        raise ConfigError(name + " is out of range")
    return value


def validate_probe(probe, name):
    if not isinstance(probe, dict):
        raise ConfigError(name + " must be an object")
    url = urllib.parse.urlsplit(probe.get("url", ""))
    if url.scheme not in ("http", "https") or not url.hostname or url.username:
        raise ConfigError(name + ".url must be an HTTP(S) URL without credentials")
    status = probe.get("expected_status", 204)
    if isinstance(status, bool) or not isinstance(status, int) or not 200 <= status <= 299:
        raise ConfigError(name + ".expected_status must be a 2xx integer")
    if status == 200 and not isinstance(probe.get("expected_body"), str):
        raise ConfigError(name + " needs expected_body for a 200 response (portal detection)")
    if "expected_body" in probe and not isinstance(probe["expected_body"], str):
        raise ConfigError(name + ".expected_body must be a string")
    number(probe.get("timeout_sec", 8), name + ".timeout_sec", 1, 60)
    proxy = probe.get("proxy", "")
    if not isinstance(proxy, str):
        raise ConfigError(name + ".proxy must be a string")
    if proxy:
        parsed = urllib.parse.urlsplit(proxy)
        if parsed.scheme != "http" or not parsed.hostname or parsed.username:
            raise ConfigError(name + ".proxy must be an HTTP proxy URL without credentials")
    if name == "campus_probe" and proxy:
        raise ConfigError("campus_probe must bypass HTTP proxies")


def validate_config(cfg):
    if not isinstance(cfg, dict) or not isinstance(cfg.get("enabled", False), bool):
        raise ConfigError("configuration must be an object with a boolean enabled")
    for key, default, low, high in (
        ("interval_sec", 10, 1, 3600), ("cooldown_sec", 60, 1, 86400),
        ("settle_sec", 3, 0, 60), ("offline_threshold", 2, 1, 100),
        ("vpn_failure_threshold", 2, 1, 100),
    ):
        value = number(cfg.get(key, default), key, low, high)
        if key.endswith("threshold") and int(value) != value:
            raise ConfigError(key + " must be an integer")
    validate_probe(cfg.get("campus_probe"), "campus_probe")
    validate_probe(cfg.get("vpn_probe"), "vpn_probe")
    commands = cfg.get("commands", {})
    if not isinstance(commands, dict):
        raise ConfigError("commands must be an object")
    for name, command in commands.items():
        if name not in ("before_auth", "connect") or not isinstance(command, dict):
            raise ConfigError("commands only supports before_auth and connect objects")
        argv = command.get("argv", [])
        if not isinstance(argv, list) or any(not isinstance(a, str) or "\0" in a for a in argv):
            raise ConfigError("command argv must be an array of strings")
        if argv and not argv[0]:
            raise ConfigError("command executable must not be empty")
        if not isinstance(command.get("wait", True), bool):
            raise ConfigError("command wait must be a boolean")
        if name == "before_auth" and not command.get("wait", True):
            raise ConfigError("before_auth must finish before campus authentication")
        number(command.get("timeout_sec", 30), name + ".timeout_sec", 1, 300)
    clash = cfg.get("clash", {})
    if not isinstance(clash, dict) or not isinstance(clash.get("enabled", False), bool):
        raise ConfigError("clash must be an object with a boolean enabled")
    if clash.get("enabled", False):
        url = urllib.parse.urlsplit(clash.get("controller", ""))
        if (url.scheme not in ("http", "https") or
                url.hostname not in ("127.0.0.1", "localhost", "::1") or
                url.username or url.path not in ("", "/") or url.query or url.fragment):
            raise ConfigError("Clash controller must be a loopback HTTP(S) origin")
        for key in ("group", "node", "secret_env"):
            if not isinstance(clash.get(key, ""), str):
                raise ConfigError("clash." + key + " must be a string")
        if not clash.get("group") or not clash.get("node"):
            raise ConfigError("Clash selection requires both group and node")
    return cfg


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def make_opener():
    # Ignore environment / OS HTTP proxies. An explicit proxy must NOT bypass
    # localhost destinations via NO_PROXY; the request itself carries the proxy.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


def http_probe(probe):
    request = urllib.request.Request(probe["url"], headers={"User-Agent": "AutoSZUWeb-VPNSync/1"})
    proxy = probe.get("proxy", "")
    if proxy:
        parsed = urllib.parse.urlsplit(proxy)
        request.set_proxy(parsed.netloc, "http")
    try:
        with make_opener().open(request, timeout=probe.get("timeout_sec", 8)) as response:
            if response.status != probe.get("expected_status", 204):
                return False
            if "expected_body" in probe:
                body = response.read(65537)
                if len(body) > 65536:
                    return False
                return body.decode("utf-8", errors="replace").strip() == probe["expected_body"].strip()
            return True
    except (OSError, ValueError, urllib.error.URLError):
        return False


class Adapter:
    def __init__(self, cfg, log):
        self.cfg, self.log = cfg, log
        self.children = []

    def probe(self, name):
        return http_probe(self.cfg[name])

    def command(self, name):
        command = self.cfg.get("commands", {}).get(name, {})
        argv = command.get("argv", [])
        if not argv:
            return True
        self.children = [p for p in self.children if p.poll() is None]
        if name == "connect" and self.children:
            # Do not spawn duplicate long-running clients on each failed probe.
            self.log.info("Connect process is still running; waiting for connectivity")
            return True
        options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                   "stderr": subprocess.DEVNULL, "shell": False}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 0
            options["startupinfo"] = startup
        try:
            if command.get("wait", True):
                result = subprocess.run(argv, timeout=command.get("timeout_sec", 30), **options)
                return result.returncode == 0
            self.children.append(subprocess.Popen(argv, **options))
            return True
        except (OSError, subprocess.TimeoutExpired):
            # Never include arguments / subprocess exception text in logs.
            self.log.warning("%s command failed or timed out", name)
            return False

    def select_clash_node(self):
        clash = self.cfg.get("clash", {})
        if not clash.get("enabled", False):
            return True
        secret_env = clash.get("secret_env", "")
        token = os.environ.get(secret_env, "") if secret_env else ""
        if secret_env and not token:
            self.log.warning("Clash API secret environment variable is missing")
            return False
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        url = clash["controller"].rstrip("/") + "/proxies/" + urllib.parse.quote(clash["group"], safe="")
        request = urllib.request.Request(url, method="PUT", headers=headers,
                                         data=json.dumps({"name": clash["node"]}).encode("utf-8"))
        try:
            with make_opener().open(request, timeout=8) as response:
                return response.status == 204
        except (OSError, ValueError, urllib.error.URLError):
            self.log.warning("Clash node selection failed (response and secret omitted)")
            return False

    def connect(self):
        if not self.command("connect"):
            return False
        time.sleep(self.cfg.get("settle_sec", 3))
        return self.select_clash_node()


class Coordinator:
    def __init__(self, cfg, adapter, log, clock=time.monotonic):
        self.cfg, self.adapter, self.log, self.clock = cfg, adapter, log, clock
        self.offline_count = self.vpn_failures = 0
        self.prepared = False
        self.last_attempt = {"before_auth": float("-inf"), "connect": float("-inf")}
        self.state = None

    def status(self, state):
        if state != self.state:
            self.log.info("State: %s", state)
            self.state = state
        return state

    def allowed(self, action):
        return self.clock() - self.last_attempt[action] >= self.cfg.get("cooldown_sec", 60)

    def tick(self):
        if not self.cfg.get("enabled", False):
            return self.status("disabled")
        if not self.adapter.probe("campus_probe"):
            self.offline_count += 1
            self.vpn_failures = 0
            if (self.offline_count >= self.cfg.get("offline_threshold", 2) and
                    not self.prepared and self.allowed("before_auth")):
                self.last_attempt["before_auth"] = self.clock()
                self.prepared = self.adapter.command("before_auth")
            return self.status("waiting_for_campus")
        was_offline = self.offline_count >= self.cfg.get("offline_threshold", 2)
        self.offline_count = 0
        if self.adapter.probe("vpn_probe"):
            self.vpn_failures = 0
            self.prepared = False
            return self.status("ready")
        self.vpn_failures += 1
        if (was_offline or self.prepared or
                self.vpn_failures >= self.cfg.get("vpn_failure_threshold", 2)) and self.allowed("connect"):
            self.last_attempt["connect"] = self.clock()
            if self.adapter.connect() and self.adapter.probe("vpn_probe"):
                self.vpn_failures = 0
                self.prepared = False
                return self.status("ready")
        return self.status("vpn_unavailable")


@contextlib.contextmanager
def instance_lock(path):
    # Keep the lock file: unlinking it creates a race between old and new inodes.
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        try:
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ConfigError("Another VPN companion is already using this config directory")
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--check-config", action="store_true", help="Validate only; no network or commands")
    parser.add_argument("--probe-only", action="store_true", help="Check connectivity once; no commands/API writes")
    args = parser.parse_args()
    try:
        path = args.config.resolve()
        cfg = validate_config(json.loads(path.read_text(encoding="utf-8-sig")))
        if args.check_config:
            print("Configuration OK; VPN sync " + ("enabled" if cfg.get("enabled", False) else "disabled"))
            return 0
        if args.probe_only:
            results = {name: http_probe(cfg[name]) for name in ("campus_probe", "vpn_probe")}
            print(json.dumps(results))
            return 0 if all(results.values()) else 1
        if not cfg.get("enabled", False):
            print("VPN sync is disabled. Set enabled=true in your local config to opt in.")
            return 0
        logs = path.parent / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        log = logging.getLogger("vpn_sync")
        log.setLevel(logging.INFO)
        handler = RotatingFileHandler(logs / "vpn_sync.log", maxBytes=1024 * 1024,
                                      backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(handler)
        with instance_lock(logs / "vpn_sync.lock"):
            coordinator = Coordinator(cfg, Adapter(cfg, log), log)
            while True:
                coordinator.tick()
                time.sleep(cfg.get("interval_sec", 10))
    except ConfigError as error:
        print("Configuration error: " + str(error), file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError, AttributeError):
        # Config contents can contain private paths or credentials: do not echo.
        print("VPN sync could not start. Check JSON, field types and whether another instance is running.", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
