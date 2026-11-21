"""Install the onboarding skill together with the versioned team template."""

import argparse
import shutil
import tempfile
from pathlib import Path

KIT = Path(__file__).resolve().parent
NAME = "dev-team-onboarding"
FILES = ("VERSION", "README.md", "team.toml", "install.py")


def install(skills_dir):
    root = skills_dir.resolve(strict=True)
    target = root / NAME
    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    with tempfile.TemporaryDirectory(dir=root) as temporary:
        packaged = Path(temporary) / NAME
        assets = packaged / "assets/dev-agent-team"
        assets.mkdir(parents=True)
        shutil.copyfile(KIT / f"skill/{NAME}/SKILL.md", packaged / "SKILL.md")
        for filename in FILES:
            shutil.copyfile(KIT / filename, assets / filename)
        packaged.rename(target)
    print(target)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("skills_dir", type=Path)
    install(parser.parse_args().skills_dir)
