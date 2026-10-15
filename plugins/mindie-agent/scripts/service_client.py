"""Exact service connection with the selected provider's cold-start context."""


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
