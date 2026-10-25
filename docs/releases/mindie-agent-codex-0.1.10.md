# mindie-agent-codex 0.1.10

本版本只修复 Codex 空闲入口锁缺陷，组件 pin 保持不变：remote-dev `4c40cb5d302ddb8400ff6abb7dc47873b931eaa9`、mindie-diagnostics `5ae643e19223f2181ecdbaa3edaf464137c4805e`、mindie-knowledge `6e921b205c00d592c25f849653cbfb72b73fe318`。

`entry.py` 的 MCP 判定此前只覆盖 `knowledge-mcp`、`remote-mcp`（以及 `bridge mcp`），遗漏 `tools-mcp`。`tools-mcp` 因此在入口共享 `update_lock` 作用域内启动常驻 serve：即使空闲也会持续持有共享锁，候选激活无法取得独占锁，只能持续返回 `status=deferred, reason=operation_busy`。0.1.10 将 `tools-mcp` 纳入与其他 MCP 入口相同的启动路径：入口完成 profile 校验和绑定后先退出共享锁，再启动 serve；空闲 serve 不持有更新锁。真实的组件工具调用仍由 `Gate.call` 在执行期间持有共享锁，调用结束即释放。

验证使用真实 `entry.py` 子进程、真实安装 profile 和真实 OS 文件锁：在仅将实际常驻 serve 替换为独立进程请求独占锁的 idle dispatch 点，`knowledge-mcp`、`remote-mcp`、`tools-mcp` 均可取得独占 activation 锁；另在真实 `Gate.call` 组件执行期间独占锁被阻止，调用返回后可再次取得。回归不写用户配置，不启动模型或服务器。

升级边界必须保留：本修复只对载入 0.1.10 新代码的入口生效。已经由 0.1.8/0.1.9 启动并常驻的 `tools-mcp` 进程仍执行旧代码，会继续持有共享锁；源代码修复无法让已载入的旧进程自行放锁，也不能热补丁。必须等待这些旧进程自然退出，之后由新入口启动的 `tools-mcp` 才会具备本修复。原生 install 和 runtime prepared 只表示安装与准备完成，不等于 activation 成功或已 active；本版本不宣称既有机器可以零重载热修复。

发布资产、tag、最终激活与对外发布由发布流程另行决定；本说明不把安装或准备完成状态改写为 active。
