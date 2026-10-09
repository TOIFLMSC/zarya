import json

import httpx
import pytest
from test_backup import source_at
from test_dialogue import FakeModel, private_ready, run_one
from test_telegram import event, store_at

from zarya.app import create_app
from zarya.backup import MARKER, backup, restore, verify
from zarya.config import Config
from zarya.dialogue import DialogueEngine
from zarya.dialogue_logic import instructions
from zarya.models import Settings
from zarya.prompts import DEFAULT_PROMPTS, MAX_BYTES, PromptBundle


def document(marker="TEST_LOCAL_PROMPT"):
    fragments = dict(DEFAULT_PROMPTS.fragments)
    fragments["dialogue.identity"] = marker
    return {"schema_version": 1, "fragments": fragments}


def bundle(marker="TEST_LOCAL_PROMPT"):
    return PromptBundle.parse(json.dumps(document(marker)).encode(), set(DEFAULT_PROMPTS.fragments))


@pytest.mark.parametrize("kind", ["missing", "extra", "type", "version", "duplicate", "oversize"])
def test_invalid_bundle_rejected_without_echoing_content(tmp_path, kind):
    doc = document("DO_NOT_ECHO_CONTENT")
    if kind == "missing":
        del doc["fragments"]["dialogue.identity"]
    if kind == "extra":
        doc["fragments"]["unknown"] = "value"
    if kind == "type":
        doc["fragments"]["dialogue.identity"] = 123
    if kind == "version":
        doc["schema_version"] = 2
    raw = json.dumps(doc)
    if kind == "duplicate":
        raw = raw.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1')
    if kind == "oversize":
        raw = " " * (MAX_BYTES + 1)
    path = tmp_path / "prompts.json"
    path.write_text(raw)
    with pytest.raises(ValueError) as exc:
        PromptBundle.load(path)
    assert "DO_NOT_ECHO_CONTENT" not in str(exc.value)


def test_bundle_is_immutable_and_braces_are_literal():
    value = bundle("literal {unknown} ${path} {% code %}")
    assert "literal {unknown}" in instructions(Settings(), False, prompts=value)
    with pytest.raises(TypeError):
        value.fragments["dialogue.identity"] = "changed"
    assert value.sha256 == bundle("literal {unknown} ${path} {% code %}").sha256
    assert value.sha256 != DEFAULT_PROMPTS.sha256


def test_cli_requires_local_file_without_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("ZARYA_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ZARYA_PROMPTS_FILE", raising=False)
    monkeypatch.delenv("ZARYA_TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config = Config.from_env()
    assert config.prompts_file == tmp_path / "prompts.json"
    with pytest.raises(ValueError, match="пакет промптов недоступен"):
        create_app(config)
    assert not (tmp_path / "zarya.sqlite3").exists()


async def test_fresh_install_starts_with_explicit_neutral_file(tmp_path, monkeypatch):
    monkeypatch.setenv("ZARYA_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ZARYA_PROMPTS_FILE", raising=False)
    monkeypatch.delenv("ZARYA_TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    (tmp_path / "prompts.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "fragments": dict(DEFAULT_PROMPTS.fragments),
            }
        )
    )
    app = create_app(Config.from_env())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8787"
        ) as client:
            assert (await client.get("/healthz")).status_code == 200
            assert (await client.get("/api/session")).json()["setup_required"] is True
        assert app.state.telegram_store.prompts.sha256 == DEFAULT_PROMPTS.sha256
        assert (tmp_path / "bootstrap-token.txt").is_file()


async def test_live_and_paid_replay_use_injected_bundle_recorded_does_not(tmp_path):
    async with store_at(tmp_path) as store:
        await private_ready(store)
        store.prompts = bundle("INSTALLATION_A")
        model = FakeModel()
        engine = DialogueEngine(store, model)
        await store.ingest("999", "test_bot", [event(2, text="Проверка")])
        work = await run_one(engine)
        detail = await engine.details(work["run_id"])
        assert "INSTALLATION_A" in model.calls[0]["instructions"]
        assert detail["snapshot"]["prompt_bundle_sha256"] == store.prompts.sha256
        store.prompts = bundle("INSTALLATION_B")
        recorded = await engine.replay(work["run_id"], "recorded")
        assert recorded["response"] == detail["response"] and len(model.calls) == 1
        replay = await engine.replay(work["run_id"], "paid", (await store.db.settings()).version)
        assert "INSTALLATION_B" in model.calls[-1]["instructions"]
        assert replay["snapshot"]["prompt_bundle_sha256"] == store.prompts.sha256


async def test_backup_restores_private_prompt_bundle_with_quarantine(tmp_path):
    source = await source_at(tmp_path / "data")
    raw = json.dumps(document())
    (source / "prompts.json").write_text(raw)
    backup(source, tmp_path / "backup")
    assert "prompts.json" in verify(tmp_path / "backup")["files"]
    restore(tmp_path / "backup", tmp_path / "restored")
    assert (tmp_path / "restored" / MARKER).is_file()
    assert (tmp_path / "restored/prompts.json").read_text() == raw
    assert not (tmp_path / "restored/openai-api-key.txt").exists()
    (tmp_path / "backup/prompts.json").write_text("tampered")
    with pytest.raises(ValueError, match="повреждён"):
        verify(tmp_path / "backup")
