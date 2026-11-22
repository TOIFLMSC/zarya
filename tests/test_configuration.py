import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from test_app import running, setup
from test_dialogue import FakeModel, private_ready, run_one
from test_photos import PhotoEngine, PhotoTransport, VisionModel, photo
from test_telegram import event, store_at

from zarya.app import create_app
from zarya.config import Config
from zarya.configuration import document, validate
from zarya.configuration_bridge import ConfigurationBridge
from zarya.database import ConflictError
from zarya.dialogue import DialogueEngine
from zarya.models import Settings
from zarya.prompts import DEFAULT_PROMPTS, PromptBundle


async def test_pre_g6_release_owned_notes_upgrade_edit_and_rollback(tmp_path):
    from zarya.configuration import canonical, digest
    from zarya.obsidian_fs import digest as file_digest

    async with bridge_at(tmp_path) as (store, bridge):
        old = (await bridge.current())["document"]
        old["settings"].pop("memory_retrieval")
        old["settings"].pop("participant_context_messages")
        old["settings"].pop("participant_summaries_enabled")
        old["settings"].pop("participant_summary_prompt")
        old["settings"].pop("topic_context_chat_id")
        old["settings"].pop("message_batch_chat_id")
        raw = canonical(old["settings"])
        # Recreate a release and an owned note from before the new setting existed.
        f = await store.db.one(
            "SELECT prefix,suffix,body FROM configuration_files WHERE path='Модели и лимиты.md'"
        )
        body = json.loads(f[2])
        body.pop("memory_retrieval")
        body.pop("participant_context_messages")
        body.pop("participant_summaries_enabled")
        body.pop("participant_summary_prompt")
        body.pop("topic_context_chat_id")
        body.pop("message_batch_chat_id")
        body = json.dumps(body, ensure_ascii=False, indent=2)
        note = (f[0] + body + f[1]).encode()
        (bridge.root / "Модели и лимиты.md").write_bytes(note)
        async with store.db.transaction() as c:
            await c.execute("UPDATE settings SET document=?", (raw,))
            await c.execute("UPDATE settings_versions SET document=? WHERE version=1", (raw,))
            await c.execute(
                "UPDATE configuration_releases SET settings_json=?,hash=? WHERE version=1",
                (raw, digest(old)),
            )
            await c.execute(
                "UPDATE configuration_files SET body=?,hash=? WHERE path='Модели и лимиты.md'",
                (body, file_digest(note)),
            )
        await bridge.scan()
        await bridge.scan()
        assert not (await bridge.listing())["drafts"]
        refreshed = await store.db.one(
            "SELECT body FROM configuration_files WHERE path='Модели и лимиты.md'"
        )
        assert json.loads(refreshed[0])["memory_retrieval"] == "shadow"
        assert json.loads(refreshed[0])["participant_context_messages"] == 20
        assert json.loads(refreshed[0])["topic_context_chat_id"] == ""
        assert json.loads(refreshed[0])["message_batch_chat_id"] == ""
        assert json.loads(refreshed[0])["participant_summaries_enabled"] is True
        assert (
            json.loads(refreshed[0])["participant_summary_prompt"]
            == Settings().participant_summary_prompt
        )
        draft = await edit(bridge)
        assert await bridge.decide(draft["id"], "apply", 1, draft["hash"]) == 2
        rollback = await bridge.rollback(1, 2)
        detail = await bridge.details(rollback)
        assert await bridge.decide(rollback, "apply", 2, detail["hash"]) == 3
        assert (await store.db.settings()).settings.memory_retrieval == "shadow"
        assert (
            await store.db.one("SELECT settings_json FROM configuration_releases WHERE version=1")
        )[0] == raw


