# mindie-agent-codex 0.1.11

本版本修复远端工具缺参时模型只能看到 `Remote caller; submission=not_sent`、无法判断如何纠正的问题。共享 remote-dev 0.10.8 提供有界的 `input_repair`；Codex 同时在结构化错误和模型可见文本中保留缺少的字段与纠正方法。

## 参数与错误契约

直接 SSH 端点需要同时提供 `host` 和 `port`，例如 `{"host":"192.0.2.10","port":22}`；22 只是示例，不是隐式默认端口。也可以使用配置的 alias 或已注册 resolver。使用已配置默认端点时应省略端点选择字段；只提供 host 不会继承默认端点。工具描述与 host/port 参数说明均包含这一规则，避免仅藏在宿主可能不展示的顶层 JSON Schema 描述里。

本地参数校验失败返回 `category=caller`、`submission_state=not_sent`、`automatic_retry=false`，以及 `input_repair.missing` 和 `input_repair.action`。例如漏传 port 时，文本明确包含 `Missing input: port`。Agent 可依据反馈发起补齐参数的新调用；适配器不会自动重放请求。只有共享组件确认的本地缺参错误获得该提示，传输失败及副作用不确定的请求继续保留原分类和任务引用，任意异常文本不会被直接公开。

## 验证范围

在 macOS 的真实 Codex CLI `0.164.0-alpha.11.1` 中，以隔离配置启动候选生产 MCP 和真实模型调用，首次故意省略 port。模型收到上述错误后，第二次调用仅增加 `port=22`，通过真实 A3 SSH 执行只读 printf；原始工具结果确认 `exit_code=0` 与唯一 stdout 标记，整轮 34.672 秒。未手工构造宿主身份，也未使用本地 SSH 替代工具调用。共享组件代码与正式 remote-dev `4b1c5ca` 的源码树一致。

合同回归覆盖真实 resolve_endpoint/call_tool 缺参链路、补齐后的新调用、异常隐私文本不外泄及目录导出。三系统 CI 和正式 Release 流水线分别验证最终提交；发布附件给出该 tag 的测试与微基准报告。此处的候选源码实测不代表用户现有宿主已加载新插件；正式发布后的正常发现、安装、运行时激活与宿主加载需分别读回。

## 历史原因

对比 Codex 0.1.0、0.1.2、0.1.8、0.1.9、0.1.10，host/port 的属性说明缺失及简化错误文本一直存在。实际已安装 remote-dev 0.10.6 与 0.10.7 对同一漏 port 输入均拒绝；历史调用也曾触发同类失败。因此本次修复的是既有的参数可发现性和错误投影缺口，没有新增强制端口规则。此前成功验收显式传入 port，未覆盖 agent 根据缺参错误自行纠正的路径。

## 运行时依赖

| 包 | 版本 | Commit |
| --- | --- | --- |
| remote-dev | 0.10.8 | `4b1c5cab5d36cdaff79d27e4288c6d5f60667c72` |
| mindie-diagnostics | 0.5.4 | `5ae643e19223f2181ecdbaa3edaf464137c4805e` |
| mindie-knowledge | 0.13.4 | `6e921b205c00d592c25f849653cbfb72b73fe318` |

继续复用 0.1.10 的空闲升级、原子绑定和稳定 Hook 入口。知识授权仍按会话由手动 /mindie-agent 启用。
