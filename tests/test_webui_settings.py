"""Settings WebUI: redaction, revisions and conservative secret writes."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from PhyAgentOS.cli.commands import app as cli_app
from PhyAgentOS.webui.app import STATIC_DIR, create_app

SECRETS = [
    "sk-openai-secret",
    "header-secret",
    "tg-secret",
    "feishu-secret",
    "feishu-encrypt",
    "matrix-token",
    "claw-token",
    "slack-bot-token",
    "slack-app-token",
    "imap-password-secret",
    "smtp-password-secret",
    "dingtalk-secret",
    "qq-secret",
    "wecom-secret",
    "discord-token",
    "brave-search-secret",
    "Bearer mcp-secret",
    "robot-env-secret",
]

SECRET_PATHS = [
    "providers.openai.apiKey",
    "providers.openai.extraHeaders.X-Custom-Token",
    "channels.telegram.token",
    "channels.feishu.appSecret",
    "channels.feishu.encryptKey",
    "channels.matrix.accessToken",
    "channels.mochat.clawToken",
    "channels.slack.botToken",
    "channels.slack.appToken",
    "channels.email.imapPassword",
    "channels.email.smtpPassword",
    "channels.dingtalk.clientSecret",
    "channels.qq.secret",
    "channels.wecom.secret",
    "channels.discord.token",
    "tools.web.search.apiKey",
    "tools.mcpServers.robot.headers.Authorization",
    "tools.mcpServers.robot.env.ROBOT_TOKEN",
]

SEEDED_CONFIG: dict[str, Any] = {
    "agents": {"defaults": {"model": "openai/gpt-4o"}},
    "providers": {
        "openai": {
            "apiKey": "sk-openai-secret",
            "extraHeaders": {"X-Custom-Token": "header-secret"},
        }
    },
    "channels": {
        "telegram": {"token": "tg-secret"},
        "feishu": {"appSecret": "feishu-secret", "encryptKey": "feishu-encrypt"},
        "matrix": {"accessToken": "matrix-token"},
        "mochat": {"clawToken": "claw-token"},
        "slack": {"botToken": "slack-bot-token", "appToken": "slack-app-token"},
        "email": {
            "imapPassword": "imap-password-secret",
            "smtpPassword": "smtp-password-secret",
        },
        "dingtalk": {"clientSecret": "dingtalk-secret"},
        "qq": {"secret": "qq-secret"},
        "wecom": {"secret": "wecom-secret"},
        "discord": {"token": "discord-token"},
    },
    "tools": {
        "web": {"search": {"apiKey": "brave-search-secret"}},
        "mcpServers": {
            "robot": {
                "headers": {"Authorization": "Bearer mcp-secret"},
                "env": {"ROBOT_TOKEN": "robot-env-secret"},
            }
        },
    },
}


@pytest.fixture()
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(SEEDED_CONFIG), encoding="utf-8")
    return path


@pytest.fixture()
def client(config_path: Path) -> TestClient:
    return TestClient(create_app(config_path))


def _get(client: TestClient) -> dict[str, Any]:
    response = client.get("/api/settings")
    assert response.status_code == 200, response.text
    return response.json()


def _put(client: TestClient, config: dict[str, Any], revision: str, **extra: Any):
    body = {"config": config, "revision": revision, **extra}
    return client.put("/api/settings", json=body)


def test_get_shape_and_no_secret_values(client: TestClient, config_path: Path) -> None:
    data = _get(client)

    assert set(data) == {"config", "schema", "revision", "secret_fields", "restart_required"}
    assert data["revision"] == hashlib.sha256(config_path.read_bytes()).hexdigest()
    assert data["restart_required"] is False
    assert "properties" in data["schema"]
    assert "providers" in data["schema"]["properties"]
    assert "resource_registry" in data["schema"]["properties"]
    assert "resource_registry" in data["config"]
    assert "resourceRegistry" not in data["config"]
    assert "memoryWindow" not in json.dumps(data["schema"])
    assert "memoryWindow" not in data["config"]["agents"]["defaults"]
    assert data["config"]["agents"]["defaults"]["model"] == "openai/gpt-4o"
    assert data["config"]["providers"]["openai"]["apiKey"] == ""

    for secret in SECRETS:
        assert secret not in json.dumps(data)
    for path in SECRET_PATHS:
        assert data["secret_fields"][path] is True, path
    assert "channels.telegram.allowFrom" not in data["secret_fields"]


def test_get_without_config_file_uses_defaults(tmp_path: Path) -> None:
    path = tmp_path / "missing.json"
    data = _get(TestClient(create_app(path)))

    assert data["revision"] == hashlib.sha256(b"").hexdigest()
    assert data["config"]["providers"]["openai"]["apiKey"] == ""
    assert data["secret_fields"]["providers.openai.apiKey"] is False


def test_put_saves_non_secret_values(client: TestClient, config_path: Path) -> None:
    data = _get(client)
    submitted = data["config"]
    submitted["agents"]["defaults"]["model"] = "openai/gpt-4.1"

    response = _put(client, submitted, data["revision"])

    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["saved"] is True
    assert "restart" in saved["message"]
    assert saved["restart_required"] is True
    assert saved["revision"] != data["revision"]
    on_disk = json.loads(config_path.read_text(encoding="utf-8"))
    assert on_disk["agents"]["defaults"]["model"] == "openai/gpt-4.1"


def test_put_without_config_file_creates_it(tmp_path: Path) -> None:
    path = tmp_path / "new" / "config.json"
    client = TestClient(create_app(path))
    data = _get(client)

    response = _put(client, data["config"], data["revision"])

    assert response.status_code == 200, response.text
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["agents"]["defaults"]["model"]


def test_stale_revision_is_rejected(client: TestClient, config_path: Path) -> None:
    data = _get(client)
    config_path.write_text(json.dumps({"agents": {"defaults": {"model": "other"}}}), encoding="utf-8")

    response = _put(client, data["config"], data["revision"])

    assert response.status_code == 409
    assert json.loads(config_path.read_text(encoding="utf-8"))["agents"]["defaults"]["model"] == "other"


def test_blank_secrets_keep_stored_values(client: TestClient, config_path: Path) -> None:
    data = _get(client)
    submitted = data["config"]
    submitted["providers"]["openai"]["apiKey"] = ""
    submitted["channels"]["feishu"]["encryptKey"] = ""
    submitted["tools"]["mcpServers"]["robot"]["headers"]["Authorization"] = ""

    response = _put(client, submitted, data["revision"])

    assert response.status_code == 200, response.text
    on_disk = json.loads(config_path.read_text(encoding="utf-8"))
    assert on_disk["providers"]["openai"]["apiKey"] == "sk-openai-secret"
    assert on_disk["channels"]["feishu"]["encryptKey"] == "feishu-encrypt"
    assert (
        on_disk["tools"]["mcpServers"]["robot"]["headers"]["Authorization"]
        == "Bearer mcp-secret"
    )
    refreshed = _get(client)
    assert refreshed["secret_fields"]["providers.openai.apiKey"] is True
    assert all(secret not in json.dumps(refreshed) for secret in SECRETS)


def test_non_blank_secrets_replace_stored_values(client: TestClient, config_path: Path) -> None:
    data = _get(client)
    submitted = data["config"]
    submitted["providers"]["openai"]["apiKey"] = "replacement-key"
    submitted["channels"]["email"]["imapPassword"] = "replacement-imap"

    response = _put(client, submitted, data["revision"])

    assert response.status_code == 200, response.text
    on_disk = json.loads(config_path.read_text(encoding="utf-8"))
    assert on_disk["providers"]["openai"]["apiKey"] == "replacement-key"
    assert on_disk["channels"]["email"]["imapPassword"] == "replacement-imap"


def test_clear_secrets_empties_only_named_paths(client: TestClient, config_path: Path) -> None:
    data = _get(client)
    submitted = data["config"]
    submitted["providers"]["openai"]["apiKey"] = ""

    response = _put(
        client,
        submitted,
        data["revision"],
        clear_secrets=[
            "providers.openai.apiKey",
            "channels.slack.botToken",
            "tools.mcpServers.robot.env.ROBOT_TOKEN",
        ],
    )

    assert response.status_code == 200, response.text
    on_disk = json.loads(config_path.read_text(encoding="utf-8"))
    assert on_disk["providers"]["openai"]["apiKey"] == ""
    assert on_disk["channels"]["slack"]["botToken"] == ""
    assert on_disk["tools"]["mcpServers"]["robot"]["env"]["ROBOT_TOKEN"] == ""
    assert on_disk["channels"]["slack"]["appToken"] == "slack-app-token"
    refreshed = _get(client)
    assert refreshed["secret_fields"]["providers.openai.apiKey"] is False


def test_null_secret_container_keeps_stored_values(client: TestClient, config_path: Path) -> None:
    data = _get(client)
    submitted = data["config"]
    submitted["providers"]["openai"]["extraHeaders"] = None

    response = _put(client, submitted, data["revision"])

    assert response.status_code == 200, response.text
    on_disk = json.loads(config_path.read_text(encoding="utf-8"))
    assert on_disk["providers"]["openai"]["extraHeaders"] == {
        "X-Custom-Token": "header-secret"
    }


def test_unknown_clear_path_is_rejected(client: TestClient, config_path: Path) -> None:
    data = _get(client)
    before = config_path.read_bytes()

    response = _put(client, data["config"], data["revision"], clear_secrets=["providers.openai.nope"])

    assert response.status_code == 400
    assert config_path.read_bytes() == before


def test_invalid_config_is_not_written_and_does_not_echo_secrets(
    client: TestClient, config_path: Path
) -> None:
    data = _get(client)
    before = config_path.read_bytes()
    submitted = data["config"]
    submitted["resource_registry"] = {"url": "ftp://invalid"}
    submitted["providers"]["openai"]["apiKey"] = "leaked-in-error"

    response = _put(client, submitted, data["revision"])

    assert response.status_code == 422
    assert "leaked-in-error" not in response.text
    assert config_path.read_bytes() == before
    detail = response.json()["detail"]
    assert detail["errors"][0]["msg"]
    assert "input" not in detail["errors"][0]


def test_malformed_request_body_does_not_echo_secrets(client: TestClient) -> None:
    response = client.put(
        "/api/settings",
        json={"config": ["leaked-body-secret"], "revision": "x"},
    )

    assert response.status_code == 422
    assert "leaked-body-secret" not in response.text
    assert "input" not in response.text


def test_token_is_required_when_configured(config_path: Path) -> None:
    client = TestClient(create_app(config_path, token="t0ken"))

    assert client.get("/api/settings").status_code == 401
    assert client.get("/api/settings", headers={"X-Paos-Token": "t0ken"}).status_code == 200
    assert (
        client.get("/api/settings", headers={"Authorization": "Bearer t0ken"}).status_code
        == 200
    )


def test_health(client: TestClient) -> None:
    assert client.get("/api/health").json() == {"status": "ok"}


def test_static_index_is_served(config_path: Path) -> None:
    index_path = STATIC_DIR / "index.html"
    if not index_path.is_file():
        pytest.skip("static page is written by the frontend change")

    response = TestClient(create_app(config_path)).get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert TestClient(create_app(config_path)).get("/favicon.ico").status_code == 204


def test_cli_web_help_lists_options() -> None:
    result = CliRunner().invoke(cli_app, ["web", "--help"])

    assert result.exit_code == 0, result.output
    for option in ("--config", "--host", "--port"):
        assert option in result.output
    # Local-only by design: there is no remote/token mode in the CLI.
    assert "--token" not in result.output


def test_cli_web_refuses_non_loopback() -> None:
    result = CliRunner().invoke(cli_app, ["web", "--host", "0.0.0.0"])

    assert result.exit_code == 2
    assert "local-only" in result.output
