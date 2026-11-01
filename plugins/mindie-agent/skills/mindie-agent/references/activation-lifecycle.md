# Capture and Operation Lifecycle

## Session Activation & Background Capture

- Knowledge operations and background `Stop` capture require an explicit `/mindie-agent` or `$mindie-agent` invocation in the current session (`CODEX_THREAD_ID`). Once granted, the lease persists across turns in that session until deactivated.
- On `Stop`, the hook checks for an active manual-entry lease (`entry_enabled = 1`) and approved sharing scope before reading the session transcript from `max(created_at, activated_at)` onward. Unactivated sessions return immediately.
- A single-turn host `Stop` event only hands off the completed turn's public transcript slice to the background organizer; it does not mark a multi-turn or long-horizon engineering task as completed.
- Remote execution (`remote-dev`) and deterministic component tools operate independently of the knowledge lease.

## Explicit Configuration, Status & Multi-Domain Routing (`entry.py bridge`)

Use the installed `PLUGIN_DATA/releases/0.1.1/entry.py bridge` in the active profile (`CODEX_THREAD_ID` is provided by the native shell):

- **`entry.py bridge activate`**: binds the session's manual-entry lease granted by `UserPromptSubmit`.
- **`entry.py bridge status`**: returns factual read-only status (`experience`, `sharing`, `admission`, `service`, `diagnostics`, `updates`, or `configured: false`).
- **Unified Contribution & Fault-Reporting Switch (`entry.py bridge config`)**:
  - `adapter_config.parent / "mindie-consent.json"` stores the single persistent choice governing both community knowledge PRs and sanitized product fault Issues.
  - Enabling `contribute` configures scoped knowledge sharing and starts the background fault reporter (`reporting.status == "running"`). Run `entry.py bridge config` with at least a 180-second timeout budget.
  - When enabling contribution for a public knowledge repository `owner/repo` (e.g., `mindie-agent/knowledge-vllm-ascend`) under account `USER`, ensure the personal fork exists (`gh repo fork owner/repo --clone=false --remote=false`) and pass `--community-fork USER/repo --community-repository owner/repo --community-account USER --community-project-root /absolute/path --community-visibility public`.
  - Keep three states distinct when reporting back: (1) local consent config written, (2) background reporter worker running, and (3) external GitHub Issue/PR created.
- **Multi-Domain Registration, Target Activation & `deactivate`**:
  - Register an additional domain route with:
    ```sh
    <python> -m mindie_knowledge.loop.domain_routes register --adapter-config "<adapter-config.json>" --domain "<domain>" --engine-config "<domain-engine-config.json>"
    ```
  - Distilling or contributing to a non-default registered `domain` requires that domain's own consent and project scope plus explicit activation:
    ```sh
    <python> "<release-entry.py>" bridge activate --domain "<domain>"
    ```
  - Running `<python> "<release-entry.py>" bridge deactivate` (with no `--domain`) revokes the current `CODEX_THREAD_ID` session across the default domain and all registered `domain_configs`, returning `status="partial"` with per-domain `domains[]` details if any domain fails during revocation.

## Historical Experience Import (Explicit User Request Only)

Run historical import only when the user explicitly asks to import past session transcripts into the knowledge base:

1. Verify that the current session has an active `/mindie-agent` lease and approved sharing scope.
2. Confirm which Codex JSONL transcript files the user wants to import.
3. Run:
   ```sh
   <python> "<release-entry.py>" bridge history-import --source "ABSOLUTE_TRANSCRIPT.jsonl"
   ```
   (Repeat `--source` for multiple files; add `--retry-summary` only when explicitly retrying an interrupted index step.)
4. Report per-file status (`imported` / `extended`, `unchanged`, `empty`, or `failed`).
