"""Project only the selected native provider into a private summary home.

[Role]
Reads the active native Codex `config.toml` model provider configuration, resolves model catalog aliases (`model_catalog_json`), and projects a temporary private `CODEX_HOME` for the background summary worker.

[Invariants]
- Never snapshots credential values into receipts or policy hashes (`<credential>` placeholder is used in `route_sha256`).
- Rejects `profile` overrides or custom providers requiring native OpenAI auth before spawning any summary process.
- Projects the selected native model record with its route; metadata changes invalidate prepared work.

[Timeouts & Locks]
- Synchronous bounded file reads (`config.toml <= 256 KiB`, `model_catalog_json <= 4 MiB`); no subprocesses or locks.

[Side-Effect Policy]
- Purely read-only except `environment(route, directory)`, which writes a mode `0o600` `config.toml` inside the caller's temporary `native-home` directory.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tomllib
from urllib.parse import parse_qsl, urlsplit, urlunsplit

_STRINGS = {"name", "base_url", "wire_api", "env_key", "env_key_instructions",
            "experimental_bearer_token"}
_BOOLEANS = {"requires_openai_auth", "supports_websockets"}
_TABLES = {"http_headers", "env_http_headers", "query_params"}
_NUMBERS = {"request_max_retries", "stream_max_retries", "stream_idle_timeout_ms"}
_CREDENTIAL_QUERY = {"key", "api_key", "apikey", "token", "access_token", "auth",
                     "authorization", "subscription_key", "signature", "sig",
                     "client_secret", "password"}
_CREDENTIAL_HEADER = {"authorization", "proxy-authorization", "api-key", "x-api-key",
                      "x-auth-token", "cookie", "ocp-apim-subscription-key"}
NATIVE_AUTH_ENV = (
    "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "OPENAI_FEDERATION_RULE_ID",
    "OPENAI_IDENTITY_TOKEN_FILE", "OPENAI_WORKLOAD_IDENTITY_CONTEXT",
)


class ProviderConfigurationError(ValueError):
    """Static configuration categories contain no native credential values."""


def _read(path, limit):
    try:
        p = Path(path)
        if not p.is_file():
            raise ProviderConfigurationError("summary_provider_document_invalid")
        with p.open("rb") as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ProviderConfigurationError("summary_provider_document_invalid")
        return raw.decode("utf-8")
    except (OSError, UnicodeError):
        raise ProviderConfigurationError("summary_provider_document_unavailable") from None


def _catalog(config, model):
    catalog = config.get("model_catalog_json")
    if catalog is None:
        # No implicit prefix is inferred from the business model or hostname.
        return model, None
    if not isinstance(catalog, str) or not Path(catalog).is_absolute():
        raise ProviderConfigurationError("summary_model_catalog_invalid")
    try:
        data = json.loads(_read(catalog, 4 * 1024 * 1024))
        entries = data["models"] if isinstance(data, dict) else data
        if not isinstance(entries, list):
            raise ValueError()
        for item in entries:
            if not isinstance(item, dict) or not isinstance(item.get("slug"), str):
                raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise ProviderConfigurationError("summary_model_catalog_invalid") from None
    candidates = [item for item in entries
                  if item["slug"] == model or item["slug"].endswith("/" + model)]
    if len(candidates) != 1:
        raise ProviderConfigurationError("summary_model_catalog_ambiguous_or_missing")
    selected = candidates[0]
    # Native capabilities and instructions travel with the alias. Copying only
    # its name makes Codex select fallback metadata before the model request.
    projected = json.dumps({"models": [selected]}, sort_keys=True,
                           ensure_ascii=True, allow_nan=False) + "\n"
    return selected["slug"], projected


def _route_query(pairs):
    # Preserve arbitrary provider parameters. Only known credential values are
    # excluded from policy identity; unknown values are hashed, never printed.
    return [(key, "<credential>" if key.casefold().replace("-", "_") in _CREDENTIAL_QUERY else value)
            for key, value in pairs]


def _route_headers(provider):
    headers = [(key.casefold(), value) for key, value in provider.get("http_headers", {}).items()]
    headers += [(key.casefold(), os.environ[env_key])
                for key, env_key in provider.get("env_http_headers", {}).items()]
    return [(key, "<credential>" if key in _CREDENTIAL_HEADER else value)
            for key, value in sorted(headers)]


def _selected_provider(home=None, *, project=True):
    """Read the selected provider without requiring its current credentials."""
    home = home or os.environ.get("CODEX_HOME") or Path.home() / ".codex"
    path = Path(home).expanduser() / "config.toml"
    if not path.exists():
        return {}, "openai", None
    raw = _read(path, 256 * 1024)
    try:
        config = tomllib.loads(raw)
    except ValueError:
        raise ProviderConfigurationError("summary_provider_config_invalid") from None
    if project and config.get("profile") is not None:
        raise ProviderConfigurationError("summary_native_profile_selection_unsupported")
    name = config.get("model_provider", "openai")
    providers = config.get("model_providers", {})
    if not isinstance(name, str) or not isinstance(providers, dict):
        raise ProviderConfigurationError("summary_provider_config_invalid")
    if name == "openai" and name not in providers:
        return config, name, None
    source = providers.get(name)
    if not isinstance(source, dict):
        if not project and name not in providers:
            # Installation only declares available names. A missing route is
            # still rejected by selected() before any summary invocation.
            return config, name, {}
        raise ProviderConfigurationError("summary_selected_provider_missing")
    if not project:
        return config, name, source
    if set(source) - (_STRINGS | _BOOLEANS | _TABLES | _NUMBERS):
        raise ProviderConfigurationError("summary_provider_field_unsupported")
    provider = {}
    for key in _STRINGS | _BOOLEANS | _TABLES | _NUMBERS:
        if key not in source:
            continue
        value = source[key]
        valid = (key in _STRINGS and isinstance(value, str)
                 or key in _BOOLEANS and type(value) is bool
                 or key in _NUMBERS and type(value) is int and value >= 0
                 or key in _TABLES and isinstance(value, dict)
                 and all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()))
        if not valid:
            raise ProviderConfigurationError("summary_provider_field_invalid")
        provider[key] = value
    return config, name, provider


def environment_keys(home=None):
    """Declare only provider-selected names; never snapshot credential values."""
    _, _, provider = _selected_provider(home, project=False)
    if provider is None:
        return list(NATIVE_AUTH_ENV)
    headers = provider.get("env_http_headers", {})
    if not isinstance(headers, dict):
        raise ProviderConfigurationError("summary_provider_environment_name_invalid")
    names = list(headers.values())
    if "env_key" in provider:
        names.append(provider["env_key"])
    if any(not isinstance(name, str) or
           re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None for name in names):
        raise ProviderConfigurationError("summary_provider_environment_name_invalid")
    return sorted(set(names))


def required_environment(home=None):
    """A cold static MCP entry must not start a service with missing auth."""
    _, _, provider = _selected_provider(home, project=False)
    if provider is None:
        return
    names = environment_keys(home)
    if any(not os.environ.get(name) for name in names):
        raise ProviderConfigurationError(
            "required provider context unavailable; start a new native SessionStart")


def selected(model):
    """Honor the installed native home, including the ordinary default home."""
    config, name, provider = _selected_provider()
    if provider is None:
        return {"model": model, "provider": None}
    required_env = list(provider.get("env_http_headers", {}).values())
    if "env_key" in provider:
        required_env.append(provider["env_key"])
    if any(not os.environ.get(key) for key in required_env):
        raise ProviderConfigurationError("summary_provider_auth_environment_unavailable")
    if provider.get("requires_openai_auth"):
        raise ProviderConfigurationError("summary_custom_provider_requires_native_auth")
    if not provider.get("name") or not provider.get("base_url"):
        raise ProviderConfigurationError("summary_provider_route_missing")
    try:
        parsed = urlsplit(provider["base_url"])
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError()
        endpoint = urlunsplit((parsed.scheme, parsed.hostname + (
            ":" + str(parsed.port) if parsed.port is not None else ""), parsed.path, "", ""))
    except ValueError:
        raise ProviderConfigurationError("summary_provider_route_invalid") from None
    route = {"provider": name, "endpoint": endpoint,
             "wire_api": provider.get("wire_api", "responses"),
             "base_url_query": _route_query(parse_qsl(parsed.query, keep_blank_values=True)),
             "query_params": _route_query(sorted(provider.get("query_params", {}).items())),
             "headers": _route_headers(provider)}
    selected_model, catalog = _catalog(config, model)
    if catalog is not None:
        route["model_catalog_sha256"] = hashlib.sha256(catalog.encode()).hexdigest()
    selected = {"model": selected_model, "provider": provider, "name": name,
                "route_sha256": hashlib.sha256(json.dumps(route, sort_keys=True).encode()).hexdigest()}
    if catalog is not None:
        selected["model_catalog"] = catalog
    return selected


def _toml(value):
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=True)
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is int:
        return str(value)
    return "{" + ", ".join(_toml(k) + "=" + _toml(v) for k, v in value.items()) + "}"


def environment(route, directory):
    """Credentials stay in a private temporary config, never argv or receipts."""
    if route["provider"] is None:
        return None
    home = Path(directory) / "native-home"
    config = home / "config.toml"
    text = "model_provider = " + _toml(route["name"]) + "\n"
    documents = []
    if route.get("model_catalog") is not None:
        catalog = home / "models.json"
        text += "model_catalog_json = " + _toml(str(catalog)) + "\n"
        documents.append((catalog, route["model_catalog"]))
    text += "[model_providers." + _toml(route["name"]) + "]\n"
    text += "".join(_toml(key) + " = " + _toml(value) + "\n"
                    for key, value in sorted(route["provider"].items()))
    try:
        home.mkdir(mode=0o700)
        for path, content in documents + [(config, text)]:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
    except OSError:
        raise ProviderConfigurationError("summary_provider_temporary_config_unavailable") from None
    env = {key: value for key, value in os.environ.items()
           if key not in {"PYTHONPATH", "PYTHONHOME", "CODEX_THREAD_ID",
                          "CODEX_TURN_ID", "CODEX_SESSION_ID"}}
    env["CODEX_HOME"] = str(home)
    return env
