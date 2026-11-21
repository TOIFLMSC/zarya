import json
import subprocess
import sys

import pytest

from zarya import codegraph as cg
from zarya import codegraph_export as exporter
from zarya.codegraph_export import FOLDER, MARKER, export_vault, name_for


def put(root, path, text):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def repository(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    put(root, "src/zarya/helpers.py", "def select_episodes():\n    return 1\n")
    put(
        root,
        "src/zarya/memory.py",
        "from zarya.helpers import select_episodes as choose\n"
        "class MemoryEngine:\n    def context(self):\n        return choose()\n",
    )
    put(
        root,
        "tests/test_memory.py",
        "from zarya.memory import MemoryEngine\ndef test_context():\n    return MemoryEngine()\n",
    )
    put(root, "web/src/api.ts", "export function api() {}\n")
    put(
        root,
        "web/src/MemoryPanel.tsx",
        'import { api } from "./api";\nexport function MemoryPanel() { return api(); }\n',
    )
    put(root, "src/zarya/migrations/001_initial.sql", "CREATE TABLE memory_facts(id INT);\n")
    put(root, "docs/architecture.md", "Code is in src/zarya/memory.py.\n")
    return root


def nodes(doc):
    return {n["id"]: n for n in doc["graph"]["nodes"]}


def test_static_alias_calls_import_impact_and_test_candidates(tmp_path):
    root = repository(tmp_path)
    doc = cg.snapshot(root)
    assert doc["complete"]
    names = nodes(doc)
    call = next(
        e
        for e in doc["graph"]["edges"]
        if e["kind"] == "calls" and names[e["target"]]["name"] == "select_episodes"
    )
    assert names[call["source"]]["name"] == "MemoryEngine.context"
    assert call["path"] == "src/zarya/memory.py" and call["line"] == 4
    assert call["file_hash"] == doc["manifest"]["files"][call["path"]]
    result = cg.impact(doc, "select_episodes", 3, 30)
    assert result["candidate_tests"] == ["tests/test_memory.py"]
    test = next(r for r in result["dependents"] if r["path"] == "tests/test_memory.py")
    assert test["distance"] == 2
    assert len(test["reasons"]) == 2
    assert cg.show(doc, "src/zarya/memory.py", 30)["node"]["kind"] == "file"
    assert cg.select(doc, "MemoryEngine.context")[0]["line"] == 3


def test_uncommitted_change_add_rename_delete_and_syntax_error_are_fresh(tmp_path):
    root = repository(tmp_path)
    first = cg.snapshot(root)
    old = root / "src/zarya/helpers.py"
    old.write_text("def different():\n    return 2\n", encoding="utf-8")
    second = cg.snapshot(root)
    assert first["snapshot_id"] != second["snapshot_id"]
    assert not cg.select(second, "select_episodes")
    added = put(root, "src/zarya/new.py", "def new_symbol(): pass\n")
    assert cg.select(cg.snapshot(root), "new_symbol")
    added.rename(root / "src/zarya/renamed.py")
    renamed = cg.snapshot(root)
    assert cg.select(renamed, "new_symbol")[0]["path"] == "src/zarya/renamed.py"
    assert "src/zarya/new.py" not in renamed["manifest"]["files"]
    old.write_text("def PRIVATE_ERROR_CANARY(:", encoding="utf-8")
    broken = cg.snapshot(root)
    assert not broken["complete"] and not cg.select(broken, "different")
    assert "PRIVATE_ERROR_CANARY" not in json.dumps(broken)
    old.unlink()
    assert "src/zarya/helpers.py" not in cg.snapshot(root)["manifest"]["files"]


@pytest.mark.parametrize(
    "body",
    [
        "from zarya.helpers import select_episodes as choose\ndef f(choose): return choose()\n",
        "from zarya.helpers import select_episodes as choose\nchoose = lambda: 2\nchoose()\n",
        "if True:\n    from zarya.helpers import select_episodes as choose\nchoose()\n",
        "from zarya.helpers import select_episodes as choose\nfor choose in []: pass\nchoose()\n",
        "from zarya.helpers import select_episodes as choose\n"
        "try: pass\nexcept Exception as choose: pass\nchoose()\n",
        "from zarya.helpers import select_episodes as choose\n"
        "match 1:\n    case choose: pass\nchoose()\n",
        "from zarya.helpers import *\nselect_episodes()\n",
        "def f(): pass\ndef f(): pass\nf()\n",
        "from zarya.helpers import select_episodes as choose\ndef choose(): pass\nchoose()\n",
        "from zarya.helpers import select_episodes as choose\nclass choose: pass\nchoose()\n",
        "from zarya.helpers import select_episodes as choose\n"
        "if True:\n    def choose(): pass\nchoose()\n",
        "from zarya.helpers import select_episodes as choose\n"
        "[(choose := 2) for x in []]\nchoose()\n",
        "class A:\n    def finish(self): pass\nclass B:\n    def finish(self): pass\n"
        "def f(obj): obj.finish()\n",
    ],
)
def test_shadowing_conditional_ambiguous_and_dynamic_calls_are_not_exact(tmp_path, body):
    root = repository(tmp_path)
    put(root, "src/zarya/consumer.py", body)
    doc = cg.snapshot(root)
    assert not [
        e
        for e in doc["graph"]["edges"]
        if e["kind"] == "calls" and e["path"] == "src/zarya/consumer.py"
    ]
    assert any(r["path"] == "src/zarya/consumer.py" for r in doc["graph"]["unresolved"])


def test_self_method_is_explicit_candidate_and_external_reassigned_target_unresolved(tmp_path):
    root = repository(tmp_path)
    put(
        root,
        "src/zarya/consumer.py",
        "class A:\n    def finish(self): pass\n    def run(self): self.finish()\n",
    )
    doc = cg.snapshot(root)
    assert any(
        e["kind"] == "calls_candidate" and e["confidence"] == "declared_method"
        for e in doc["graph"]["edges"]
    )
    put(root, "src/zarya/helpers.py", "def select_episodes(): pass\nselect_episodes = object()\n")
    doc = cg.snapshot(root)
    assert not [
        e
        for e in doc["graph"]["edges"]
        if e["kind"] == "calls" and e["path"] == "src/zarya/memory.py"
    ]


def test_no_private_folder_body_docstring_comment_or_constant_is_retained(tmp_path, monkeypatch):
    root = repository(tmp_path)
    secret = "PRIVATE_CONTENT_CANARY_NOT_FOR_GRAPH"
    for path in (
        "data/private.py",
        "data/prompts.json",
        ".codex/agents/private.py",
        "docs/product-brief.md",
        "docs/private.md",
        "tests/data/private.py",
    ):
        put(root, path, secret)
    put(
        root,
        "src/zarya/prompts.py",
        f'"""{secret}"""\nVALUE = "{secret}"\n# {secret}\ndef safe(): return VALUE\n',
    )
    put(root, "docs/privacy.md", secret + "\n")
    put(root, "docs/memory-retrieval.md", secret + "\n")
    read = cg.fs.read
    opened = []

    def recording(root, name, **kwargs):
        opened.append(name)
        return read(root, name, **kwargs)

    monkeypatch.setattr(cg.fs, "read", recording)
    doc = cg.snapshot(root)
    assert secret not in json.dumps(doc)
    assert all(cg.allowed(name) for name in opened)
    assert "docs/memory-retrieval.md" in opened
    assert "data/private.py" not in opened and "tests/data/private.py" not in opened
    output = tmp_path / FOLDER
    export_vault(root, output, doc)
    assert secret not in "".join(p.read_text(encoding="utf-8") for p in output.rglob("*.md"))
    assert secret not in (output / "graph.json").read_text(encoding="utf-8")


def test_frontend_hints_ignore_comments_strings_and_sql_literals(tmp_path):
    root = repository(tmp_path)
    put(
        root,
        "web/src/Fake.ts",
        '// import {api} from "./api";\nconst s = `import {api} from "./api";`;\n',
    )
    put(
        root,
        "src/zarya/migrations/002_change.sql",
        "-- CREATE TABLE FAKE_TABLE(id INT);\nALTER TABLE memory_facts ADD extra TEXT;\n"
        "INSERT INTO memory_facts VALUES ('CREATE TABLE SECRET_TABLE');\n",
    )
    doc = cg.snapshot(root)
    assert not [e for e in doc["graph"]["edges"] if e["path"] == "web/src/Fake.ts"]
    assert any(
        e["kind"] == "import_hint" and e["confidence"] == "hint" for e in doc["graph"]["edges"]
    )
    assert any(e["kind"] == "alters_table" for e in doc["graph"]["edges"])
    assert not cg.select(doc, "FAKE_TABLE") and not cg.select(doc, "SECRET_TABLE")


def test_bounded_results_and_import_impact_do_not_walk_contains_or_doc_links(tmp_path):
    root = repository(tmp_path)
    doc = cg.snapshot(root)
    result = cg.impact(doc, "select_episodes", 1, 1)
    assert result["depth_limited"] and result["broad_validation_recommended"]
    assert not any(r["path"].startswith("docs/") for r in result["dependents"])
    assert cg.show(doc, "file:src/zarya/memory.py", 1)["truncated"]
    assert "Related tests not established" in cg.impact(doc, "api.ts", 3, 30)["note"]


def test_concurrent_workspace_change_retries_or_fails_without_old_snapshot(tmp_path, monkeypatch):
    root = repository(tmp_path)
    original = cg.sources
    calls = 0

    def changing(path):
        nonlocal calls
        calls += 1
        put(root, "src/zarya/new.py", f"def version_{calls}(): pass\n")
        return original(path)

    monkeypatch.setattr(cg, "sources", changing)
    with pytest.raises(cg.GraphError, match="workspace_keeps_changing"):
        cg.snapshot(root)


def test_readonly_cli_has_no_runtime_data_or_cache_and_build_is_atomic(tmp_path):
    root = repository(tmp_path)
    command = [sys.executable, "-m", "zarya.codegraph", "--root", str(root)]
    result = subprocess.run(
        [*command, "--read-only", "search", "MemoryEngine"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    assert json.loads(result.stdout)["matches"]
    assert not (root / "data").exists()
    result = subprocess.run(
        [*command, "build"], capture_output=True, text=True, encoding="utf-8", check=True
    )
    assert (
        json.loads((root / cg.CACHE).read_text(encoding="utf-8"))["snapshot_id"]
        == json.loads(result.stdout)["snapshot_id"]
    )
    assert not list((root / "data/codegraph").glob("*.tmp"))
    assert not (root / "data/zarya.sqlite3").exists()


def test_export_updates_only_owned_files_and_preserves_foreign_or_edited_notes(tmp_path):
    root = repository(tmp_path)
    output = tmp_path / FOLDER
    first = cg.snapshot(root)
    export_vault(root, output, first)
    put(output, "my-note.md", "owner note")
    removed = root / "src/zarya/helpers.py"
    removed.unlink()
    export_vault(root, output, cg.snapshot(root))
    assert not (output / name_for("src/zarya/helpers.py")).exists()
    assert (output / "my-note.md").read_text() == "owner note"
    edited = output / name_for("src/zarya/memory.py")
    edited.write_text("owner editing", encoding="utf-8")
    marker = (output / MARKER).read_bytes()
    with pytest.raises(cg.GraphError, match="export_note_changed"):
        export_vault(root, output, cg.snapshot(root))
    assert edited.read_text() == "owner editing" and (output / MARKER).read_bytes() == marker
    foreign = tmp_path / "foreign" / FOLDER
    foreign.mkdir(parents=True)
    with pytest.raises(cg.GraphError, match="foreign_folder"):
        export_vault(root, foreign, first)


def test_export_forbids_source_overlap_foreign_marker_and_linked_paths(tmp_path):
    root = repository(tmp_path)
    doc = cg.snapshot(root)
    with pytest.raises(cg.GraphError):
        export_vault(root, root / FOLDER, doc)
    out = tmp_path / FOLDER
    export_vault(root, out, doc)
    marker = json.loads((out / MARKER).read_text())
    marker["files"]["../private.md"] = "0" * 64
    (out / MARKER).write_text(json.dumps(marker))
    with pytest.raises(cg.GraphError, match="marker_invalid"):
        export_vault(root, out, doc)
    target = tmp_path / "outside.py"
    target.write_text("def SHOULD_NOT_APPEAR(): pass")
    link = root / "src/zarya/linked.py"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("OS account cannot create symlinks")
    with pytest.raises(cg.GraphError, match="linked"):
        cg.snapshot(root)


def test_size_limit_never_returns_previous_graph(tmp_path, monkeypatch):
    root = repository(tmp_path)
    monkeypatch.setattr(cg, "MAX_FILE", 8)
    with pytest.raises(cg.GraphError, match="source_size_limit"):
        cg.snapshot(root)


def test_imported_definition_conflict_is_not_exact(tmp_path):
    root = repository(tmp_path)
    put(
        root,
        "src/zarya/helpers.py",
        "from zarya.memory import MemoryEngine as select_episodes\ndef select_episodes(): pass\n",
    )
    doc = cg.snapshot(root)
    assert not [
        e
        for e in doc["graph"]["edges"]
        if e["kind"] == "calls" and e["path"] == "src/zarya/memory.py"
    ]


def test_export_recovers_two_interruptions_after_marker(tmp_path, monkeypatch):
    root = repository(tmp_path)
    output = tmp_path / FOLDER
    export_vault(root, output, cg.snapshot(root))
    put(root, "src/zarya/helpers.py", "def changed(): pass\n")
    desired = cg.snapshot(root)
    write = cg.fs.write

    def interrupted(root, name, data, expected, **kwargs):
        result = write(root, name, data, expected, **kwargs)
        if name == MARKER and json.loads(data)["phase"] == "writing":
            raise RuntimeError("simulated interruption")
        return result

    monkeypatch.setattr(cg.fs, "write", interrupted)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="simulated interruption"):
            export_vault(root, output, desired)
    monkeypatch.setattr(cg.fs, "write", write)
    export_vault(root, output, desired)
    marker = json.loads((output / MARKER).read_text(encoding="utf-8"))
    assert marker["phase"] == "ready" and not marker["previous"]
    assert marker["snapshot_id"] == desired["snapshot_id"]
    assert "changed" in (output / name_for("src/zarya/helpers.py")).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "body",
    [
        "class A:\n    def finish(): pass\nA = object()\n",
        "from zarya.memory import MemoryEngine as A\nclass A:\n    def finish(): pass\n",
        "if True:\n    class A:\n        def finish(): pass\n",
        "class A:\n    class B:\n        def finish(): pass\n    B = object()\n",
    ],
)
def test_imported_nested_owner_shadowing_never_binds_exactly(tmp_path, body):
    root = repository(tmp_path)
    put(root, "src/zarya/helpers.py", body)
    target = "A.B.finish" if "class B" in body else "A.finish"
    put(root, "src/zarya/consumer.py", f"from zarya.helpers import A\n{target}()\n")
    doc = cg.snapshot(root)
    assert not [
        e
        for e in doc["graph"]["edges"]
        if e["kind"] == "calls" and e["path"] == "src/zarya/consumer.py"
    ]


def test_bulk_note_rename_recovers_after_partial_write_and_second_interruption(
    tmp_path, monkeypatch
):
    root = repository(tmp_path)
    for i in range(300):
        put(root, f"src/zarya/module_{i}.py", "def function(): pass\n")
    doc = cg.snapshot(root)
    output = tmp_path / FOLDER

    def legacy(path):
        return "Модули/" + cg.fs.digest(path.encode())[:24] + ".md"

    with monkeypatch.context() as scoped:
        scoped.setattr(exporter, "name_for", legacy)
        export_vault(root, output, doc)

    def before_cleanup(*args):
        raise RuntimeError("before cleanup")

    with monkeypatch.context() as scoped:
        scoped.setattr(exporter, "remove_owned", before_cleanup)
        with pytest.raises(RuntimeError, match="before cleanup"):
            export_vault(root, output, doc)
    write = cg.fs.write

    def interrupted(root, name, data, expected, **kwargs):
        result = write(root, name, data, expected, **kwargs)
        if name == MARKER and json.loads(data)["phase"] == "writing":
            raise RuntimeError("after marker")
        return result

    with monkeypatch.context() as scoped:
        scoped.setattr(cg.fs, "write", interrupted)
        with pytest.raises(RuntimeError, match="after marker"):
            export_vault(root, output, doc)
    assert len(json.loads((output / MARKER).read_text())["previous"]) > 514
    export_vault(root, output, doc)
    marker = json.loads((output / MARKER).read_text())
    assert marker["phase"] == "ready" and not marker["previous"]
    assert (output / name_for("src/zarya/module_1.py")).exists()
    assert not (output / legacy("src/zarya/module_1.py")).exists()
