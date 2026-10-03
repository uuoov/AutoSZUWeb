AutoSZUWeb Windows 连接助手（预览版）

完整解压这个压缩包，然后双击 AutoSZUWeb-Connect.exe。
不用安装 Python，不用输入命令，也不用知道代理端口。

三步开始：
1. 点“设置 / 启动校园网”。首次启动后打开桌面上的 userdata.txt，
   第一行填校园卡号，第二行填统一身份认证密码；保存后再次点这个按钮。
   认证结果提示出现后点“确定”。已经设置并运行过的用户可以跳过。
2. 打开自己已有的 VPN 软件，选好线路，点击“连接”。
3. 回到助手，点“自动检测连接”，测试通过后点“开始监测”。

本工具不提供 VPN 账号、订阅或线路，请先确保 VPN 软件本身能正常使用。
新手入口持续检测已有代理，访问失败时提醒你到 VPN 软件重新连接或换线路。
自动识别不等于能自动重连所有 VPN；只提供 TUN、SOCKS 或单位内网连接的
客户端，可能需要按高级说明接入。找不到不代表 VPN 一定没连接。

点“停止监测”或关闭窗口，会停止这个窗口的代理监测。
可以最小化窗口。助手不会设置自己的登录自启动，下次请重新打开。
校园网程序独立运行，配置完成后会启用自己的登录自启动。

详细新手步骤：
https://github.com/uuoov/AutoSZUWeb/blob/main/docs/beginner-windows.md

高级接入、自动连接和兼容范围：
https://github.com/uuoov/AutoSZUWeb/blob/main/docs/vpn-sync.md

校园网程序来自 ATMLuck/AutoSZUWeb；本 fork 增加连接助手和联动脚本。
项目按随包 LICENSE 中的 MIT 许可证分发。
