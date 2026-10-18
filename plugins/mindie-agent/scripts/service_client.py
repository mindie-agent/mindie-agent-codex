"""Exact service connection with the selected provider's cold-start context.

[Role]
Connects to or cold-starts the local `mindie_knowledge` background service
after validating summary provider credentials when the service is absent.

[Invariants]
- Requires active lifecycle state before probing or launching the service.
- Validates `summary_provider.required_environment(home)` on cold start.

[Timeouts & Locks]
- Delegates service probe and startup timeouts to `mindie_knowledge.loop.cli`.

[Side-Effect Policy]
- Starts the local knowledge service only when `_probe` reports `"absent"`.
"""


def connect(engine):
    from mindie_knowledge.loop.cli import config_at, connect as connection, ensure_service
    from mindie_knowledge.loop.handoff import _probe
    from mindie_knowledge.loop.lifecycle import require_active
    from summary_provider import required_environment

    config = config_at(engine)
    require_active(engine, config)
    state = _probe(config, None, config_path=engine)
    if state == "ready":
        return connection(config, config_path=engine)
    if state == "absent":
        home = config.get("service_launch_context", {}).get("paths", {}).get("CODEX_HOME")
        required_environment(home)
    return ensure_service(engine)
