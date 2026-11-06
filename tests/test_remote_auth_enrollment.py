"""The strict Codex catalog and its runtime accept the same chat credential."""
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"))
import mcp_gate
import runtime_call
from remote_dev.auth import access, CredentialStore, ProfileStore
from remote_dev.core.endpoint import Endpoint


@pytest.mark.parametrize("key_available", [True, False])
def test_catalog_to_runtime_password_enrollment(tmp_path, monkeypatch, key_available):
    monkeypatch.setenv("REMOTE_DEV_AUTH_DIR", str(tmp_path / "auth"))
    monkeypatch.delenv("REMOTE_DEV_CREDENTIAL_FILE", raising=False)
    monkeypatch.setattr(access, "ensure_key", lambda *args, **kwargs: (
        str(tmp_path / "key"), "ssh-ed25519 AAAAFIXTURE codex-enrollment"))
    calls = []
    secret = "fixture-\u5bc6\u7801-!$`'\""

    def probe(endpoint, **kwargs):
        calls.append(kwargs["method"])
        if kwargs["method"] == "password":
            assert CredentialStore().get(kwargs["credential_ref"]) == secret
        denied = len(calls) == 1 or (len(calls) == 4 and not key_available)
        return access._Outcome(255 if denied else 0, "", "Permission denied (publickey)." if denied else "")

    monkeypatch.setattr(access, "_probe", probe)
    name, arguments = mcp_gate.Gate("remote")._tool_args({"params": {
        "name": "remote_auth", "arguments": {
            "host": "auth.example", "port": 2200, "user": "alice", "password": secret,
        },
    }})
    response = runtime_call.remote({"remote_session_id": "auth-contract-session",
                                    "name": name, "arguments": arguments})
    assert response["isError"] is False, response
    assert calls == ["publickey", "password", "password", "publickey"]
    profile = ProfileStore().resolve(Endpoint("auth.example", 2200, user="alice"))
    assert profile["auth_method"] == ("publickey" if key_available else "password")
    assert secret not in json.dumps(response, ensure_ascii=False) + json.dumps(profile, ensure_ascii=False)
