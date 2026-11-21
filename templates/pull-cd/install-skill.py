"""Install the packaged onboarding skill with a self-contained copy of this kit."""

import argparse
import shutil
import tempfile
from pathlib import Path

from scaffold import FILES, SOURCE

NAME = "pull-cd-onboarding"


def install(skills_dir):
    skills_dir = skills_dir.resolve(strict=True)
    target = skills_dir / NAME
    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    with tempfile.TemporaryDirectory(dir=skills_dir) as temp:
        output = Path(temp) / NAME
        assets = output / "assets/pull-cd"
        assets.mkdir(parents=True)
        shutil.copyfile(SOURCE / f"skill/{NAME}/SKILL.md", output / "SKILL.md")
        for filename in (*FILES, "scaffold.py"):
            dest = assets / filename
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(SOURCE / filename, dest)
        output.rename(target)
    print(target)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("skills_dir", type=Path)
    args = parser.parse_args()
    install(args.skills_dir)
