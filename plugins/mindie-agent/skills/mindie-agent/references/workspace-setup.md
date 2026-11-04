# Development Workspace & First-Time Onboarding Setup

Run this flow during initial installation (Step 5) or whenever `python <runtime_scripts>/setup.py sharing --status` reports `"needs_onboarding_prompt": true`, right after presenting [用户须知](user-notice.md):

1. **Pop up the 3 onboarding questions via native `request_user_input`** (falling back to a numbered prompt only if `request_user_input` is unavailable in the current execution mode):
   - **`community_sharing`** (*Community Knowledge Sharing & Automated Fault Reporting*):
     - `Enable (Recommended)` — Enable sanitized experience sharing PRs and automatic tool fault Issue reporting
     - `Keep Local-Only Mode` — Disable outgoing sharing and fault reporting; keep retrieval and tools local
   - **`github_star`** (*GitHub Star ⭐ for `mindie-agent/mindie-agent`*):
     - `Star on GitHub (Recommended)` — Star `mindie-agent/mindie-agent` via `gh api -X PUT /user/starred/mindie-agent/mindie-agent`
     - `Skip for now` — Do not star the repository right now
   - **`workspace_setup`** (*Local Development Workspace `vllm-ascend-workspace`*):
     - `Initialize vllm-ascend-workspace (Recommended)` — Clone and initialize the multi-repo `vllm` + `vllm-ascend` workspace
     - `Keep current directory` — Use the current working directory as the project root
     - `Use existing workspace directory` — Bind an existing local workspace checkout
2. **Verify GitHub authentication**: Before cloning or configuring forks, ensure GitHub authentication is ready via [GitHub Sign-In](github-authentication.md) using process-scoped `GIT_CONFIG_COUNT` (without mutating global or system-wide Git configuration).
3. **Resolve or initialize `WORKSPACE_ROOT` first**:
   - New directory: `git clone https://github.com/mindie-agent/vllm-ascend-workspace.git "<destination>"` and set `WORKSPACE_ROOT="<destination>"`.
   - Existing directory: inspect the checkout, preserve local changes and configuration, and set `WORKSPACE_ROOT="<existing-path>"`.
   - Keep current directory: set `WORKSPACE_ROOT="$(pwd)"`.
4. **Register the workspace with the installed host**:
   - **Codex CLI (preferred when Codex.app is absent)**: launch the next interactive session with `codex -C "<absolute-path>"`. This is a complete success path for a CLI-only install, including macOS Homebrew `codex` with no Codex.app. Do not run `codex app`, and do not treat a missing Desktop app as a failed registration.
   - **Codex Desktop (only when the app is installed)**: add/open the workspace root via **Add project**. On macOS this may be `codex app "<absolute-path>"`; on Windows, complete selection in the folder picker if needed. If `codex app` fails because Codex.app is not installed, fall back to `codex -C "<absolute-path>"` and continue.
5. **Initialize repositories (when creating/reusing `vllm-ascend-workspace`)**: Read the workspace's `AGENTS.md`, `README.md`, and `.agents/scripts/README.md`, then run:
   - `python3 .agents/scripts/repo_init.py --no-input` (reusing saved `workspace.json` fork settings), or
   - `python3 .agents/scripts/repo_init.py --github-user <account> --no-input` (for first-time setup with the user's forks).
6. **Persist the sharing & fault-reporting choice and optional Star**:
   - If the user chose `Enable (Recommended)`: run `python <runtime_scripts>/setup.py sharing --enable --project-root "$WORKSPACE_ROOT"`.
   - If the user chose `Keep Local-Only Mode`: run `python <runtime_scripts>/setup.py sharing --disable`.
   - If the user chose `Star on GitHub (Recommended)`: run `gh api --hostname github.com --silent -X PUT /user/starred/mindie-agent/mindie-agent && gh api --hostname github.com --silent /user/starred/mindie-agent/mindie-agent`.
7. **Report status**: Report the plugin status, sharing & fault-reporting status, workspace path (`WORKSPACE_ROOT`), host registration (`codex -C` and/or Desktop **Add project**), repository initialization status, and GitHub Star status. CLI `codex -C` alone is a successful registration.
