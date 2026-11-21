"""Copy this version of the kit into an existing project without overwriting its CI."""

import argparse
import shutil
from pathlib import Path

SOURCE = Path(__file__).resolve().parent
FILES = (
    "VERSION",
    "README.md",
    "install.py",
    "controller.py",
    "backup.py",
    "bundle.py",
    "pull_release.py",
    "project.example.json",
    "demo/app.py",
    "demo/adapter.py",
    "tests/feed.py",
    "tests/systemd_drill.py",
    "workflow.yml",
)


def scaffold(project, workflow):
    project = project.resolve(strict=True)
    if not project.is_dir():
        raise ValueError("project must be an existing directory")
    dest = project / "deploy/pull-cd"
    ci = project / ".github/workflows/ci-pull.yml"
    for path in (dest, ci) if workflow else (dest,):
        if path.exists() or path.is_symlink():
            raise FileExistsError(path)
        if not path.parent.resolve().is_relative_to(project):
            raise ValueError("destination is outside the project")
    for filename in FILES:
        if not (SOURCE / filename).is_file():
            raise FileNotFoundError(filename)
    dest.mkdir(parents=True)
    for filename in FILES:
        target = dest / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SOURCE / filename, target)
    if workflow:
        ci.parent.mkdir(parents=True, exist_ok=True)
        with ci.open("xb") as output:
            output.write((SOURCE / "workflow.yml").read_bytes())
    print(f"Installed pull-cd files in {dest}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    parser.add_argument("--example-workflow", action="store_true")
    args = parser.parse_args()
    scaffold(args.project, args.example_workflow)
