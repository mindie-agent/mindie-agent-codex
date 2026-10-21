# Development workspace setup

Run this flow during normal installation and first-use setup, immediately after
presenting the existing MindIE Agent user notice. The offer is part of setup even
when contribution is disabled. Reuse the user's creation, initialization and
registration decisions; ordinary tasks and updates do not repeat onboarding.

1. Recommend [mindie-agent/vllm-ascend-workspace](https://github.com/mindie-agent/vllm-ascend-workspace.git).
   Offer creating and initializing it, reusing an existing directory, or keeping
   the current directory. Obtain the destination when needed and explain that
   the selected workspace will also be added to the current client's native
   project/workspace entry. An installation choice includes this registration;
   do not ask again whether to register it. If the user keeps the current
   directory without scaffold setup, finish the offer and retain that directory.
2. Before downloads or fork setup, follow [GitHub Sign-In](github-authentication.md).
   Reuse authentication already verified during plugin installation. Otherwise
   offer browser login or token creation/input, preferring tokens on enterprise
   networks. A GitHub username alone does not establish authentication.
3. For a new directory, run
   `git clone https://github.com/mindie-agent/vllm-ascend-workspace.git "<destination>"`.
   For reuse, inspect the selected checkout first. Preserve existing files and
   configuration. Report clone failures with the actual path and result; inspect
   an interrupted clone before retrying it. Keeping the current directory needs
   no scaffold installation or unrelated configuration changes.
4. Once the selected workspace exists, register/open its absolute root through
   the current harness's native project controls. Follow the checkout's
   `.agents/harness-workspace.md` when available and the native entry below.
   Reuse an existing record for the same path. Do this even when the user defers
   source initialization. Confirm the native path before reporting completion;
   a child-shell `cd` or filesystem access grant is not project registration.
5. If initialization was not already decided, offer it now for a new or reused
   scaffold that needs it. On approval, read its `AGENTS.md`, `README.md` and
   `.agents/scripts/README.md`. Use the checked-out initializer:
   `python3 .agents/scripts/repo_init.py --no-input` reuses saved `workspace.json`
   fork selections. For first setup using the verified account's existing forks,
   use `python3 .agents/scripts/repo_init.py --github-user <account> --no-input`.
   The initializer configures forks; it does not create GitHub forks. Resolve
   missing forks within the requested setup and keep existing personal remotes.
   Do not replace this flow with manual submodule commands or copying the sample
   configuration over the user's file.
6. Report the path, native registration/open status and initialization status
   separately. Resume a failed initialization in the same workspace. Contribution
   settings retain their approved scope; workspace installation does not enable
   sharing or expand that scope.

## Native entry

Codex desktop: use **Add project** or an available native project tool and
verify the selected path in the project list. On supported macOS installations,
`codex app "<absolute-path>"` opens the workspace. On Windows this command may
only print the path; finish through the app folder picker. Codex CLI uses
`codex -C "<absolute-path>"` for the next interactive session, which does not
register a desktop project. Check the installed CLI help before launching.

Use available native tools or UI automation to complete the action. If the agent
cannot operate the required folder picker, give the selected path and that one
remaining action, and report registration as pending. Never edit private harness
project databases or create a second client profile to simulate registration.

Suggested combined offer (use the user's language):

“是否创建并初始化 vllm-ascend-workspace，同时添加到当前客户端工作区？也可以使用已有目录，或继续当前目录。”
