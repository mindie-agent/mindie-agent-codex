# mindie-agent-codex 0.1.3

**中文** · [English](mindie-agent-codex-0.1.3.en.md)

候选运行时的发布契约校验现在继承 Git 系统和用户配置，保留企业代理、CA 路径及 Windows `schannel` 系统信任设置。校验继续使用临时裸仓库、禁用 Git hooks，并核对精确发布提交。

插件锁定 `mindie-knowledge 0.13.4`，使共享组件独立更新的旁路 uv 构建沿用安装器的系统证书默认策略。`remote-dev 0.10.6` 和 `mindie-diagnostics 0.5.2` 保持原有正式版本。

在 Windows 企业网络中，旧版候选校验的 `publication_fetch:fetch_failed` 已复现，修复后的真实 Git 拉取与发布契约读取通过。原修复通过 Ubuntu、macOS、Windows CI；本版本的精确依赖、完整合同测试和微基准由对应发布流水线验证，报告随 Release 资产交付。

更新继续在后台准备环境并等待空闲切换。已保存的贡献设置和会话手动启用知识能力的边界保持不变。
