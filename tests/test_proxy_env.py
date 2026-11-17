"""Proxy environment values that httpx cannot parse are normalized at CLI startup."""

from __future__ import annotations

import os

import httpx
import pytest
from typer.testing import CliRunner

from PhyAgentOS.cli.commands import app
from PhyAgentOS.utils.proxy import normalize_proxy_env

_BROKEN_NO_PROXY = "localhost,[::1],::1/128,fd00::/8"
_FIXED_NO_PROXY = "localhost,::1"


@pytest.fixture(autouse=True)
def _clean_proxy_env(monkeypatch):
    for var in list(os.environ):
        if var.lower().endswith("_proxy"):
            monkeypatch.delenv(var)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # The unbracketed duplicate collapses into the existing ::1.
        ("localhost,127.0.0.1,::1,[::1]", "localhost,127.0.0.1,::1"),
        # httpx has no port-scoped IPv6 bypass, so the port is dropped.
        ("[fd00::1]:8080,[FD00::2],[fe80::1%eth0]", "fd00::1,FD00::2,fe80::1%eth0"),
        # A /128 range is exactly one address and keeps bypassing it.
        ("localhost,127.0.0.0/8,::1/128,[::2]/128", "localhost,127.0.0.0/8,::1,::2"),
        # Wider IPv6 ranges and invalid masks go; IPv4 ranges never crashed httpx.
        ("10.0.0.0/8,fd7a:115c:a1e0::/48,[fd00::]/8,::1/129", "10.0.0.0/8"),
        ("localhost;[::1];<local>", "localhost,::1,<local>"),
        # Non-ASCII hosts fail IDNA after httpx adds its "*" prefix; punycode works.
        ("中国.cn,.中国.cn,*.中国.cn,xn--fiqs8s.cn", "xn--fiqs8s.cn"),
    ],
)
@pytest.mark.parametrize("var", ["NO_PROXY", "no_proxy", "No_Proxy"])
def test_normalizes_no_proxy_entries(monkeypatch, var, value, expected):
    monkeypatch.setenv(var, value)
    normalize_proxy_env()
    assert os.environ[var] == expected


def test_keeps_supported_no_proxy_entries(monkeypatch):
    value = "example.com, .corp,*.internal,10.0.0.0/8,[127.0.0.1],fe80::1%eth0,http://[::1]:8080,*"
    monkeypatch.setenv("NO_PROXY", value)
    normalize_proxy_env()
    assert os.environ["NO_PROXY"] == value


@pytest.mark.parametrize("var", ["ALL_PROXY", "all_proxy", "HTTPS_PROXY", "http_proxy", "Https_Proxy"])
@pytest.mark.parametrize("scheme", ["socks", "SOCKS", "Socks"])
def test_rewrites_socks_alias(monkeypatch, var, scheme):
    monkeypatch.setenv(var, f"{scheme}://127.0.0.1:7897/")
    normalize_proxy_env()
    assert os.environ[var] == "socks5://127.0.0.1:7897/"


@pytest.mark.parametrize("url", ["socks5://127.0.0.1:7897", "socks5h://127.0.0.1:7897", "http://127.0.0.1:7897"])
def test_keeps_supported_proxy_urls(monkeypatch, url):
    monkeypatch.setenv("ALL_PROXY", url)
    normalize_proxy_env()
    assert os.environ["ALL_PROXY"] == url


@pytest.mark.parametrize(
    "no_proxy",
    [_BROKEN_NO_PROXY, "localhost,127.0.0.0/8,::1/128", "localhost;[::1]", "[::1],中国.cn"],
)
def test_httpx_client_builds_and_bypasses_loopback(monkeypatch, no_proxy):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")
    monkeypatch.setenv("ALL_PROXY", "socks://127.0.0.1:7897")
    monkeypatch.setenv("No_Proxy", no_proxy)
    normalize_proxy_env()
    with httpx.Client() as client:
        # Building the client is not enough: loopback must still skip the proxy.
        assert client._transport_for_url(httpx.URL("http://[::1]:8000/")) is client._transport


def test_cli_entry_normalizes_before_commands(monkeypatch, tmp_path):
    monkeypatch.setenv("NO_PROXY", _BROKEN_NO_PROXY)
    result = CliRunner().invoke(
        app, ["provider", "list", "--json", "-c", str(tmp_path / "config.json")]
    )
    assert result.exit_code == 0, result.output
    assert os.environ["NO_PROXY"] == _FIXED_NO_PROXY