async def test_edited_old_model_note_can_be_applied_before_schema_refresh(tmp_path):
    from zarya.configuration import canonical, digest
    from zarya.obsidian_fs import digest as file_digest

    async with bridge_at(tmp_path) as (store, bridge):
        old = (await bridge.current())["document"]
        old["settings"].pop("memory_retrieval")
        raw = canonical(old["settings"])
        f = await store.db.one(
            "SELECT prefix,suffix,body FROM configuration_files WHERE path='Модели и лимиты.md'"
        )
        value = json.loads(f[2])
        value.pop("memory_retrieval")
        before = json.dumps(value, ensure_ascii=False, indent=2)
        async with store.db.transaction() as c:
            await c.execute("UPDATE settings SET document=?", (raw,))
            await c.execute("UPDATE settings_versions SET document=? WHERE version=1", (raw,))
            await c.execute(
                "UPDATE configuration_releases SET settings_json=?,hash=? WHERE version=1",
                (raw, digest(old)),
            )
            await c.execute(
                "UPDATE configuration_files SET body=?,hash=? WHERE path='Модели и лимиты.md'",
                (before, file_digest((f[0] + before + f[1]).encode())),
            )
        value["video_max_frames"] = 8
        body = json.dumps(value, ensure_ascii=False, indent=2)
        (bridge.root / "Модели и лимиты.md").write_text(f[0] + body + f[1], encoding="utf-8")
        await bridge.scan()
        await bridge.scan()
        draft = (await bridge.listing())["drafts"][0]
        assert await bridge.decide(draft["id"], "apply", 1, draft["hash"]) == 2
        setting = (await store.db.settings()).settings
        assert setting.memory_retrieval == "shadow" and setting.video_max_frames == 8


@asynccontextmanager
async def bridge_at(path):
    async with store_at(path) as store:
        await store.db.initialize_configuration(DEFAULT_PROMPTS)
        vault = path / "vault"
        vault.mkdir()
        config = path / "obsidian.local.json"
        config.write_text(json.dumps({"enabled": True, "project_path": str(vault)}))
        bridge = ConfigurationBridge(store, config)
        await bridge.scan()
        yield store, bridge


async def edit(bridge, path="Характер.md", replace=None):
    f = await bridge.db.one(
        "SELECT prefix,suffix,body FROM configuration_files WHERE path=?", (path,)
    )
    body = replace or f[2].replace('"persona":', '"persona":')
    if replace is None:
        data = json.loads(f[2])
        data["persona"] = "SENTINEL_DRAFT"
        body = json.dumps(data, ensure_ascii=False, indent=2)
    (bridge.root / path).write_text(f[0] + body + f[1], encoding="utf-8")
    await bridge.scan()
    await bridge.scan()
    return (await bridge.listing())["drafts"][0]


async def test_export_draft_is_inert_apply_and_text_rollback(tmp_path):
    async with bridge_at(tmp_path) as (store, bridge):
        assert (bridge.root / "Начало.md").is_file()
        draft = await edit(bridge)
        assert (await store.db.settings()).settings.persona != "SENTINEL_DRAFT"
        detail = await bridge.details(draft["id"])
        assert len(detail["nodes"]) == 1
        version = await bridge.decide(draft["id"], "apply", 1, draft["hash"])
        assert version == 2 and (await store.db.settings()).settings.persona == "SENTINEL_DRAFT"
        assert await bridge.decide(draft["id"], "apply", 1, draft["hash"]) == 2
        await bridge.scan()
        assert not (await bridge.listing())["drafts"]
        undo = await bridge.rollback(1, 2)
        detail = await bridge.details(undo)
        assert detail["origin"] == "rollback"
        assert await bridge.decide(undo, "apply", 2, detail["hash"]) == 3
        assert (await store.db.settings()).settings.persona == Settings().persona


async def test_prompt_only_release_pins_existing_dialogue_and_invalidates_paid_confirmation(
    tmp_path,
):
    async with bridge_at(tmp_path) as (store, bridge):
        await private_ready(store)
        model = FakeModel()
        engine = DialogueEngine(store, model)
        await store.ingest("999", "test_bot", [event(2, text="Привет")])
        work = await engine.claim("999")
        draft = await edit(bridge, "Инструкции/dialogue.identity.md", "SENTINEL_NEW_PROMPT")
        await bridge.decide(draft["id"], "apply", 1, draft["hash"])
        await engine.execute(work)
        assert "SENTINEL_NEW_PROMPT" not in model.calls[0]["instructions"]
        await store.ingest("999", "test_bot", [event(3, text="Привет снова")])
        new = await run_one(engine)
        assert "SENTINEL_NEW_PROMPT" in model.calls[1]["instructions"]
        assert (await engine.details(new["run_id"]))["snapshot"]["settings_version"] == 2
        with pytest.raises(ValueError, match="Настройки изменились"):
            await engine.replay(new["run_id"], "paid", 1)
        assert len(model.calls) == 2


