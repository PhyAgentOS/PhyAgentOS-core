from __future__ import annotations

import httpx
import pytest

from PhyAgentOS.skill_runtime.registry import RegistryClient, RegistryError


def test_node_uses_skill_lock_digest_and_probes_size() -> None:
    digest = "a" * 64

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/v1/forge-nodes/"):
            return httpx.Response(
                200,
                json={
                    "artifact_id": "example-node",
                    "download_url": "https://downloads.example/node.tar.gz",
                    "mode": "direct",
                },
            )
        assert request.method == "HEAD"
        assert request.url == "https://downloads.example/node.tar.gz"
        return httpx.Response(200, headers={"Content-Length": "321"})

    with httpx.Client(
        transport=httpx.MockTransport(handle), follow_redirects=True
    ) as http:
        artifact = RegistryClient("https://registry.example", client=http).node(
            "example-node",
            expected_sha256=digest,
        )

    assert artifact.sha256 == digest
    assert artifact.size == 321


def test_node_without_registry_or_lock_digest_is_rejected() -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "download_url": "https://downloads.example/node.tar.gz",
                "size": 321,
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(RegistryError, match="invalid sha256"):
            RegistryClient("https://registry.example", client=http).node("example-node")


def test_node_rejects_digest_that_disagrees_with_skill_lock() -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "download_url": "https://downloads.example/node.tar.gz",
                "sha256": "b" * 64,
                "size": 321,
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        registry = RegistryClient("https://registry.example", client=http)
        with pytest.raises(RegistryError, match="does not match the expected digest"):
            registry.node("example-node", expected_sha256="a" * 64)


def test_node_size_probe_falls_back_to_single_byte_range() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/v1/forge-nodes/"):
            return httpx.Response(
                200,
                json={"download_url": "https://downloads.example/node.tar.gz"},
            )
        if request.method == "HEAD":
            return httpx.Response(405)
        assert request.headers["Range"] == "bytes=0-0"
        return httpx.Response(
            206,
            headers={"Content-Range": "bytes 0-0/654"},
            content=b"x",
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        artifact = RegistryClient("https://registry.example", client=http).node(
            "example-node",
            expected_sha256="c" * 64,
        )

    assert artifact.size == 654
