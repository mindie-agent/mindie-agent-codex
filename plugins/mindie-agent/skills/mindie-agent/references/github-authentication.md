# GitHub Sign-In During Setup

Use this flow during installation, first-use setup, or when GitHub authentication fails:

1. **Check existing login**: Install `gh` if missing and run `gh api --hostname github.com user --jq .login` with a bounded timeout. If the expected account is already authenticated (including via `GH_TOKEN`), proceed directly to step 6.
2. **Choose login method**: Prefer token login on enterprise networks, behind corporate proxies/custom CAs, or when browser login is unavailable; otherwise default to browser login. Respect the user's explicit preference. Keep TLS verification enabled; repair network/proxy/CA issues directly rather than replacing valid tokens.
3. **Browser login**: Run `gh auth login --hostname github.com --git-protocol https --web` in a user-accessible terminal. Offer the token route if browser authorization is blocked.
4. **Token creation**: If the user needs a new token, open [GitHub's token page](https://github.com/settings/tokens/new?scopes=repo%2Cread%3Aorg%2Cgist&description=MindIE%20Agent) for a classic PAT with `repo`, `read:org`, and `gist` scopes (or an organization-approved fine-grained PAT covering the target repositories).
5. **Token input**: Pass the token through `gh` rather than chat or command-line arguments:
   - Interactive terminal: run `gh auth login --hostname github.com --git-protocol https` and choose `Paste an authentication token`.
   - Token file: pipe via stdin (`gh auth login --hostname github.com --git-protocol https --with-token < "$TOKEN_FILE"` on POSIX, or `Get-Content -Raw -LiteralPath $TokenFile | gh auth login --hostname github.com --git-protocol https --with-token` on PowerShell).
   - If a stale `GH_TOKEN` or `GITHUB_TOKEN` environment variable overrides saved credentials, update or clear that environment variable first.
6. **Configure Git & verify access**:
   - Ensure `git` is on `PATH`. On Windows, clear unscoped system credential selectors (`git config --system --unset-all credential.helper` and `GCM_INTERACTIVE=never`; if corporate proxy `schannel` revocation checks fail with `CRYPT_E_NO_REVOCATION_CHECK`, set `http.sslBackend=schannel` and `http.schannelCheckRevoke=false` while keeping `http.sslVerify=true`).
   - Run `gh auth setup-git --hostname github.com` and verify read access to the target repository. For saved logins, also verify in a child environment without `GH_TOKEN`/`GITHUB_TOKEN` so background workers can reuse the stored credentials. See [enterprise network design](https://github.com/mindie-agent/mindie-agent/blob/main/docs/enterprise-network.en.md) for transport details.
