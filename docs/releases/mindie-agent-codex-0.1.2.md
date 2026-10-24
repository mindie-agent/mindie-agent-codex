# mindie-agent-codex 0.1.2 Release Notes

**中文** | [English](https://github.com/mindie-agent/mindie-agent-codex/blob/v0.1.2/docs/releases/mindie-agent-codex-0.1.2.en.md)

`mindie-agent-codex 0.1.2` 是面向 OpenAI Codex CLI 的维护更新版本（配套主产品 `mindie-agent 0.1.1`，保留既有 `0.1.1` 插件标签不变），新增共享运行时组件的独立正式 Release 自动更新能力，并收敛冷启动激活与按会话手动开启 `/mindie-agent` 知识循环的边界。

## 核心更新

- **共享组件独立自动更新与单调不降级保护**：
  - 当没有更高版本的插件 Release 需要切换时，后台更新检查直接从各官方组件仓库发现正式 GitHub Release，按“当前已安装版本、插件基线版本、组件最新正式 Release”三者最大值选定目标组件版本，在旁路隔离目录构建带完整 `uv.lock` 的独立运行环境并保留原生插件包（若组件切换暂缓或未完成，状态如实反映 `deferred` / `incomplete`）。
  - 当升级到新版本插件 Release 时，自动继承当前已安装组件的版本下界，防止插件升级将已独立更新的组件回退到旧基线。
- **无模型冷启动校验、原生插件缓存零写入与进程内可取消检查**：
  - 移除候选环境准备阶段启动知识后台服务的探针路径；改用隔离包清单校验、无模型运行时接口探针、空闲服务退役与本地绑定文件（`codex.json`）原子替换，仅在后续已授权的手动知识操作中按需启动后台知识服务。
  - 后台更新触发器（`plugins/mindie-agent/scripts/update_trigger.py`）在现有常驻 MCP 进程内按 `CHECK_INTERVAL = 300`（300 秒）运行可取消等待循环，不新增独立知识 worker 或后台守护进程。
  - 所有源码 Python 启动入口显式传入 `-B`（禁止生成 `.pyc` 字节码文件），且基线 `uv sync` 在既有构建锁（`prepare.lock`）内将打包文件复制到 `PLUGIN_DATA/releases/<version>/build` 短生命副本目录中构建并自动清理，防止构建产物（如 `.pyc`、`build/`、`*.egg-info`）写入 Codex 原生插件缓存目录而破坏宿主 Hook 信任哈希。
- **探索上下文原样透传与会话显式授权边界**：
  - 知识检索的探索载荷直接透传 `mindie-knowledge` 引擎输出，适配层不再二次剥离或改写；软件更新与环境构建绝不自动授予会话知识权限或改变已保存的用户贡献同意，会话知识能力仍仅在当前会话手动输入 `/mindie-agent`（或 `$mindie-agent`）后生效。

## 运行时组件与验证范围

- **配套运行时组件**：锁定正式发布的 `mindie-knowledge 0.13.3`（`38a42f6f778cfa555d8918fdcacd12cee4a181f5`）、`remote-dev 0.10.6`（`0463a13fce9618ec9a51904869d61acb0cdb8538`）与 `mindie-diagnostics 0.5.2`（`801a01b2789aed7f58e971a909205a7d40b61077`）；兼容宿主版本范围（含 `0.153.4`、`0.162.0-alpha.17.2`、`0.164.0-alpha.11.1`）以随包发布的 `plugins/mindie-agent/uv.lock`、`product-contract.json` 与 `harness-compatibility.json` 为准。
- **验证与验收层级**：
  1. **macOS `arm64` 原生候选实测（Codex `0.164` / `0.164.0-alpha.11.1`）**：冷启动暴露 `11` 个 `remote-dev` 工具，默认 `remote_job_status` 直接走确定性远端路径（查询不存在的 `job_id` 正常返回调用方未找到错误，不受知识门禁阻塞）；`5` 个 Hook 保持信任状态、原生插件载荷哈希不变且无 `.pyc` / `build/` / `*.egg-info` 污染。默认新会话直接调用 `knowledge_query` 在执行前被拒绝、不初始化知识服务且不重试；手动触发 `$mindie-agent` / `/mindie-agent` 正常激活 `entry_enabled=1` 并由同会话续跑继承，随后新建的独立会话不继承该授权。组件独立更新实测对齐 `mindie-knowledge 0.13.3`、`remote-dev 0.10.6` 与 `mindie-diagnostics 0.5.2`（保留原生插件包），`codex.json` 绑定切换阶段约 `10ms`（`unavailable_seconds`，不含前置构建）。
  2. **自动化测试与各 OS CI**：原生载荷只读保护与组件更新测试套件（`57 passed, 6 subtests`）、组件更新与候选校验测试（`13 passed, 31 subtests`）、既有更新套件（`44 passed, 8 subtests`）及启动器套件（`24 passed, 2 subtests`）全部通过，跨平台契约由各 OS CI 流水线覆盖。
  3. **正式发布后回读待办**：待四端 `0.1.2` 正式 GitHub Release 发布后，执行面向已发布正式包的本地自动更新回读确认。
