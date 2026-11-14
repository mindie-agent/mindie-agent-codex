# mindie-agent-codex 0.1.32

[English](mindie-agent-codex-0.1.32.en.md)

- **无控制台 Windows PowerShell 5.1 Hook 编码与无弹窗加固（`native_entry.py`、`hooks/hooks.json`）**：在 `CREATE_NO_WINDOW` 无控制台句柄场景（如中文 OEM `CP936`）下改用 `System.Diagnostics.Process`（`CreateNoWindow=$true` + 原始字节流 UTF-8 读写）启动子进程并规避 `WindowsApps` 占位别名，消除子进程控制台闪烁、`ErrorRecord` 包装污染与中文 `systemMessage` 乱码，同时对齐 800 字符错误摘要截断与 `WindowsPowerShell 5.1` 专项回归覆盖。
- **WSL2 `/mnt/<drive>` 跨环境二进制遮蔽防护与更新传输层异常分型对齐（`native_context.py`、`bootstrap.py`、`component_update.py`、`harness_update.py`）**：在 Linux/WSL2 下过滤 `/mnt/<drive>` DrvFS 与 PE `.exe` 宿主二进制遮蔽，并将 TLS 中途断流（`ssl.SSLError`）、证书校验失败（`SSLCertVerificationError`）、`http.client.IncompleteRead`/`RemoteDisconnected` 与 DNS 解析异常精确归类为非产品故障的传输/凭据分类。
