# Capture and Operation Lifecycle

## Default-On Knowledge & Opt-In Background Capture

- Knowledge tools (`knowledge_query`, `knowledge_explain`, `knowledge_feedback`, `knowledge_distill`, `knowledge_sync`) and deterministic component tools are exposed by default on `mindie-tools` (`tools-mcp`) and via `knowledge_cli.py` with zero session-activation prefix or `_meta` prerequisite.
- `SessionStart` and `UserPromptSubmit` are fast passive hooks that never create SQLite databases on cold start.
- On `Stop`, the hook checks community sharing consent and authorized `project_roots` first before touching SQLite. When community sharing is unconfigured, disabled, or outside authorized `project_roots`, `Stop` exits cleanly in `< 100ms` without creating SQLite state. When community sharing IS enabled and `cwd` is inside authorized `project_roots`, `Stop` auto-ensures an active session lease (`not_before=0.0`) and hands off the completed turn's redacted public transcript slice to the background organizer.
- A single-turn host `Stop` event only hands off the completed turn's public transcript slice; it does not mark a multi-turn or long-horizon engineering task as completed.

## Knowledge CLI Input & Failure Stages

- Invoke `knowledge_cli.py` as `describe`, `<operation>` with UTF-8 JSON on binary stdin, or `<operation> --input-file <path>`. A selected file is the only source; an unreadable file never falls back to stdin. Input is bounded to 1 MiB and accepts UTF-8 with an optional BOM. UTF-16/GBK, empty, oversized, malformed and non-object JSON are rejected before dispatch.
- On Windows PowerShell 5.1, write the payload as UTF-8 (its BOM is accepted) and pass the file instead of relying on the console pipe:
  ```powershell
  $payload | Set-Content -LiteralPath $inputFile -Encoding UTF8 -ErrorAction Stop
  & $runtimePython -I $cli knowledge_query --input-file $inputFile
  if ($LASTEXITCODE -ne 0) { throw "knowledge_cli.py failed with exit code $LASTEXITCODE" }
  ```
- Closed failure stages: `cli_input` (`argv_invalid`, `input_empty`, `input_too_large`, `input_encoding`, `input_json` with optional `line`/`column`, `input_not_object`), `input_read` (`code="input_unavailable"`), and `tool_schema` (`unknown_operation`, `schema_missing_field`, `schema_unknown_field`, `schema_unknown_input_field`).
- Windows service preparation may fail at `windows_service_interpreter` with `windowless_interpreter_missing` or `windowless_interpreter_invalid`. An invalid candidate never retires the current service owner or publishes its binding; API-only preparation remains usable without scheduled-service capability.

## Explicit Configuration, Status & Multi-Domain Routing (`entry.py bridge`)

Use the installed `<release-entry.py> bridge` in the active profile (`CODEX_THREAD_ID` is provided by the native shell when running inside a Codex thread):

- **`entry.py bridge status`**: returns factual read-only status (`experience`, `sharing`, `admission`, `service`, `diagnostics`, `updates`, or `configured: false`).
- **`entry.py bridge activate`**: binds or inspects a session lease (`--domain <domain>` for non-default domains).
- **Unified Contribution & Fault-Reporting Switch (`entry.py bridge config`)**:
  - `adapter_config.parent / "mindie-consent.json"` stores the single persistent choice governing both community knowledge PRs and sanitized product fault Issues.
  - Enabling `contribute` configures scoped knowledge sharing and starts the background fault reporter (`reporting.status == "running"`). Run `entry.py bridge config` with at least a 180-second timeout budget.
  - When enabling contribution for a public knowledge repository `owner/repo` (e.g., `mindie-agent/knowledge-vllm-ascend`) under account `USER`, ensure the personal fork exists (`gh repo fork owner/repo --clone=false --remote=false`) and pass `--community-fork USER/repo --community-repository owner/repo --community-account USER --community-project-root /absolute/path --community-visibility public`.
  - Keep three states distinct when reporting back: (1) local consent config written, (2) background reporter worker running, and (3) external GitHub Issue/PR created.
- **Multi-Domain Registration, Target Activation & Session Mute (`bridge deactivate`)**:
  - Register an additional domain route with:
    ```sh
    <python> -m mindie_knowledge.loop.domain_routes register --adapter-config "<adapter-config.json>" --domain "<domain>" --engine-config "<domain-engine-config.json>"
    ```
  - Distilling or contributing to a non-default registered `domain` requires that domain's own consent and project scope plus explicit domain lease activation:
    ```sh
    <python> "<release-entry.py>" bridge activate --domain "<domain>"
    ```
  - Running `<python> "<release-entry.py>" bridge deactivate` revokes the current `CODEX_THREAD_ID` session across the default domain and all registered `domain_configs`, returning `status="partial"` with per-domain `domains[]` details if any domain fails during revocation.

## Historical Experience Import (Explicit User Request Only)

Run historical import only when the user explicitly asks to import past session transcripts into the knowledge base:

1. Verify that the current session has an active task lease and approved sharing scope.
2. Confirm which Codex JSONL transcript files the user wants to import.
3. Run:
   ```sh
   <python> "<release-entry.py>" bridge history-import --source "ABSOLUTE_TRANSCRIPT.jsonl"
   ```
   (Repeat `--source` for multiple files; add `--retry-summary` only when explicitly retrying an interrupted index step.)
4. Report per-file status (`imported` / `extended`, `unchanged`, `empty`, or `failed`).
