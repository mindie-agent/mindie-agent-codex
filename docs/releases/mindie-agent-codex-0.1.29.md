# mindie-agent-codex 0.1.29：Windows 诊断与企业网络修复

[English](mindie-agent-codex-0.1.29.en.md)

修复 #113 涉及的 Windows 扩展路径、SQLite URI 与保留清理预算问题，锁定正式 diagnostics 0.5.10 与 knowledge 0.13.15。生成的诊断运行时采用短路径，已有授权、日志、游标和待发送队列继续保留；身份核验通过的存活降级 worker 可以升级至健康新代。

插件诊断维护使用 15 秒内层预算与 20 秒外层截止，覆盖 5 秒清理及 SQLite 的最低等待，不再用宿主较短截止打断合法维护。企业网络的 Git 代理、CA/TLS 配置在同步及后台上下文中保持，并清除撤销设置。

GitHub 登录说明明确区分 DNS、TCP、代理、TLS、HTTP 与账号权限；TCP 连接失败不要求反复登录，浏览器能打开 github.com 也不代表后台能访问 api.github.com。仍需在用户现场确认具体网络阻断原因。

正式发布流水线验证多平台测试、静态目录一致性、精确源码 benchmark、发行资产与原生旧插件真实升级。已加载的宿主定义按原生刷新边界生效，保留活动会话。
