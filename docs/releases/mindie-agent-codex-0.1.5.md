# mindie-agent-codex 0.1.5

**中文** · [English](mindie-agent-codex-0.1.5.en.md)

修复自动升级在继承独立更新组件时的隔离进程启动失败：`bootstrap.py` 用 `python -I` 启动 `component_update.py`，后者此前未初始化自身目录的模块搜索路径，导致 `ModuleNotFoundError: No module named 'bounded_process'`，插件文件已安装但运行时无法激活。本版本显式绑定候选脚本目录，保留 Python 隔离模式，并为该子进程传入 `-B`。

候选运行时校验现在实际启动隔离的组件更新 CLI，使用无网络、无构建、无模型的 `--help` 路径验证导入边界。回归测试覆盖 Unicode/空格安装路径、环境 `PYTHONPATH` 干扰和缺失候选依赖的负对照。更新相关合同测试继续覆盖失败后的当前绑定保留、忙碌服务延后切换、取消、并发检查及原生 Hook 信任继承。

运行时组件继续锁定正式发布的 `knowledge 0.13.4`、`remote-dev 0.10.6` 和 `diagnostics 0.5.2`。完整组件测试与发布基准由本 Tag 的流水线交付；其他设备的原生自动更新验收单独确认。

已卡在旧版待激活候选的设备可能仍需一次恢复：旧更新器会优先恢复同一候选，其本地执行代码不会因新 Release 发布而自动改变。不要把插件目录中的版本号视为运行时已完成激活。