async def test_disk_change_after_preview_and_concurrent_ui_settings_conflict(tmp_path):
    async with bridge_at(tmp_path) as (store, bridge):
        draft = await edit(bridge)
        (bridge.root / "Характер.md").write_text("SYNC_DIFFERENT", encoding="utf-8")
        with pytest.raises(ConflictError):
            await bridge.decide(draft["id"], "apply", 1, draft["hash"])
        assert (await store.db.settings()).version == 1
    # Separate clean bridge, owner direct UI release wins CAS.
    sub = tmp_path / "second"
    sub.mkdir()
    async with bridge_at(sub) as (store, bridge):
        draft = await edit(bridge)
        await store.db.update_settings(1, Settings(persona="UI_CURRENT"))
        assert (await bridge.details(draft["id"]))["conflict"]
        with pytest.raises(ConflictError):
            await bridge.decide(draft["id"], "apply", 2, draft["hash"])
        assert (await store.prompts_for(2)).sha256 == DEFAULT_PROMPTS.sha256
        await bridge.decide(draft["id"], "reject", 2, draft["hash"])
        await bridge.scan()
        assert "UI_CURRENT" in (bridge.root / "Характер.md").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "invalid", ["json", "type", "duplicate", "unknown", "structure", "missing", "size"]
)
async def test_invalid_and_partial_sync_never_activates(tmp_path, invalid):
    async with bridge_at(tmp_path) as (store, bridge):
        path = bridge.root / "Характер.md"
        row = await bridge.db.one(
            "SELECT prefix,suffix,body FROM configuration_files WHERE path='Характер.md'"
        )
        doc = json.loads(row[2])
        if invalid == "json":
            body = "{"
        elif invalid == "type":
            doc["persona"] = 12
            body = json.dumps(doc)
        elif invalid == "duplicate":
            body = row[2].replace('"tone":', '"tone":"warm", "tone":')
        elif invalid == "unknown":
            doc["unknown"] = "x"
            body = json.dumps(doc)
        elif invalid == "structure":
            body = "BAD_PREFIX"
        elif invalid == "size":
            doc["persona"] = "x" * 4001
            body = json.dumps(doc)
        else:
            path.unlink()
            body = ""
        if invalid != "missing":
            path.write_text(
                body if invalid == "structure" else row[0] + body + row[1], encoding="utf-8"
            )
        await bridge.scan()
        await bridge.scan()
        assert bridge.error and not (await bridge.listing())["drafts"]
        assert (await store.db.settings()).version == 1


async def test_reject_same_version_restores_canonical_notes(tmp_path):
    async with bridge_at(tmp_path) as (_, bridge):
        draft = await edit(bridge)
        await bridge.decide(draft["id"], "reject", 1, draft["hash"])
        await bridge.scan()
        await bridge.scan()
        assert not (await bridge.listing())["drafts"]
        assert "SENTINEL_DRAFT" not in (bridge.root / "Характер.md").read_text(encoding="utf-8")


async def test_transaction_failure_rolls_back_settings_prompts_and_draft(tmp_path, monkeypatch):
    async with bridge_at(tmp_path) as (store, bridge):
        draft = await edit(bridge)

        async def fail(*args):
            raise RuntimeError("FAULT_BEFORE_COMMIT")

        monkeypatch.setattr("zarya.database.effects", fail)
        with pytest.raises(RuntimeError):
            await bridge.decide(draft["id"], "apply", 1, draft["hash"])
        assert (await store.db.settings()).version == 1
        assert (await bridge.details(draft["id"]))["state"] == "pending"
        assert (await store.db.one("SELECT count(*) FROM configuration_releases"))[0] == 1


async def test_existing_sql_release_restarts_without_bootstrap_file(tmp_path):
    async with running(tmp_path) as (app, client):
        await setup(client, tmp_path)
        await app.state.db.update_settings(1, Settings(persona="SQL_CANONICAL"))
    (tmp_path / "prompts.json").write_text("INVALID_BOOTSTRAP_OBSOLETE")
    async with running(tmp_path) as (app, client):
        assert (await app.state.db.settings()).settings.persona == "SQL_CANONICAL"
        assert (await client.get("/api/configuration")).status_code == 401


