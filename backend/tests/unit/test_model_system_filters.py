"""Regression coverage for the built-in Models albums."""

from __future__ import annotations

import os


def _seed_models() -> None:
    from app.core import db as dbmod

    now = dbmod.now_ms()

    def seed(conn):
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO roots(kind,path,path_key,label,is_default,source,available,created_at) "
            "VALUES ('comfyui','C:/probe','c:/probe','Probe',1,'config',1,?)", (now,))
        root_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        def model(name: str, integrity: str, *, missing: bool = False) -> None:
            conn.execute(
                "INSERT INTO models(name,category,integrity,created_at,updated_at,missing_since) "
                "VALUES (?,'checkpoints',?,?,?,?)",
                (name, integrity, now, now, now if missing else None))
            model_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO model_files(model_id,root_id,abs_path,path_key,rel_path,folder,"
                "filename,stem,ext,size,mtime_ns,fingerprint,format,first_seen_at,last_seen_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (model_id, root_id, f"C:/probe/{name}.safetensors",
                 os.path.normcase(f"C:/probe/{name}.safetensors"),
                 f"checkpoints/{name}.safetensors", "checkpoints", f"{name}.safetensors",
                 name, ".safetensors", 1, now * 1_000_000, f"fp-{name}", "safetensors",
                 now, now))
            file_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute("UPDATE models SET primary_file_id = ? WHERE id = ?", (file_id, model_id))

        model("good", "ok")
        model("damaged", "invalid_header")
        model("gone", "ok", missing=True)

        def package(name: str, *, missing: bool = False) -> int:
            conn.execute(
                "INSERT INTO node_packages(folder_name,path_key,abs_path,display_name,fingerprint,"
                "created_at,updated_at,missing_since) VALUES (?,?,?,?,?,?,?,?)",
                (name, f"c:/probe/{name}", f"C:/probe/{name}", name, f"fp-{name}",
                 now, now, now if missing else None))
            return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        package("present-package")
        missing_package = package("gone-package", missing=True)
        conn.execute(
            "INSERT INTO node_classes(package_id,node_id,display_name,source_strategy,created_at,"
            "updated_at) VALUES (?, 'GoneNode', 'Gone node', 'S1', ?, ?)",
            (missing_package, now, now))

        def workflow(name: str, *, missing: bool = False) -> None:
            conn.execute(
                "INSERT INTO workflows(root_id,abs_path,path_key,rel_path,folder,name,size,mtime_ns,"
                "fingerprint,created_at,updated_at,missing_since) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (root_id, f"C:/probe/{name}.json", f"c:/probe/{name}.json", f"{name}.json", "",
                 name, 1, now * 1_000_000, f"fp-{name}", now, now, now if missing else None))

        workflow("present-workflow")
        workflow("gone-workflow", missing=True)

        def output(name: str, *, missing: bool = False) -> None:
            conn.execute(
                "INSERT INTO outputs(root_id,abs_path,path_key,rel_path,folder,filename,ext,size,mtime_ns,"
                "created_at_file,fingerprint,created_at,updated_at,missing_since) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (root_id, f"C:/probe/{name}.png", f"c:/probe/{name}.png", f"{name}.png", "",
                 f"{name}.png", ".png", 1, now * 1_000_000, now, f"fp-{name}", now, now,
                 now if missing else None))

        output("present-output")
        output("gone-output", missing=True)
        conn.commit()

    dbmod.writer().run(seed)


def test_models_system_album_filters_do_not_fall_back_to_all_models(hermetic_client):
    """The two health albums must pass real, restrictive filters to /models."""
    _seed_models()

    missing = hermetic_client.get("/api/v1/models?missing_files_only=true")
    assert missing.status_code == 200, missing.text
    assert missing.json()["page"]["total"] == 1
    assert [item["name"] for item in missing.json()["items"]] == ["gone"]

    integrity = hermetic_client.get("/api/v1/models?integrity_not_ok=true")
    assert integrity.status_code == 200, integrity.text
    assert integrity.json()["page"]["total"] == 1
    assert [item["name"] for item in integrity.json()["items"]] == ["damaged"]


def test_missing_files_filter_is_restrictive_in_every_asset_tab(hermetic_client):
    """The shared Missing files album must never widen a tab to all of its rows."""
    _seed_models()

    checks = {
        "/api/v1/models?missing_files_only=true": 1,
        "/api/v1/node-packages?missing_files_only=true": 1,
        "/api/v1/node-classes?missing_files_only=true": 1,
        "/api/v1/workflows?missing_files_only=true": 1,
        "/api/v1/outputs?missing_files_only=true": 1,
    }
    for path, expected in checks.items():
        response = hermetic_client.get(path)
        assert response.status_code == 200, f"{path}: {response.text}"
        assert response.json()["page"]["total"] == expected, path
