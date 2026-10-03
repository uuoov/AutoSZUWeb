#!/usr/bin/env python3
"""Windows beginner interface. Detection is read-only; no VPN credentials needed."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import socket
import subprocess
import sys
import threading
import urllib.parse

from vpn_sync import Adapter, ConfigError, Coordinator, http_probe, instance_lock, validate_config


COMMON_PORTS = (7890, 7891, 7892, 7897, 7898, 1080, 10809, 2080)
CLIENT_NAMES = ("clash", "mihomo", "fcclient", "v2ray", "v2rayn", "sing-box", "singbox", "hiddify")
PROBE_URL = "https://www.gstatic.com/generate_204"


@dataclass(frozen=True)
class Candidate:
    proxy: str
    label: str


def local_proxy(value):
    """Accept local HTTP endpoints only; never probe arbitrary remote services."""
    try:
        parsed = urllib.parse.urlsplit(value if "://" in value else "http://" + value)
        if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment
                or not parsed.port or not 1 <= parsed.port <= 65535):
            return None
        host = "[::1]" if parsed.hostname == "::1" else "127.0.0.1"
        return "http://{}:{}".format(host, parsed.port)
    except (ValueError, TypeError, AttributeError):
        return None


def system_candidates():
    if os.name != "nt":
        return []
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
            if not winreg.QueryValueEx(key, "ProxyEnable")[0]:
                return []
            value = winreg.QueryValueEx(key, "ProxyServer")[0]
        parts = value.split(";")
        preferred = []
        for part in parts:
            if "=" in part:
                protocol, endpoint = part.split("=", 1)
                if protocol.lower() not in ("http", "https"):
                    continue
            else:
                endpoint = part
            proxy = local_proxy(endpoint.strip())
            if proxy:
                preferred.append(Candidate(proxy, "系统当前使用的代理"))
        return preferred
    except (OSError, AttributeError, TypeError):
        return []


def client_label(name):
    name = name.lower()
    if "fcclient" in name:
        return "肥猫云"
    if "clash" in name or "mihomo" in name:
        return "Clash / Mihomo"
    return "已运行的 VPN 客户端"


def listener_candidates(records):
    result = []
    for record in records:
        if not isinstance(record, dict):
            continue
        name = str(record.get("Name", "")).lower()
        address = record.get("Address")
        port = record.get("Port")
        if (not any(client in name for client in CLIENT_NAMES)
                or address not in ("127.0.0.1", "0.0.0.0", "::", "::1")
                or isinstance(port, bool) or not isinstance(port, int)):
            continue
        host = "[::1]" if address == "::1" else "127.0.0.1"
        proxy = local_proxy("http://{}:{}".format(host, port))
        if proxy:
            result.append(Candidate(proxy, client_label(name)))
    return result


def running_candidates():
    if os.name != "nt":
        return []
    # Collect listener ownership only: no command lines, subscriptions or secrets.
    script = r"""+$ErrorActionPreference = 'Stop'
