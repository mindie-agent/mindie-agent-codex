# mindie-agent-codex 0.1.23

[English](mindie-agent-codex-0.1.23.en.md)

修复 Windows 旧安装升级时，在默认原生插件数据目录中继承独立组件版本的构建失败。深层 generation 目录叠加 setuptools 的 `build/bdist.../wheel` 中间路径，可能超过 Windows 相对路径限制；失败时旧运行绑定仍保留。

插件改用 Hatchling 将同一份 Python、MCP 目录、Hook、Skill、资产和锁定清单直接打包，避免在源码下创建该中间目录。真实原生升级回归使用 `CODEX_HOME/plugins/data/mindie-agent-mindie-agent` 的目录深度，不再用较短的检出目录代替。

本版本包含 0.1.22 的四个 `remote_probe` 参数与共享源码/解释器指引，并继续锁定正式 remote-dev 0.11.7 与 coordinator 0.7.6。插件生命周期、MCP 超时、用户授权、组件版本下界和失败结果的语义保持一致。

发行资产、精确 Tag 测试与 benchmark 由正式工作流核验。原生重编译、动态库加载及 A2/A3 NPU Canary 不属于本补丁的已完成验收。
