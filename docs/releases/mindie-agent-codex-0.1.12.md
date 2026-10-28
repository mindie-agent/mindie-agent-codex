# mindie-agent-codex 0.1.12

This release integrates persistent SSH authentication from remote-dev 0.11.1.
After a user supplies a password once, remote-dev verifies access and attempts
to install and verify a managed public key. Password-only endpoints reuse the
saved credential across new chats and independent plugin runtimes. A rejected
key alone is not treated as proof that the server has disabled public keys.

Remote-dev owns the shared `~/.mindie/ssh/credentials.json` file and profiles
scoped by host, port, and SSH user. The plugin adds no password store or OS
keychain access and does not request the computer-account password. Key repair
happens before business submission; uncertain business operations are not
automatically replayed. Authentication failures preserve safe recovery hints,
submission state, and separate key-maintenance facts.

The generated Codex tool catalog includes `remote_auth`. The adapter reuses shared safe authentication hints and retains submission certainty without exposing raw SSH errors.

The active runtime pins are:

| Component | Version / tag | Source commit |
| --- | --- | --- |
| remote-dev | 0.11.1 / v0.11.1 | `2a750ec8c74d7f053410d48dd820aeb542e7d3b1` |
| knowledge | 0.13.4 / v0.13.4 | `6e921b205c00d592c25f849653cbfb72b73fe318` |
| diagnostics | 0.5.4 / v0.5.4 | `5ae643e19223f2181ecdbaa3edaf464137c4805e` |

Existing resolver overrides select diagnostics 0.5.4 over older transitive
pins. Normal resolver installation was verified in a separate virtual
environment, including installed `direct_url.json` identities and actual MCP
`tools/list` discovery of `remote_auth` with `password_env` and no plaintext
`password` field. Coordinator and npu-top remain independently installed
components; manual knowledge activation stays scoped to the native session.

Real SSH acceptance passed on macOS (7 tests, 34.58 seconds), Windows Server
2025 with PowerShell 7.6.7, Python 3.12.10 and Git OpenSSH 10.5p1 (4 tests,
14.186 seconds), and WSL2 Ubuntu 24.04 with OpenSSH 9.6p1 (4 tests, 12.694
seconds). [Platform run 40774308378](https://github.com/mindie-agent/remote-dev/actions/runs/40774308378)
tested remote-dev candidate `a7564f49b1ff106e59f72961c851b7b11eb81130`;
the 0.11.1 pin additionally fixes Git for Windows SSH discovery for the
`ucrt64` and `usr/bin` installation layouts. Final 0.11.1 platform acceptance
is recorded separately before publication.

Coverage includes managed-key installation, reuse across independent virtual
environments, password-only authentication, revoked-key repair before one
business execution, and one shared credential used by coordinator, two Kimi
chat processes, and npu-top after reopening its database. Kimi uses its actual
nonce/MCP adapter; Codex, Claude Code and DSH use their shared endpoint profiles.
These checks do not claim complete native-host end-to-end runs or that an
existing host has loaded this release. All acceptance uses isolated state and
the default credential-file provider. Formal tag workflows attach test and
benchmark reports plus checksums from the exact released source.
