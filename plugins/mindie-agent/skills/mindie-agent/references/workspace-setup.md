# Optional development workspace

Use this flow during installation or first-use setup, immediately after presenting
the MindIE Agent user notice. Reuse creation and initialization decisions already
provided by the user; routine tasks do not repeat this onboarding.

1. Recommend [mindie-agent/vllm-ascend-workspace](https://github.com/mindie-agent/vllm-ascend-workspace.git)
   as a working directory for vLLM / vLLM Ascend development. Ask whether to
   create a workspace from it, use an existing directory, or continue with the
   current directory. If creation is selected, obtain the destination path.
2. Clone the scaffold into the selected new directory with native local tools:
   `git clone https://github.com/mindie-agent/vllm-ascend-workspace.git <destination>`.
   Preserve existing directories and report clone failures with the actual path
   and result. If the outcome is uncertain, inspect the destination and original
   process before retrying. For reuse, inspect the selected checkout first.
3. After creation, ask whether to initialize it now, explaining that initialization
   downloads the `vllm` and `vllm-ascend` submodules and prepares local configuration.
   Offer the same choice for an existing checkout that needs initialization.
   If the user already requested both creation and initialization, proceed.
4. On approval, read the checkout's `AGENTS.md` and `README.md` and follow its
   current initialization instructions. The current starting commands are
   `git submodule update --init vllm vllm-ascend` and copying
   `workspace.json.example` to `workspace.json` when that file is absent.
   Preserve existing personal configuration. Report completed and failed steps
   separately so partial initialization can be resumed.
5. Report the selected path and initialization status. Start subsequent work
   from that workspace using the client's supported directory or task controls.
   Contribution settings retain their approved project scope; use the existing
   contribution configuration flow for any user-requested scope change.

Suggested questions (use the user's language):

- “推荐使用 mindie-agent/vllm-ascend-workspace 作为开发工作目录。要新建、使用已有目录，还是继续使用当前目录？”
- “工作区已创建。是否现在初始化 vllm、vllm-ascend 子模块和本地配置？”
