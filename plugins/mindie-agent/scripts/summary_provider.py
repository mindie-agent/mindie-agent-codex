"""Project only the selected native provider into a private summary home."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
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


class ProviderConfigurationError(ValueError):
    """Static configuration categories contain no native credential values."""


def _read(path, limit):
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise ProviderConfigurationError("summary_provider_document_invalid")
            raw = os.read(fd, limit + 1)
            if len(raw) > limit:
                raise ProviderConfigurationError("summary_provider_document_invalid")
            return raw.decode("utf-8")
        finally:
            os.close(fd)
    except (OSError, UnicodeError):
        raise ProviderConfigurationError("summary_provider_document_unavailable") from None


def _alias(config, model):
    catalog = config.get("model_catalog_json")
    if catalog is None:
        # No implicit prefix is inferred from the business model or hostname.
        return model
    if not isinstance(catalog, str) or not Path(catalog).is_absolute():
        raise ProviderConfigurationError("summary_model_catalog_invalid")
    try:
        data = json.loads(_read(catalog, 4 * 1024 * 1024))
        entries = data["models"] if isinstance(data, dict) else data
        if not isinstance(entries, list):
            raise ValueError()
        names = []
        for item in entries:
            if not isinstance(item, dict) or not isinstance(item.get("slug"), str):
                raise ValueError()
            names.append(item["slug"])
    except (ValueError, TypeError, KeyError):
        raise ProviderConfigurationError("summary_model_catalog_invalid") from None
    candidates = [name for name in names if name == model or name.endswith("/" + model)]
    if len(candidates) != 1:
        raise ProviderConfigurationError("summary_model_catalog_ambiguous_or_missing")
    return candidates[0]


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


def selected(model):
    """Honor the installed native home, including the ordinary default home."""
    home = os.environ.get("CODEX_HOME") or Path.home() / ".codex"
    path = Path(home).expanduser() / "config.toml"
    try:
        path.lstat()
    except FileNotFoundError:
        return {"model": model, "provider": None}
    except OSError:
        raise ProviderConfigurationError("summary_provider_document_unavailable") from None
    raw = _read(path, 256 * 1024)
    try:
        config = tomllib.loads(raw)
    except ValueError:
        raise ProviderConfigurationError("summary_provider_config_invalid") from None
    if config.get("profile") is not None:
        raise ProviderConfigurationError("summary_native_profile_selection_unsupported")
    name = config.get("model_provider", "openai")
    providers = config.get("model_providers", {})
    if not isinstance(name, str) or not isinstance(providers, dict):
        raise ProviderConfigurationError("summary_provider_config_invalid")
    if name == "openai" and name not in providers:
        return {"model": model, "provider": None}
    source = providers.get(name)
    if not isinstance(source, dict):
        raise ProviderConfigurationError("summary_selected_provider_missing")
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
    return {"model": _alias(config, model), "provider": provider, "name": name,
            "route_sha256": hashlib.sha256(json.dumps(route, sort_keys=True).encode()).hexdigest()}


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
    text += "[model_providers." + _toml(route["name"]) + "]\n"
    text += "".join(_toml(key) + " = " + _toml(value) + "\n"
                    for key, value in sorted(route["provider"].items()))
    try:
        home.mkdir(mode=0o700)
        fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
    except OSError:
        raise ProviderConfigurationError("summary_provider_temporary_config_unavailable") from None
    env = {key: value for key, value in os.environ.items()
           if key not in {"PYTHONPATH", "PYTHONHOME", "CODEX_THREAD_ID",
                          "CODEX_TURN_ID", "CODEX_SESSION_ID"}}
    env["CODEX_HOME"] = str(home)
    return env