async def test_configuration_api_csrf_and_rollback_review(tmp_path):
    async with running(tmp_path) as (app, client):
        await setup(client, tmp_path)
        await app.state.db.update_settings(1, Settings(persona="NEW"))
        denied = await client.post(
            "/api/configuration/rollback",
            json={"version": 1, "expected_version": 2},
            headers={"X-CSRF-Token": "wrong"},
        )
        assert denied.status_code == 403
        response = await client.post(
            "/api/configuration/rollback", json={"version": 1, "expected_version": 2}
        )
        detail = (await client.get("/api/configuration/drafts/" + response.json()["id"])).json()
        assert detail["nodes"][0]["path"] == "Характер.md"
        assert (await app.state.db.settings()).version == 2


def test_strict_validation_does_not_echo_private_text():
    doc = document(Settings(), DEFAULT_PROMPTS)
    doc["settings"]["persona"] = 42
    with pytest.raises(ValueError) as exc:
        validate(json.dumps(doc))
    assert "42" not in str(exc.value)


async def test_large_utf8_package_roundtrips_without_ascii_expansion(tmp_path):
    async with store_at(tmp_path) as store:
        fragments = {k: "" for k in DEFAULT_PROMPTS.fragments}
        for k in list(fragments)[:3]:
            fragments[k] = "Ж" * 18000
        raw = json.dumps({"schema_version": 1, "fragments": fragments}, ensure_ascii=False).encode()
        prompts = PromptBundle.parse(raw, set(fragments))
        await store.db.initialize_configuration(prompts)
        assert (await store.prompts_for(1)).sha256 == prompts.sha256


async def test_large_emoji_note_fits_its_envelope(tmp_path):
    async with store_at(tmp_path) as store:
        fragments = {k: "" for k in DEFAULT_PROMPTS.fragments}
        fragments["dialogue.identity"] = "🙂" * 32000
        raw = json.dumps({"schema_version": 1, "fragments": fragments}, ensure_ascii=False).encode()
        prompts = PromptBundle.parse(raw, set(fragments))
        await store.db.initialize_configuration(prompts)
        vault = tmp_path / "vault"
        vault.mkdir()
        config = tmp_path / "obsidian.local.json"
        config.write_text(json.dumps({"enabled": True, "project_path": str(vault)}))
        bridge = ConfigurationBridge(store, config)
        await bridge.scan()
        await bridge.scan()
        assert not bridge.error
        assert (await store.prompts_for(1)).sha256 == prompts.sha256


async def test_photo_download_stays_on_admitted_prompt_release(tmp_path):
    async with store_at(tmp_path) as store:
        await store.db.initialize_configuration(DEFAULT_PROMPTS)
        await private_ready(store)
        model = VisionModel()
        engine = PhotoEngine(store, model, tmp_path)
        await store.ingest("999", "zarya_test", [photo(2)])
        work = await engine.claim("999")
        transport = PhotoTransport()
        transport.release = asyncio.Event()
        task = asyncio.create_task(engine.execute(work, transport))
        await transport.entered.wait()
        fragments = dict(DEFAULT_PROMPTS.fragments)
        fragments["media.photo"] = "NEW_PHOTO_RELEASE"
        prompts = PromptBundle.parse(
            json.dumps({"schema_version": 1, "fragments": fragments}).encode(), set(fragments)
        )
        async with store.db.transaction() as c:
            await store.db._update_settings(c, 1, Settings(), prompts, "obsidian")
        transport.release.set()
        await task
        assert "NEW_PHOTO_RELEASE" not in model.calls[0]["instructions"]
        assert work["settings_version"] == 1


async def test_legacy_null_photo_is_bound_at_bootstrap(tmp_path):
    async with store_at(tmp_path) as store:
        await private_ready(store)
        await store.ingest("999", "zarya_test", [photo(2)])
        async with store.db.transaction() as c:
            await c.execute("UPDATE photo_batches SET settings_version=NULL")
        await store.db.initialize_configuration(DEFAULT_PROMPTS)
        await store.db.update_settings(1, Settings())
        work = await PhotoEngine(store, VisionModel(), tmp_path).claim("999")
        assert work["settings_version"] == 1


