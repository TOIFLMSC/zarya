import importlib.util
import json
import sqlite3
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "publication", Path(__file__).parents[1] / "scripts/check-publication.py"
)
publication = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publication)


def test_publication_blocks_runtime_and_unexpected_files():
    for path in (
        "data/export.py",
        "src/zarya/cache.db",
        "docs/private.md",
        "web/src/prompts.json",
        ".env",
        "tests/secret.txt",
        ".github/workflows/unknown.yml",
    ):
        assert publication.findings(path, b"innocent", set())
    assert not publication.findings("src/zarya/prompts.example.json", b"{}", set())
    assert not publication.findings(".github/workflows/ci.yml", b"name: CI", set())
    assert not publication.findings("docs/codegraph.md", b"Public CLI instructions", set())
    assert not publication.findings(
        "docs/memory-retrieval.md", b"Public retrieval instructions", set()
    )
    assert publication.findings("docs/codegraph-stage5.md", b"Private installation notes", set())


def test_publication_detects_credentials_without_echoing_values():
    token = "123456789:" + "A" * 35
    issues = publication.findings("src/zarya/example.py", token.encode(), set())
    assert issues and all(token not in issue for issue in issues)


def test_publication_detects_escaped_private_prompt():
    private = "Частная инструкция конкретной установки. " * 12
    chunks = publication.private_chunks(json.dumps({"fragments": {"test": private}}), "{}")
    encoded = ("PROMPT = " + ascii(private)).encode()
    assert publication.findings("src/zarya/example.py", encoded, chunks)
    neutral = json.dumps({"fragments": {"test": private}})
    assert not publication.private_chunks(neutral, neutral)


def test_publication_inspects_sql_releases_and_drafts_without_bootstrap_file(tmp_path):
    active = "Приватный рабочий промпт только в SQL. " * 12
    proposed = "Отдельный приватный черновик из Obsidian. " * 12
    with sqlite3.connect(tmp_path / "zarya.sqlite3") as c:
        c.execute("CREATE TABLE configuration_releases (prompts_json TEXT)")
        c.execute("CREATE TABLE configuration_drafts (document TEXT)")
        c.execute(
            "INSERT INTO configuration_releases VALUES (?)",
            (json.dumps({"fragments": {"x": active}}),),
        )
        c.execute(
            "INSERT INTO configuration_drafts VALUES (?)",
            (json.dumps({"prompts": {"fragments": {"x": proposed}}}),),
        )
    packages = publication.local_packages(tmp_path, tmp_path / "prompts.json")
    chunks = set().union(*(publication.private_chunks(p, "{}") for p in packages))
    for value in (active, proposed):
        issues = publication.findings("tests/example.py", ("S = " + ascii(value)).encode(), chunks)
        assert issues and all(value not in issue for issue in issues)


def test_publication_does_not_create_missing_database(tmp_path):
    assert publication.local_packages(tmp_path, tmp_path / "prompts.json") == []
    assert not (tmp_path / "zarya.sqlite3").exists()