$names = @{}
Get-Process | ForEach-Object { $names[$_.Id] = $_.ProcessName }
$items = @(Get-NetTCPConnection -State Listen | ForEach-Object {
    $name = $names[[int]$_.OwningProcess]
    if ($name -match 'clash|mihomo|fcclient|v2ray|sing.?box|hiddify') {
        [pscustomobject]@{ Name=$name; Port=[int]$_.LocalPort; Address=$_.LocalAddress }
    }
})
ConvertTo-Json -InputObject $items -Compress
"""
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode:
            return []
        records = json.loads(result.stdout.decode("utf-8-sig"))
        return listener_candidates(records if isinstance(records, list) else [])
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []


def detection_probe(proxy):
    return {"url": PROBE_URL, "proxy": proxy, "expected_status": 204, "timeout_sec": 6}


def candidate_works(candidate):
    parsed = urllib.parse.urlsplit(candidate.proxy)
    try:
        with socket.create_connection((parsed.hostname, parsed.port), timeout=0.3):
            pass
    except OSError:
        return False
    # A listening port alone is insufficient. SOCKS/API endpoints fail this test.
    return http_probe(detection_probe(candidate.proxy))


def detect_proxies(sources=None, checker=candidate_works):
    if sources is None:
        sources = system_candidates() + running_candidates()
        sources += [Candidate("http://127.0.0.1:{}".format(port), "本机可用代理")
                    for port in COMMON_PORTS]
    unique = {}
    for candidate in sources:
        proxy = local_proxy(candidate.proxy)
        if proxy:
            unique.setdefault(proxy, Candidate(proxy, candidate.label))
    candidates = list(unique.values())[:32]
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(checker, candidates))
    return [candidate for candidate, passed in zip(candidates, results) if passed]


def make_config(candidate):
    proxy = local_proxy(candidate.proxy)
    if not proxy:
        raise ConfigError("A local HTTP proxy is required")
    return validate_config({
        "enabled": True, "interval_sec": 10,
        "campus_probe": {"url": "http://www.msftconnecttest.com/connecttest.txt",
                         "expected_status": 200, "expected_body": "Microsoft Connect Test",
                         "timeout_sec": 6},
        "vpn_probe": detection_probe(proxy),
        "commands": {"connect": {"argv": []}, "before_auth": {"argv": []}},
        "clash": {"enabled": False},
    })


def monitor(candidate, directory, stop, events):
    log = logging.getLogger("vpn_setup")
    handler = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with instance_lock(directory / "logs" / "vpn_sync.lock"):
            cfg = make_config(candidate)
            (directory / "vpn-sync.local.json").write_text(
                json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            handler = RotatingFileHandler(directory / "logs" / "vpn_sync.log",
                                          maxBytes=1024 * 1024, backupCount=3, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            log.setLevel(logging.INFO)
            log.addHandler(handler)
            coordinator = Coordinator(cfg, Adapter(cfg, log), log)
            while not stop.is_set():
                events.put(("status", coordinator.tick()))
                stop.wait(cfg["interval_sec"])
    except ConfigError:
        events.put(("error", "另一个窗口正在监测，请先停止或关闭它。"))
    except (OSError, ValueError):
        events.put(("error", "无法开始监测，请确认当前用户可以保存设置。"))
    finally:
        if handler is not None:
            log.removeHandler(handler)
            handler.close()
        events.put(("stopped", None))


class SetupWindow:
    def __init__(self, root):
        import tkinter as tk
        from tkinter import ttk
        self.root, self.tk, self.ttk = root, tk, ttk
        self.events = queue.Queue()
        self.stop = threading.Event()
        self.worker = None
        self.candidates = []
        self.directory = Path(os.environ["APPDATA"]) / "AutoSZUWebVPNSync"
        root.title("AutoSZUWeb · 连接助手")
        root.geometry("660x560")
        root.minsize(640, 540)
        style = ttk.Style(root)
        style.configure("TLabel", font=("Microsoft YaHei UI", 10))
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=6)
        frame = ttk.Frame(root, padding=24)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="校园网 + VPN，按下面三步开始",
                  font=("Microsoft YaHei UI", 15, "bold")).pack(anchor="w", pady=(0, 16))
        ttk.Label(frame, text="1. 首次使用：设置校园网账号；已设置过可跳过。 ").pack(anchor="w")
        ttk.Button(frame, text="设置 / 启动校园网", command=self.start_campus).pack(anchor="w", pady=(6, 14))
        ttk.Label(frame, text="2. 打开你已有的 VPN 软件，选好线路，点击“连接”。").pack(anchor="w")
        ttk.Label(frame, text="需要先有可用的 VPN 账号或订阅；本工具不提供这些。").pack(anchor="w", pady=(4, 14))
        ttk.Label(frame, text="3. 点击自动检测，成功后点击“开始监测”。不用填写端口。").pack(anchor="w")
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(10, 12))
        self.detect_button = ttk.Button(row, text="自动检测连接", command=self.detect)
        self.detect_button.pack(side="left")
        self.selection = ttk.Combobox(row, state="disabled", width=30)
        self.selection.pack(side="left", padx=10)
        controls = ttk.Frame(frame)
        controls.pack(fill="x")
        self.start_button = ttk.Button(controls, text="开始监测", state="disabled", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(controls, text="停止监测", state="disabled", command=self.stop_monitor)
        self.stop_button.pack(side="left", padx=10)
        self.status = tk.StringVar(value="等待检测。请先在 VPN 软件里连接成功。")
        ttk.Label(frame, textvariable=self.status, wraplength=580).pack(anchor="w", pady=(18, 10))
        ttk.Label(frame, text="此入口只监测连接，不会替你点击 VPN 重连或购买订阅。\n"
                  "保持窗口运行（可以最小化）；关闭窗口会停止本窗口的监测。",
                  wraplength=580).pack(anchor="w")
        ttk.Button(frame, text="找不到连接？查看帮助", command=self.help).pack(anchor="w", pady=(12, 0))
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(150, self.poll)

    def detect(self):
        self.candidates = []
        self.selection.set("")
        self.selection.configure(state="disabled", values=[])
        self.start_button.configure(state="disabled")
        self.detect_button.configure(state="disabled")
        self.status.set("正在查找并测试本机代理，请稍候……")
        def work():
            try:
                self.events.put(("detected", detect_proxies()))
            except Exception:
                self.events.put(("detect_error", None))
        threading.Thread(target=work, daemon=True).start()

    def start(self):
        index = self.selection.current()
        if not 0 <= index < len(self.candidates):
            return
        self.stop.clear()
        self.detect_button.configure(state="disabled")
        self.start_button.configure(state="disabled")
        self.selection.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.status.set("正在检查校园网和所选代理……")
        self.worker = threading.Thread(target=monitor, daemon=True,
                                       args=(self.candidates[index], self.directory, self.stop, self.events))
        self.worker.start()

    def stop_monitor(self):
        self.stop.set()
        self.stop_button.configure(state="disabled")
        self.status.set("正在停止监测，请稍候……")

    def poll(self):
        from tkinter import messagebox
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind in ("detected", "detect_error"):
                self.candidates = value if kind == "detected" else []
                self.detect_button.configure(state="normal")
                if self.candidates:
                    labels = ["{} · 可用连接 {}".format(c.label, i + 1)
                              for i, c in enumerate(self.candidates)]
                    self.selection.configure(state="readonly", values=labels)
                    self.selection.current(0)
                    self.start_button.configure(state="normal")
                    self.status.set("代理访问测试通过。点击“开始监测”。有多个结果时可选择要使用的连接。")
                else:
                    self.start_button.configure(state="disabled")
                    self.selection.configure(state="disabled", values=[])
                    self.selection.set("")
                    self.status.set("没有找到通过测试的代理。请先确认 VPN 已连接，再点检测；"
                                    "仍找不到请查看下方帮助。无需填写端口。")
            elif kind == "status":
                if not self.stop.is_set():
                    self.status.set({
                        "ready": "校园网检测通过，所选代理访问测试通过。正在持续监测。",
                        "waiting_for_campus": "基础网络检测未通过。请检查校园网连接，等待校园网程序恢复。",
                        "vpn_unavailable": "代理访问测试未通过。请到 VPN 软件重新连接或更换线路；本窗口会继续检测。",
                    }.get(value, "正在监测……"))
            elif kind == "error":
                messagebox.showerror("无法开始监测", value, parent=self.root)
            elif kind == "stopped":
                self.worker = None
                self.detect_button.configure(state="normal")
                self.stop_button.configure(state="disabled")
                if self.candidates:
                    self.selection.configure(state="readonly")
                    self.start_button.configure(state="normal")
                self.status.set("监测已停止。VPN 软件和校园网程序仍独立运行。")
        self.root.after(150, self.poll)

    def start_campus(self):
        from tkinter import messagebox
        base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1] / "dist" / "windows"
        executable = base / "AutoSZUWeb.exe"
        if not executable.is_file():
            messagebox.showinfo("缺少校园网程序", "请下载并完整解压 Windows 新手包，"
                                "让 AutoSZUWeb.exe 和本助手放在同一文件夹。", parent=self.root)
            return
        if not messagebox.askokcancel("校园网首次使用", "首次启动会在桌面生成 userdata.txt。\n"
                "打开它：第一行填校园卡号，第二行填统一身份认证密码，保存后再点本按钮。\n"
                "认证结果提示出现后点“确定”。校园网程序会启用自己的登录自启动。\n\n"
                "连接助手本身不会设置自启动。现在启动校园网程序？", parent=self.root):
            return
        try:
            subprocess.Popen([str(executable)], creationflags=subprocess.CREATE_NO_WINDOW,
                             cwd=str(executable.parent))
        except OSError:
            messagebox.showerror("启动失败", "校园网程序未能启动，请确认文件已完整解压。", parent=self.root)

    def help(self):
        from tkinter import messagebox
        messagebox.showinfo("检测不到时怎么做", "1. 先确认 VPN 软件能正常使用，账号 / 订阅已配置。\n"
            "2. 在 Clash 类软件里可尝试打开“系统代理”，再点自动检测。\n"
            "3. 检测网址不可用、仅提供 SOCKS、只有 TUN 或单位内网 VPN 时，"
            "这个入口可能识别不到。找不到不代表 VPN 一定没连接。\n"
            "4. 请向提供软件的人说明客户端名称；高级接入方式见仓库的 VPN 联动指南。\n\n"
            "本工具不自动修改 VPN 设置，也不提供线路。", parent=self.root)

    def close(self):
        self.stop.set()
        self.root.destroy()


def main():
    if sys.argv[1:] == ["--check-connection"]:
        candidates = detect_proxies()
        campus = http_probe(make_config(candidates[0])["campus_probe"]) if candidates else False
        if sys.stdout is not None:
            print(json.dumps({"detected_connections": len(candidates), "campus_probe": campus}))
        return 0 if candidates and campus else 1
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk()
    if os.name != "nt":
        root.withdraw()
        messagebox.showinfo("平台提示", "连接助手目前提供 Windows 版本。其他平台请使用 VPN 联动指南。")
        root.destroy()
        return 1
    try:
        directory = Path(os.environ["APPDATA"]) / "AutoSZUWebVPNSync"
        with instance_lock(directory / "logs" / "window.lock"):
            SetupWindow(root)
            root.mainloop()
        return 0
    except (ConfigError, OSError, KeyError):
        root.withdraw()
        messagebox.showinfo("连接助手", "连接助手可能已经打开，或无法保存设置。请检查已打开的窗口。")
        root.destroy()
        return 1


if __name__ == "__main__":
    sys.exit(main())
