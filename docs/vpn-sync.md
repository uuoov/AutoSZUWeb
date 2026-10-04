# 校园网与 VPN / 代理联动

不熟悉代理、端口或命令行的 Windows 用户，请先看 **[Windows 新手指南](beginner-windows.md)**。新手包可双击运行并自动识别可用本机代理；下面是需要自动连接命令、API 选节点或隧道适配时使用的高级说明。

本 fork 保留 [ATMLuck/AutoSZUWeb](https://github.com/ATMLuck/AutoSZUWeb) 的校园网认证功能，增加 `scripts/vpn_sync.py` 作为独立、可选的联动脚本。原作者的 MIT 许可证和版权声明保留不变。

自动重连的触发条件、执行顺序、失败重试和客户端接入方式，见 **[自动重连逻辑说明](auto-reconnect.md)**。其中明确区分了高级脚本已有能力与新手版尚未接入的自动重连功能。

它能在校园网恢复后尝试连接 VPN，并持续检查 VPN 是否可用。你仍需安装 VPN 客户端、配置自己的账号或订阅，先手动连接成功一次。

## 支持哪些客户端

| 类型 | 接入方式 | 范围 |
|---|---|---|
| Clash / Mihomo 及其兼容客户端 | HTTP / mixed 代理端口 + 可选本地 API | 检查代理访问是否正常；重连时可选择指定节点；可配置启动客户端命令 |
| 其他提供 HTTP 代理的客户端 | HTTP 代理端口 + 连接命令 | 不要求 Clash API；客户端需要提供命令行、脚本或已配置的自动连接能力 |
| OpenVPN、WireGuard、系统 VPN 等隧道客户端 | 客户端自己的连接/断开命令 + VPN 专用探测网址 | 需按平台和客户端设置命令、权限与路由；没有统一的连接命令 |
| 只有界面按钮的客户端，如部分版本的肥猫云 | 外部客户端适配脚本 | 单纯启动 EXE 不等于连接；本 fork 不内置鼠标点击适配 |

**通用配置接口不代表任意客户端都能零配置自动连接。** 脚本原生支持 HTTP / mixed 代理，不支持 SOCKS-only 端口；Clash 请使用 HTTP 或 mixed 端口。

## 工作方式

```text
AutoSZUWeb C++ 程序：检测校园网 → SRun / ePortal 自动认证

VPN 联动脚本：
  校园网探测连续失败
    → 可选运行 before_auth 命令，临时断开 VPN
    → 等待 AutoSZUWeb 恢复校园网
  校园网可用
    → 检查 VPN 专用网址（HTTP 代理模式下强制经过指定代理）
    → VPN 不可用：运行 connect 命令，可选调用 Clash API 选节点
    → 再次探测；成功才记录 ready
```

两个程序独立运行。`before_auth` 是断网后的准备操作，**不是 C++ 认证函数的同步回调**；认证程序可能已在尝试登录，解除 VPN 干扰后由其下一轮重试恢复。脚本通过联网探测确认基础网络恢复，不读取校园网账号密码，也不重复实现认证协议。

校园网和 VPN 故障分别处理：VPN 探测失败不会触发脚本重新认证校园网。校园网连续失败才运行准备命令，避免瞬间抖动造成反复断开；连接操作有冷却时间，日志有轮转，同一配置目录有单实例锁。

脚本启动后持续维护 VPN 连接。若想主动关闭 VPN，请先停止脚本，或将 `enabled` 改为 `false` 后重启脚本。

## 1. 先运行校园网程序

按 [主 README](../README.md#windows-使用) 配置和启动 AutoSZUWeb，确认能够独立完成校园网认证。联动脚本不会自动启动或下载 AutoSZUWeb，也不会接管它的自启动设置。

原作者下载站的 EXE 只包含校园网功能；新增功能需要同时运行本 fork 的 Python 脚本。

## 2. 选择并复制示例

要求 Python 3.9 或更新版本，无需 `pip install`。

Windows PowerShell，在仓库目录执行：

```powershell
Copy-Item .\examples\vpn-sync.clash.example.json .\vpn-sync.local.json
```

其他客户端改用 `vpn-sync.generic.example.json`。macOS 使用：

```bash
cp examples/vpn-sync.clash.example.json vpn-sync.local.json
```

编辑 `vpn-sync.local.json`。该文件已被 Git 忽略，不要上传私人配置。

## 3. Clash / Mihomo 配置

先在客户端中确认 HTTP / mixed 端口，例如 `7890`，填入：

```json
"vpn_probe": {
  "url": "https://www.gstatic.com/generate_204",
  "proxy": "http://127.0.0.1:7890",
  "expected_status": 204,
  "timeout_sec": 8
}
```

这会实际通过代理访问网址，不能仅凭端口监听或 `/version` 响应判断节点可用。按你的网络选择稳定的测试网址；返回 200 时还必须设置 `expected_body`，脚本会去除首尾空白后严格比对正文，避免把认证页当成正常联网。

### 自动启动客户端（可选）

客户端需事先保存“启动后自动连接”或“系统代理/TUN”的设置。如果仅需监测一个已由客户端自启动的代理，可让 `connect.argv` 保持为空；这时不负责启动或重启客户端。

要由脚本启动客户端，改成实际路径，例如：

```json
"connect": {
  "argv": ["C:/Program Files/YourClashClient/YourClashClient.exe"],
  "wait": false,
  "timeout_sec": 30
}
```

`wait: false` 适合常驻的 GUI 客户端；`wait: true` 适合执行后退出的连接命令。脚本会隐藏启动窗口，但客户端自身仍可能弹窗；保留自身会话设置由客户端决定。命令必须能重复执行，且重复执行不会切换成“断开”。脚本会避免重复启动由自己启动且仍存活的进程，但不会猜测或结束其他进程。对于原本已启动的客户端，请用客户端提供的幂等连接命令或进程检测脚本。

### 通过本地 API 选择节点（可选）

确认客户端已提供本地控制接口及其端口、密钥，然后将 `clash.enabled` 设为 `true`，填写真实的组名和节点名：

```json
"clash": {
  "enabled": true,
  "controller": "http://127.0.0.1:9090",
  "secret_env": "AUTOSZUWEB_CLASH_SECRET",
  "group": "PROXY",
  "node": "你的节点名称"
}
```

API 密钥从启动脚本的环境变量读取，不写入示例或日志。Windows 可在同一 PowerShell 窗口通过隐藏输入设置：

```powershell
$vpnSecret = Read-Host 'Clash API 密钥' -AsSecureString
$env:AUTOSZUWEB_CLASH_SECRET = [System.Net.NetworkCredential]::new('', $vpnSecret).Password
```

macOS：

```bash
read -s AUTOSZUWEB_CLASH_SECRET
export AUTOSZUWEB_CLASH_SECRET
```

后台自启动时，应在自己维护的启动脚本中从系统凭据库设置该环境变量；当前终端的临时环境变量不会自动传给下一次登录。若客户端未启用 API 密钥，可将 `secret_env` 设为空字符串；控制接口仍应只监听本机。

脚本只允许 loopback 控制接口；调用 `PUT /proxies/{组名}`，正确编码中文和特殊字符。它在重连尝试时选节点，不会在已经健康时强制更换节点，也不会修改订阅、全局模式、TUN 或 Windows 系统代理设置。

接口依据：[Mihomo API 文档](https://wiki.metacubex.one/api/#proxiesproxies_name)。部分客户端不开放这个 API，使用通用命令方式即可。

## 4. 其他 VPN：配置自己的命令

使用 generic 示例，保持 `clash.enabled: false`。填写客户端的连接命令：

```json
"commands": {
  "before_auth": {"argv": [], "wait": true, "timeout_sec": 30},
  "connect": {
    "argv": ["C:/Tools/my-vpn-adapter.exe", "connect"],
    "wait": true,
    "timeout_sec": 30
  }
}
```

`my-vpn-adapter.exe` 只是占位示例，需要替换成真实客户端命令或你编写的适配器。脚本不会提供或下载这个程序。需要通过 Python 适配器时，可以配置 `["C:/路径/python.exe", "C:/Tools/my_vpn_adapter.py", "connect"]`。路径使用绝对路径；每个参数单独一个数组元素，不要额外加引号。

当 VPN 的路由阻碍校园认证时，可以填入 `before_auth.argv` 调用客户端的断开命令。正常完成返回 0；脚本在校园网恢复前不会调用 connect。准备命令失败会按冷却时间重试。

命令不通过 shell 执行。`.ps1` 应显式调用 `powershell.exe -File`，`.sh` 应显式调用其解释器；不要把密码放在命令行里。需要管理员权限、首次授权、验证码或 MFA 的客户端，仍须完成其正常授权流程，脚本不代替这些步骤。

对于不提供 HTTP 代理端口的全隧道 VPN，删除 `vpn_probe.proxy`，将探测网址换成**只有连接该 VPN 后才可访问**的内网服务，并配置真实的状态码及正文。公共网址在 VPN 断开时也能访问，不能证明隧道已连接。客户端若只能点击界面按钮，需要另外开发适配器。

## 5. 先检查，再启用

```powershell
python .\scripts\vpn_sync.py --config .\vpn-sync.local.json --check-config
python .\scripts\vpn_sync.py --config .\vpn-sync.local.json --probe-only
```

`--check-config` 只验证格式；`--probe-only` 只访问探测网址，不执行命令、不写 Clash API。手动连接校园网和 VPN 后，两个探测都应该为 `true`。

确认配置后，将最外层的 `enabled` 设为 `true`，启动：

```powershell
python .\scripts\vpn_sync.py --config .\vpn-sync.local.json
```

macOS 将 `python` 换成 `python3`。前台运行可用 Ctrl+C 停止；停止脚本不会关闭 VPN 客户端，也不会删除配置。

日志位于配置文件旁的 `logs/vpn_sync.log`：

| 状态 | 含义 |
|---|---|
| `waiting_for_campus` | 基础网络尚未通过探测，等待校园网恢复 |
| `vpn_unavailable` | 基础网络可用，VPN 探测未通过；可能在等待客户端或重试冷却 |
| `ready` | 基础网络与 VPN 专用探测均已通过 |

网络结果代表指定测试网址的可用性，不保证所有网站都可访问。日志不记录账号、密码、API 密钥、命令参数或完整响应正文。

## TUN / 全局路由特别注意

绕过 HTTP 代理**不会绕过 TUN、VPN 路由或 DNS 接管**。请在 VPN 客户端中让这些地址及其当前解析的内网 IP 保持直连：

- `net.szu.edu.cn`、宿舍认证地址 `172.30.255.42`；
- 校园网检测网址 `www.msftconnecttest.com`（或你的替代探测域名）；
- 校园网认证实际使用的其他内网地址。

规则和 TUN 路由排除方式按你的客户端文档配置。若仍无法认证，配置客户端的 `before_auth` 断开操作。脚本不自动改路由、关闭保护开关或结束进程。

## 登录后自动运行

先保持原 AutoSZUWeb 自启动正常。联动脚本的自启动需要另行设置：

- Windows：任务计划程序创建“当前用户登录时”任务，程序填实际 `pythonw.exe` 路径，参数填 `"仓库绝对路径/scripts/vpn_sync.py" --config "配置文件绝对路径/vpn-sync.local.json"`。使用需要桌面会话的适配器时，选择“只在用户登录时运行”。
- macOS：创建用户级 LaunchAgent，`ProgramArguments` 依次填写 `python3` 的绝对路径、脚本绝对路径、`--config`、配置绝对路径。

这里不自动安装自启动任务。移动仓库后需要更新路径；可将配置放在自己的固定目录，继续使用 `--config` 指向它。检查日志确认脚本启动成功。

## 验证范围

新增脚本有状态机、配置校验、代理转发、API 编码与认证、超时处理和单实例锁的自动化测试：

```bash
python -m unittest discover -s tests -p "test_vpn_sync.py" -v
```

测试使用本地模拟服务与进程，不依赖校园网、不使用真实 VPN 账号。Windows/macOS 的自动化测试由独立工作流执行。实际客户端按钮、VPN 权限、节点可用性和 TUN 路由仍需在对应环境验证。

### 2026-10-03 Windows 实机验证

在现有校园网和肥猫云代理可用的 Windows 电脑上，使用已安装的 Mihomo v1.19.17 创建独立测试实例。测试实例通过现有 HTTP 代理访问外网，不接管系统代理或 TUN，不改变原客户端配置。

已实际验证：

- 校园网基础连通性、现有肥猫云 HTTPS 代理访问；
- 脚本识别测试代理未运行，并通过真实客户端命令启动 Mihomo；
- 使用真实 Clash API 选择代理节点，读取 API 确认选中结果；
- 通过新启动的 Mihomo 代理访问外网，探测通过后进入 `ready`；
- 停止这个独立测试进程后，脚本识别故障、自动重新启动、重新选节点并恢复访问；
- 结束后清理测试进程与临时配置，原校园网、原 VPN 和原网络守护进程仍正常。

实测修复了 HTTPS 代理请求的 `Host` 问题：手动设置代理地址时，应保留目标站点的 Host，不能将本地代理地址作为隧道内的 Host。新增对应回归测试，自动化测试共 24 项。

这证明了真实 Mihomo 启动、API 选节点、HTTP 代理访问和进程退出后的恢复。**尚未实测校园网注销/掉线后的重新认证、其他 VPN 客户端、GUI 点击或 macOS 真机 VPN 连接。** 自动化状态机测试不替代这些现场验证。

## 同步原作者后续更新

在自己的 fork 本地副本中添加原仓库为上游（已有 `upstream` 时跳过第一行）：

```bash
git remote add upstream https://github.com/ATMLuck/AutoSZUWeb.git
git fetch upstream
git merge upstream/main
```

合并后重新运行上述 Python 测试；涉及原 C++ 程序的更新还应运行主 README 的构建和测试。处理完冲突、确认配置示例与指南仍匹配后，再推送自己的分支。保留许可证及原作者来源说明。
