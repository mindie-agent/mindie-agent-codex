# mindie-agent-codex 0.1.25

[English](mindie-agent-codex-0.1.25.en.md)

修复 Codex Desktop `0.162.0-alpha.2` 被兼容声明过高的 `alpha.17.2` 下限排除，导致安装或更新报告 `no_compatible_release` 的问题。顶层声明与全部 12 个已使用接口统一支持精确 `0.153.4` 或 `>=0.162.0-alpha.2 <=0.999.999`（含预发布）。

`0.1.17` 引入的前向区间一直有效，`0.162.3-alpha.17.2` 原本已在范围内。本补丁保留区间选择，增加 45 组后续 patch/minor、alpha/beta/rc/稳定版本的首次安装和已有插件选择回归；区间内的新版本不需要逐个加入名单。

原生 CI 增加 Ubuntu 与 Windows 的官方 `0.162.0-alpha.2`。Windows 隔离验收检查插件安装、5 条 Hook、信任 CAS、保留显式禁用、锁定运行时、SessionStart、23 个远端工具与 6 个组件工具发现，以及组件新代构建、验证和绑定切换。不声称模型轮次或 NPU 工作负载验收。

本版本保留主线 `0.1.24` 的 Hook 启动与激活诊断改进，并复用其正式运行时依赖。本补丁不改动共享组件依赖、MCP 工具目录、权限或主产品组合清单。发布工作流核验精确 Tag 的测试、benchmark、插件元数据和 SHA256SUMS。
