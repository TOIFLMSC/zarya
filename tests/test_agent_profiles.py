import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_sync_preserves_private_instructions_and_only_updates_complete_profiles(tmp_path: Path):
    script = Path("scripts/sync-agent-models.py")
    matrix = Path("config/codex-agent-models.toml")
    for name in (script, matrix):
        (tmp_path / name.parent).mkdir()
        shutil.copyfile(ROOT / name, tmp_path / name)
    profiles = tomllib.loads((tmp_path / matrix).read_text(encoding="utf-8"))
    assert set(profiles) == {"primary", "oracle", "librarian", "finder", "reviewer", "designer"}
    codex = tmp_path / ".codex"
    (codex / "agents").mkdir(parents=True)
    original = (
        b'model = "old" # keep comment\r\nmodel_reasoning_effort = "low"\r\n'
        b'developer_instructions = """Preserve this contract.\r\nmodel = "example"\r\n"""\r\n'
    )
    targets = {codex / "config.toml": profiles["primary"]}
    targets.update({codex / "agents" / f"{k}.toml": v for k, v in profiles.items()})
    missing = codex / "agents/designer.toml"
    for path in targets.keys() - {missing}:
        path.write_bytes(original)

    def sync(*args: str) -> int:
        return subprocess.run([sys.executable, tmp_path / script, *args], check=False).returncode

    assert sync() == 2
    assert all(path.read_bytes() == original for path in targets if path.exists())
    missing.write_bytes(original)
    assert sync("--check") == 1
    assert all(path.read_bytes() == original for path in targets)
    assert sync() == 0
    assert sync("--check") == 0
    for path, values in targets.items():
        assert path.read_bytes() == original.replace(
            b'"old"', f'"{values["model"]}"'.encode()
        ).replace(b'"low"', f'"{values["model_reasoning_effort"]}"'.encode())