async def test_publish_crash_after_sql_intent_recovers(tmp_path, monkeypatch):
    async with bridge_at(tmp_path) as (store, bridge):
        await store.db.update_settings(1, Settings(persona="AFTER_CRASH"))
        original = __import__("zarya.obsidian_fs", fromlist=["write"]).write

        def fail(root, name, data, expected, **kwargs):
            if name == "Характер.md":
                raise OSError("SIMULATED_CRASH")
            return original(root, name, data, expected, **kwargs)

        monkeypatch.setattr("zarya.obsidian_fs.write", fail)
        with pytest.raises(OSError):
            await bridge.scan()
        assert (await store.db.settings()).version == 2
        monkeypatch.setattr("zarya.obsidian_fs.write", original)
        await bridge.scan()
        await bridge.scan()
        assert "AFTER_CRASH" in (bridge.root / "Характер.md").read_text(encoding="utf-8")
        assert not bridge.error and not (await bridge.listing())["drafts"]


async def test_cli_existing_release_no_longer_requires_prompt_file(tmp_path):
    prompt_file = tmp_path / "prompts.json"
    prompt_file.write_text(
        json.dumps({"schema_version": 1, "fragments": dict(DEFAULT_PROMPTS.fragments)})
    )
    config = Config(data_dir=tmp_path, web_dir=tmp_path / "web", prompts_file=prompt_file)
    app = create_app(config)
    async with app.router.lifespan_context(app):
        assert (await app.state.configuration_bridge.current())["version"] == 1
    prompt_file.unlink()
    app = create_app(config)
    async with app.router.lifespan_context(app):
        assert (await app.state.telegram_store.prompts_for(1)).sha256 == DEFAULT_PROMPTS.sha256


async def test_roll_back_enabling_does_not_resurrect_cancelled_photo(tmp_path):
    async with store_at(tmp_path) as store:
        await store.db.initialize_configuration(DEFAULT_PROMPTS)
        await private_ready(store)
        await store.ingest("999", "zarya_test", [photo(2)])
        await store.db.update_settings(1, Settings(photo_enabled=False))
        assert (await store.db.one("SELECT state FROM photo_batches"))[0] == "cancelled"
        bridge = ConfigurationBridge(store, tmp_path / "not-configured.json")
        draft = await bridge.rollback(1, 2)
        detail = await bridge.details(draft)
        await bridge.decide(draft, "apply", 2, detail["hash"])
        assert (await store.db.settings()).settings.photo_enabled
        assert (await store.db.one("SELECT state FROM photo_batches"))[0] == "cancelled"


async def test_foreign_folder_and_marker_changes_are_not_adopted(tmp_path):
    async with bridge_at(tmp_path) as (store, bridge):
        draft = await edit(bridge)
        marker = bridge.root / ".zarya-configuration.json"
        marker.write_text("FOREIGN")
        with pytest.raises(ValueError):
            await bridge.decide(draft["id"], "apply", 1, draft["hash"])
        assert (await store.db.settings()).version == 1


async def test_bootstrap_crlf_prompt_is_not_its_own_draft(tmp_path):
    async with store_at(tmp_path) as store:
        fragments = dict(DEFAULT_PROMPTS.fragments)
        fragments["dialogue.identity"] = "line1\r\nline2"
        prompts = PromptBundle.parse(
            json.dumps({"schema_version": 1, "fragments": fragments}).encode(), set(fragments)
        )
        await store.db.initialize_configuration(prompts)
        vault = tmp_path / "vault"
        vault.mkdir()
        config = tmp_path / "obsidian.local.json"
        config.write_text(json.dumps({"enabled": True, "project_path": str(vault)}))
        bridge = ConfigurationBridge(store, config)
        await bridge.scan()
        await bridge.scan()
        await bridge.scan()
        assert not (await bridge.listing())["drafts"]
        draft = await edit(bridge)
        detail = await bridge.details(draft["id"])
        assert [n["path"] for n in detail["nodes"]] == ["Характер.md"]
        await bridge.decide(draft["id"], "apply", 1, draft["hash"])
        assert (await store.prompts_for(2)).text("dialogue.identity") == "line1\r\nline2"
