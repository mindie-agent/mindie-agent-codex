# mindie-agent-codex 0.1.12

Release preparation is in progress. The remote-dev 0.11.0 formal dependency
revision, regenerated tool catalog, and final platform evidence must be recorded
before this candidate is published.

This update integrates shared SSH authentication from remote-dev. A user can
provide a password once to verify access and install a managed public key.
Servers accepting only password authentication can reuse the saved credential
across independent plugin runtimes and new chats. A rejected key is classified
from the actual authentication result; it does not by itself mean the server
has disabled public-key authentication.

Authentication belongs to the local user's remote-dev store. Its default
credential file is `~/.mindie/ssh/credentials.json`; profiles are scoped by SSH
host, port, and user. The plugin passes profile references and does not maintain
another credential store. Normal operation does not invoke an OS keychain or
request the computer account password.

The runtime retains diagnostics 0.5.4 and knowledge 0.13.4. Coordinator and
npu-top continue to follow their existing component installation boundaries.
Knowledge activation remains scoped to explicit session entry.

Validation must distinguish installed package execution, the four remote-dev
harness profiles, actual plugin MCP adapter calls, and a running native host.
Shared authentication acceptance covers key installation, password-only access,
new processes and virtual environments, and key recovery before a business
command is submitted once. These checks do not establish that an already-running
Codex host has refreshed its tool catalog or activated this plugin. Release
assets must contain the final tag's test and benchmark reports and checksums.
