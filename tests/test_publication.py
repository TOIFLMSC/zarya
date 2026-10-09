import importlib.util
import json
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
    ):
        assert publication.findings(path, b"innocent", set())
    assert not publication.findings("src/zarya/prompts.example.json", b"{}", set())


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
