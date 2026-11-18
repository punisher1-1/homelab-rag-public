#!/usr/bin/env python3
"""Tests for POST /v1/search (added 2026-10-16). Retrieval is faked, so no
database, embedder or reranker is needed:

    pip install fastapi httpx psycopg[binary] requests
    python3 -m pytest -q test_search.py
"""
import importlib
import os
import sys

import pytest
from fastapi.testclient import TestClient

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN = "s" * 32
ROWS = [
    (1, "IT301 Week 4", "dropzone/it301/week4.pdf",
     "[week4 · §2 Files · p. 9]\nA sequential file is read in order from start to end.",
     0.7, {"collection": "it301", "section": "2 Files", "printed_page": 9, "type": "reference"}),
    (2, None, "vault/Lab/files.md", "Random access " * 400, 0.5, {}),
]


def load(monkeypatch, token=TOKEN, collections="it301,nist"):
    monkeypatch.setenv("DB_PASSWORD", "unused")
    monkeypatch.setenv("SEARCH_TOKEN", token)
    monkeypatch.setenv("COLLECTIONS", collections)
    monkeypatch.setenv("SEARCH_TEXT_CHARS", "1000")
    sys.path.insert(0, HERE)
    sys.modules.pop("app", None)
    app = importlib.import_module("app")
    calls = []

    def fake(question, collection=None, source=None):
        calls.append((question, collection, source))
        return ROWS, {"top_sim": 0.7, "top_rerank": None, "n": 2}
    monkeypatch.setattr(app, "hybrid_retrieve", fake)
    monkeypatch.setattr(app, "indexed_collections", lambda: ["it help", "it301", "pmi"])
    return app, TestClient(app.app), calls


def post(client, body, token=TOKEN):
    return client.post("/v1/search", json=body, headers={"Authorization": "Bearer " + token} if token else {})


def test_returns_passages_with_locators_and_no_generation(monkeypatch):
    app, client, calls = load(monkeypatch)
    monkeypatch.setattr(app, "resolve_model", lambda: pytest.fail("search must not generate"))
    r = post(client, {"query": "sequential file", "collection": "IT301", "top_k": 2})
    assert r.status_code == 200
    data = r.json()
    assert calls == [("sequential file", "it301", None)]
    first, second = data["results"]
    assert first["title"] == "IT301 Week 4" and first["locator"] == "§2 Files, p. 9"
    assert first["text"] == "A sequential file is read in order from start to end."  # ingest label stripped
    assert second["title"] == "files.md" and second["truncated"] and len(second["text"]) == 1000
    assert data["weak"] is False and data["collections"] == ["it help", "it301", "pmi"]


def test_token_is_required_and_endpoint_is_off_without_one(monkeypatch):
    _, client, calls = load(monkeypatch)
    assert post(client, {"query": "x"}, token=None).status_code == 401
    assert post(client, {"query": "x"}, token="t" * 32).status_code == 401
    _, client, _ = load(monkeypatch, token="")
    assert post(client, {"query": "x"}).json() == {"error": "search_disabled"}
    assert calls == []


@pytest.mark.parametrize("body,code", [
    ({"query": ""}, "invalid_query"),
    ({"query": "x" * 1001}, "invalid_query"),
    ({"query": "x", "collection": "atf"}, "unknown_collection"),
    ({"query": "x", "source": "web"}, "invalid_source"),
    ({"query": "x", "top_k": 50}, "invalid_top_k"),
    ({"query": "x", "top_k": "3"}, "invalid_top_k"),
])
def test_arguments_are_validated(monkeypatch, body, code):
    _, client, calls = load(monkeypatch)
    r = post(client, body)
    assert r.status_code == 400 and r.json()["error"] == code and calls == []


def test_retrieval_failure_hides_detail(monkeypatch):
    app, client, _ = load(monkeypatch)

    def broken(*a, **k):
        raise RuntimeError("password=hunter2 at 192.0.2.67")
    monkeypatch.setattr(app, "hybrid_retrieve", broken)
    r = post(client, {"query": "x"})
    assert r.status_code == 502 and r.json() == {"error": "retrieval_failed"}


def test_collections_come_from_the_index_not_the_env(monkeypatch):
    app, client, calls = load(monkeypatch, collections="")
    r = post(client, {"query": "reserve", "collection": "PMI"})
    assert r.status_code == 200 and calls == [("reserve", "pmi", None)]
    r = post(client, {"query": "x", "collection": "nist"})
    assert r.json() == {"error": "unknown_collection", "collections": ["it help", "it301", "pmi"]}


def test_collection_list_is_cached(monkeypatch):
    app, _, _ = load(monkeypatch)
    monkeypatch.undo()
    monkeypatch.setenv("DB_PASSWORD", "unused")
    queries = []

    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql): queries.append(sql)
        def fetchall(self): return [("pmi",), ("nist",), (None,)]

    class Conn(Cursor):
        def cursor(self): return Cursor()
    monkeypatch.setattr(app.psycopg, "connect", lambda **k: Conn())
    app._collection_cache.update(at=0.0, names=[])
    assert app.indexed_collections() == ["pmi", "nist"] and app.indexed_collections() == ["pmi", "nist"]
    assert len(queries) == 1 and "DISTINCT" in queries[0]


def test_it_collection_is_exact_not_a_prefix(monkeypatch):
    app, client, calls = load(monkeypatch)
    monkeypatch.setattr(app, "indexed_collections", lambda: ["it", "it help", "it301"])
    r = post(client, {"query": "pi-hole local dns", "collection": " IT "})
    assert r.status_code == 200 and calls == [("pi-hole local dns", "it", None)]
    sql, params = app.scope_clause("it")
    assert sql.endswith("metadata->>'collection' = %s") and params[-1] == "it"
