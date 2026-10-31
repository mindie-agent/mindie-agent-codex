# mindie-agent-codex 0.1.14

Codex Desktop `0.166.0-alpha.5.1` was excluded by the exact-version
compatibility registry, so automatic updates reported `no_compatible_release`.
Model-free native acceptance on Windows confirms that the consumed plugin,
hook and MCP interfaces still work. This release adds that exact Desktop
version and stable CLI `0.166.0`; other unverified versions remain excluded.

## Windows installation and update fixes

- Start newly prepared interpreters through the Windows extended path
  namespace. Python retains that spelling in `sys.prefix`, allowing dependency
  resources beyond `MAX_PATH` to load. This fixes the reproduced
  `jsonschema_specifications` resource import failure during candidate
  validation. Component candidates use the same spelling for interpreter,
  retained source and release paths, including generated domain profiles.
- Preserve already published runtime receipts and their original interpreter
  spelling when reusing an existing environment.
- Resolve the running Desktop executable from `CODEX_APP_RUNTIME_CLI_PATH`
  for installed operations while retaining the selected `CODEX_HOME`. Desktop
  replaces versioned binary directories during updates; a retired path in
  the installation context must not keep blocking future update checks.

The release also includes the bounded component-output capture and owned
process cleanup fixes already merged since 0.1.13.

## Validation

The real Windows Desktop binary passes isolated native marketplace installation
and readback, all five hooks, trust compare-and-swap, explicit disable
preservation, locked runtime preparation, SessionStart and discovery of 23
remote tools plus five component tools. No knowledge tools enter the public
catalog. The native test uses a long plugin-data path and reproduced the
dependency-resource failure before the fix.

CI retains the three-platform adapter contracts and the two earlier stable
native hosts, adds native CLI `0.166.0` on Ubuntu, and adds Windows native
acceptance for `0.166.0`. Tests isolate host profiles and MindIE state. Local
Windows network acceptance reuses the operator's existing Git transport
configuration; production TLS verification is unchanged.

These checks make no model requests and do not claim paid model-turn or NPU
acceptance. Exact component pins remain knowledge `0.13.6`, remote-dev
`0.11.1` and diagnostics `0.5.4`; automatic updates preserve installed component
version floors. The tag workflow verifies formal dependency releases and
publishes the wheel, source archive, compatibility metadata, benchmark reports
and checksums from the exact tagged commit.
