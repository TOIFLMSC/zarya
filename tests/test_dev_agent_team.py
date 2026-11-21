import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

KIT = Path(__file__).resolve().parents[1] / "templates/dev-agent-team"


def run(project, *options, initial_encoding="utf-8"):
    return subprocess.run(
        [sys.executable, KIT / "install.py", project, *options],
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
        env=dict(os.environ, PYTHONIOENCODING=initial_encoding),
    )


def snapshot(project):
    return {
        p.relative_to(project).as_posix(): p.read_bytes()
        for p in project.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(project).parts
    }


@pytest.mark.parametrize("profile", ["backend", "ui"])
def test_install_reinstall_preserves_project_configuration(tmp_path, profile):
    project = tmp_path / profile
    project.mkdir()
    subprocess.run(["git", "init", "--quiet", project], check=True)
    (project / ".codex/agents").mkdir(parents=True)
    if profile == "backend":
        (project / "pyproject.toml").write_text('[project]\nname = "sample-server"\n')
        (project / "server.py").write_text("PORT = 8080\n")
        original = b"\xef\xbb\xbf# Server instructions\r\n\r\nRun pytest.\r\n"
    else:
        (project / "package.json").write_text(
            '{"name":"sample-ui","scripts":{"test":"node --test"}}'
        )
        (project / "index.html").write_text("<title>Sample</title><button>Save</button>")
        original = b"# Frontend instructions\n\nUse the existing button styles.\n"
    (project / "AGENTS.md").write_bytes(original)
    original_config = (
        b'# local choices\nmodel="existing-model"\n[mcp_servers.local]\ncommand="test"\n'
    )
    (project / ".codex/config.toml").write_bytes(original_config)
    (project / ".codex/agents/local.toml").write_bytes(b'name = "local_role"\n')
    before = snapshot(project)
    preview = run(project, "--profile", profile, initial_encoding="cp1252")
    assert preview.returncode == 0 and "+<!-- dev-agent-team:begin -->" in preview.stdout
    assert snapshot(project) == before
    check = run(project, "--profile", profile, "--check")
    assert check.returncode == 1 and snapshot(project) == before
    install = run(project, "--profile", profile, "--apply")
    assert install.returncode == 0, install.stderr
    files = snapshot(project)
    assert files["AGENTS.md"].startswith(original)
    for name in before.keys() - {"AGENTS.md"}:
        assert files[name] == before[name]
    assert (".codex/agents/dev-team-designer.toml" in files) == (profile == "ui")
    assert (b"dev_team_designer" in files["AGENTS.md"]) == (profile == "ui")
    assert ".codex/agents/dev-team-primary.toml" not in files
    reviewer = project / ".codex/agents/dev-team-reviewer.toml"
    assert tomllib.loads(reviewer.read_text("utf-8"))["sandbox_mode"] == "read-only"

    settings = project / ".codex/dev-team.toml"
    extra = '\n[roles.reviewer]\nmodel_reasoning_effort = "medium"\ncallsign = "Ревьюер"\n'
    with settings.open("ab") as stream:
        stream.write(extra.encode())
    agents = project / "AGENTS.md"
    local_tail = b"\n## Local checks\nRun the smoke test before release.\n"
    with agents.open("ab") as stream:
        stream.write(local_tail)
    saved = settings.read_bytes()
    alternate = "ui" if profile == "backend" else "backend"
    assert run(project, "--profile", alternate, "--apply").returncode == 0
    assert settings.read_bytes() == saved
    assert agents.read_bytes().startswith(original) and agents.read_bytes().endswith(local_tail)
    assert agents.read_bytes().count(b"<!-- dev-agent-team:begin -->") == 1
    assert (b"dev_team_designer" in agents.read_bytes()) == (profile == "ui")
    assert tomllib.loads(reviewer.read_text("utf-8"))["model_reasoning_effort"] == "medium"
    settled = snapshot(project)
    assert run(project, "--apply").returncode == 0
    assert run(project, "--check").returncode == 0
    assert snapshot(project) == settled


@pytest.mark.parametrize("source", ["role", "block", "missing markers", "duplicate"])
def test_manual_edits_and_duplicate_agent_names_are_preserved(tmp_path, source):
    assert run(tmp_path, "--apply").returncode == 0
    if source == "role":
        path = tmp_path / ".codex/agents/dev-team-reviewer.toml"
        path.write_bytes(path.read_bytes() + b"# locally edited\n")
    elif source == "block":
        path = tmp_path / "AGENTS.md"
        path.write_bytes(path.read_bytes().replace(b"small edits", b"approved edits"))
    elif source == "missing markers":
        path = tmp_path / "AGENTS.md"
        path.write_bytes(
            path.read_bytes()
            .replace(b"<!-- dev-agent-team:begin -->\n", b"")
            .replace(b"<!-- dev-agent-team:end -->\n", b"")
        )
    else:
        path = tmp_path / ".codex/agents/other.toml"
        path.write_bytes(b'name = "dev_team_reviewer"\n')
    before = snapshot(tmp_path)
    for option in ("--apply", "--check"):
        result = run(tmp_path, option)
        assert result.returncode == 1 and "PRESERVE" in result.stdout
        assert snapshot(tmp_path) == before


def test_checkout_crlf_is_clean_and_preserved_when_updating_roles(tmp_path):
    assert run(tmp_path, "--apply").returncode == 0
    for name, content in snapshot(tmp_path).items():
        (tmp_path / name).write_bytes(content.replace(b"\n", b"\r\n"))
    before = snapshot(tmp_path)
    assert run(tmp_path, "--check").returncode == 0
    assert run(tmp_path, "--apply").returncode == 0
    assert snapshot(tmp_path) == before
    settings = tmp_path / ".codex/dev-team.toml"
    with settings.open("ab") as stream:
        stream.write(b'\r\n[roles.reviewer]\r\nmodel_reasoning_effort = "medium"\r\n')
    assert run(tmp_path, "--apply").returncode == 0
    reviewer = (tmp_path / ".codex/agents/dev-team-reviewer.toml").read_bytes()
    assert b'model_reasoning_effort = "medium"\r\n' in reviewer
    assert run(tmp_path, "--check").returncode == 0


def test_skill_is_self_contained_and_profile_can_disable_designer(tmp_path):
    home = tmp_path / "skills"
    home.mkdir()
    subprocess.run([sys.executable, KIT / "install-skill.py", home], check=True)
    skill = home / "dev-team-onboarding"
    assert (skill / "SKILL.md").is_file()
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(
        [
            sys.executable,
            skill / "assets/dev-agent-team/install.py",
            project,
            "--profile",
            "ui",
            "--apply",
        ],
        check=True,
    )
    settings = project / ".codex/dev-team.toml"
    settings.write_text('profile = "ui"\n[roles.designer]\nenabled = false\n')
    assert run(project, "--apply").returncode == 0
    assert not (project / ".codex/agents/dev-team-designer.toml").exists()
    assert b"dev_team_designer" not in (project / "AGENTS.md").read_bytes()
