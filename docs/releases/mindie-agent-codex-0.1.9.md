# mindie-agent-codex 0.1.9

本版本固定 remote-dev `2bd3a9e`，并保留大日志的模型可见头尾摘要：命令与 TaskOutput 结果给出有界 UTF-8 头尾、省略说明和可读取的完整远端/本地日志引用；增量/后台页仍按预算原样返回，游标不越过未返回字节。预览读取失败会显式标记 preview degraded，同时保留真实 exit/结果。

自动发现与主仓合并后的 22 个 remote 工具 + 5 个 component 工具保持；Codex 宿主外层超时字段为 720s（覆盖 600s Bash + 60s helper grace + 60s cleanup）。三平台 oversized-output 测试改为在子进程内生成数据，避免 Linux E2BIG/Windows WinError206。

本地/CI 机制验证不代表所有设备已自动升级；正式发布资产与 tag 由发布流程另行生成。
