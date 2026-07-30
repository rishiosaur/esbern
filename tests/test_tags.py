from __future__ import annotations

import stat

from esbern.tags import FileTags, TagStore


def test_tag_records_are_scoped_to_the_sync_root(tmp_path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    store = TagStore()

    store.scope_to(first)
    store.set("book.pdf", ["History"])
    store.scope_to(second)

    assert store.get("book.pdf") == []
    store.set("book.pdf", ["Science"])

    store.scope_to(first)
    assert store.get("book.pdf") == ["History"]
    store.scope_to(second)
    assert store.get("book.pdf") == ["Science"]


def test_scoping_migrates_a_legacy_relative_key_once(tmp_path) -> None:
    store = TagStore(files={"book.pdf": FileTags(["Fiction"])})

    store.scope_to(tmp_path / "first")
    assert store.get("book.pdf") == ["Fiction"]
    store.scope_to(tmp_path / "second")
    assert store.get("book.pdf") == []


def test_tag_store_rejects_paths_outside_its_root(tmp_path) -> None:
    store = TagStore()
    store.scope_to(tmp_path)

    try:
        store.set("../outside.pdf", ["Private"])
    except ValueError as error:
        assert "within the sync root" in str(error)
    else:
        raise AssertionError("unsafe tag path was accepted")


def test_saved_tag_store_is_private(tmp_path, monkeypatch) -> None:
    import esbern.tags as tags_module

    config_dir = tmp_path / "config"
    tags_path = config_dir / "tags.json"
    monkeypatch.setattr(tags_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(tags_module, "TAGS_PATH", tags_path)

    store = TagStore()
    store.scope_to(tmp_path / "library")
    store.set("book.pdf", ["Research"])
    store.save()

    assert stat.S_IMODE(tags_path.stat().st_mode) == 0o600
