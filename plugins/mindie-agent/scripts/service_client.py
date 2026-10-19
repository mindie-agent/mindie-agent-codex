"""Exact service connection with the selected provider's cold-start context.

[Role]
Connects to or cold-starts the local `mindie_knowledge` background service
after validating summary provider credentials when the service is absent.

[Invariants]
- Requires active lifecycle state before probing or launching the service.
- Validates `summary_provider.required_environment(home)` on cold start.
- A no-domain/no-URI call keeps the exact configured default engine and never
  imports domain routing, preserving installed old-runtime behavior.
- An explicit domain or `mindie://DOMAIN/...` reference selects only one
  existing configured engine; missing routing support or an unknown/mismatched
  domain fails closed instead of falling back to the default.

[Timeouts & Locks]
- Delegates service probe and startup timeouts to `mindie_knowledge.loop.cli`.

[Side-Effect Policy]
- Starts the local knowledge service only when `_probe` reports `"absent"`.
- Routing reads existing native install configuration; it never creates a
  config, registry, daemon, lease or cross-domain authority.
"""

import json
from collections.abc import Mapping
from pathlib import Path
import re


class DomainRouteError(ValueError):
    """An explicit domain or reference could not be routed safely."""


_REFERENCE_AUTHORITY = re.compile(r"mindie://(?P<domain>[a-z][a-z0-9-]{0,63})/")
_DOMAIN = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
_MULTI_DOMAIN_UNAVAILABLE = (
    "installed knowledge runtime does not provide configured multi-domain routing"
)


def explicit_route(domain=None, ref=None):
    """True only for a public domain or a canonical mindie:// reference."""
    return domain is not None or (
        isinstance(ref, str) and ref.startswith("mindie://")
    )


def _reference_domain(ref):
    match = _REFERENCE_AUTHORITY.match(ref) if isinstance(ref, str) else None
    return match.group("domain") if match is not None else None


def _default_domain(engine):
    """Read the configured default authority for a no-import default route."""
    try:
        config = json.loads(Path(engine).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    domain = config.get("domain") if isinstance(config, dict) else None
    return domain if isinstance(domain, str) else None


def select_engine(engine, *, domain=None, ref=None, domain_configs=None):
    """Choose the one engine for an explicit domain/reference.

    No-domain calls return the configured default unchanged and do not import
    the routing helper. A canonical reference to the configured default domain
    also reaches that default without requiring the newer helper. Unknown or
    non-default domains still fail closed on an old runtime, and an explicit
    domain/ref conflict is never resolved by selecting the default.
    """
    if not explicit_route(domain, ref):
        return engine
    routed_ref = ref if isinstance(ref, str) and ref.startswith("mindie://") else None
    ref_domain = _reference_domain(routed_ref)
    requested = domain
    if routed_ref is not None and ref_domain is not None:
        if domain is not None and domain != ref_domain:
            raise DomainRouteError("explicit domain conflicts with the reference authority")
        requested = ref_domain
    if requested is not None and _DOMAIN.fullmatch(requested) is None:
        raise DomainRouteError("domain must match [a-z][a-z0-9-]{0,63}")
    if domain_configs is not None and not isinstance(domain_configs, Mapping):
        raise DomainRouteError("domain_configs must be a mapping")
    routed_default = (
        requested is not None
        and not (isinstance(domain_configs, Mapping) and requested in domain_configs)
        and _default_domain(engine) == requested
    )
    if routed_default:
        return engine
    try:
        from mindie_knowledge.loop.domain_routes import select_engine_config
    except ModuleNotFoundError as exc:
        if exc.name != "mindie_knowledge.loop.domain_routes":
            raise
        raise DomainRouteError(_MULTI_DOMAIN_UNAVAILABLE) from exc
    try:
        return select_engine_config(
            engine, domain=domain, ref=routed_ref, domain_configs=domain_configs
        )
    except ValueError as exc:
        raise DomainRouteError(str(exc)) from exc


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
