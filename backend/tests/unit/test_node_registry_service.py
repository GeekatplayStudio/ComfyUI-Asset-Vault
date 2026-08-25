"""Read-only node catalogue contracts: no package download is involved."""

from __future__ import annotations


def test_registry_endpoint_uses_bounded_metadata_and_legacy_provenance(hermetic_client, monkeypatch, tmp_path):
    from app.core import config_service
    from app.services import node_registry_service as registry

    monkeypatch.setattr(registry, "CACHE_PATH", tmp_path / "registry.json")
    config_service.set_config({"online_enabled": True})
    monkeypatch.setattr(registry, "_fetch_cnr", lambda: [{
        "id": "example.safe-node", "name": "Safe Example",
        "description": "metadata only", "repository": "https://github.com/example/safe-node",
        "publisher": {"name": "Example"},
        "latest_version": {"version": "1.2.3", "dependencies": ["thing>=1"]},
    }])
    response = hermetic_client.get("/api/v1/node-registry?refresh=true", headers={"X-Vault-Request": "1"})
    assert response.status_code == 200
    body = response.json()
    item = next(x for x in body["items"] if x["id"] == "example.safe-node")
    assert item["source"] == "comfy_registry"
    assert item["version"] == "1.2.3"
    assert item["warnings"]
    assert body["meta"]["source"] == "https://api.comfy.org"


def test_registry_does_not_fetch_when_offline_without_cache(hermetic_client, monkeypatch, tmp_path):
    from app.core import config_service
    from app.services import node_registry_service as registry

    monkeypatch.setattr(registry, "CACHE_PATH", tmp_path / "registry.json")
    monkeypatch.setattr(registry, "_fetch_cnr", lambda: (_ for _ in ()).throw(AssertionError("offline fetch")))
    config_service.set_config({"online_enabled": False})
    response = hermetic_client.get("/api/v1/node-registry", headers={"X-Vault-Request": "1"})
    assert response.status_code == 200
    assert response.json()["meta"]["online_enabled"] is False
