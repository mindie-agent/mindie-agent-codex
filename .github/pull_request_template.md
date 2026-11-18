## 变更摘要 (Summary)

- 简述本次变更解决的问题、影响范围与核心设计取舍：

## 设计原则 Review (Pre-PR Checklist — `host-plugin`)

- [ ] **HOST-NATIVE** — 原生宿主契约保持不变：`hooks/hooks.json` 字节级哈希（`ea80d0815485ba5f762ff5275f1e40817130694870405237dbd65d03f28f9981`）、MCP 服务名（`mindie-remote-dev`、`mindie-tools`）、静默默认启用语义及 Windows 零弹窗（`CREATE_NO_WINDOW`）全部通过校验。
- [ ] **HOST-WIRE** — 薄适配层委托与协议边界：共享锁、RSI 与基础设施逻辑已委托 `mindie-harness`（含 `assets/seed-lock.json` 校验），`.mcp.json` 无内联状态机脚本，模块行数 `< 800` 且具备顶部四段契约注释。
- [ ] **HOST-ADMISSION** — 准入与并发诚实性：全量工具（含 `knowledge_*`）满足零前置会话绑定门禁，只读调用不受 4 槽写并发限制且热路径无 `.owner.lock` 全目录扫描（`AD-09`），`Stop` 未开启贡献或越界时 `< 100ms` 零 SQLite 退出。
- [ ] **HOST-UPGRADE** — 升级/回滚连续性与收据诚实：兼容既有 `codex.json` / `prepare.lock` / `runtime.json` 布局，外部写入区分 `completed` / `failed` / `unknown`，主操作成功后本地记账异常保留 `isError=False`。

### 自查证据 (Evidence)

- **HOST-NATIVE**:
- **HOST-WIRE**:
- **HOST-ADMISSION**:
- **HOST-UPGRADE**:

## 验证结果 (Verification)

- 本地测试、构建冒烟与静态门禁执行命令及结果：
