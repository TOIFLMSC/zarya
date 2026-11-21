"""Download only the successful push build for the current branch tip (stdlib)."""

import hashlib
import json
import os
import re
import shutil
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_BYTES = 256 * 1024 * 1024


class Redirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme != "https":
            raise ValueError("expected HTTPS artifact redirect")
        redirect = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirect and urlsplit(newurl).netloc != urlsplit(req.full_url).netloc:
            redirect.remove_header("Authorization")
        return redirect


class GitHub:
    def __init__(self, repository, branch, workflow, prefix, token):
        self.base = f"https://api.github.com/repos/{repository}"
        self.repository = repository
        self.branch = branch
        self.workflow = workflow
        self.prefix = prefix
        self.token = token

    def open(self, path):
        req = Request(self.base + path, headers={"Accept": "application/vnd.github+json"})
        if self.token:
            req.add_unredirected_header("Authorization", "Bearer " + self.token)
        return build_opener(Redirect()).open(req, timeout=30)

    def json(self, path):
        with self.open(path) as response:
            return json.load(response)

    def tip(self):
        sha = self.json(f"/branches/{self.branch}")["commit"]["sha"]
        if not re.fullmatch(r"[a-f0-9]{40}", sha):
            raise ValueError("invalid branch tip")
        return sha

    def artifact(self, sha):
        query = urlencode(dict(branch=self.branch, event="push", head_sha=sha, per_page=1))
        runs = self.json(f"/actions/workflows/{self.workflow}/runs?{query}")["workflow_runs"]
        if not runs:
            return None
        run = runs[0]
        if (run["status"], run["conclusion"]) != ("completed", "success"):
            return None
        expected_path = ".github/workflows/" + self.workflow
        if run["path"] not in {
            expected_path,
            f"{expected_path}@{self.branch}",
            f"{expected_path}@refs/heads/{self.branch}",
        } or (
            run["head_sha"],
            run["head_branch"],
            run["event"],
            run["head_repository"]["full_name"],
        ) != (sha, self.branch, "push", self.repository):
            raise ValueError("workflow provenance mismatch")
        name = self.prefix + "-" + sha
        results = self.json(
            f"/actions/runs/{int(run['id'])}/artifacts?" + urlencode({"name": name})
        )
        matches = [a for a in results["artifacts"] if a["name"] == name and not a["expired"]]
        if len(matches) != 1 or not re.fullmatch(r"sha256:[a-f0-9]{64}", matches[0]["digest"]):
            raise ValueError("missing, ambiguous or expired release artifact")
        return matches[0]

    def download(self, artifact, folder):
        archive = folder / "artifact.zip"
        with self.open(f"/actions/artifacts/{int(artifact['id'])}/zip") as response:
            with archive.open("xb") as output:
                while chunk := response.read(1024 * 1024):
                    if output.tell() + len(chunk) > MAX_BYTES:
                        raise ValueError("artifact too large")
                    output.write(chunk)
        if "sha256:" + digest(archive) != artifact["digest"]:
            raise ValueError("GitHub artifact digest mismatch")
        return archive


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def unpack(archive, target, sha):
    target.mkdir()
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        if len(entries) > 4096 or sum(e.file_size for e in entries) > MAX_BYTES:
            raise ValueError("oversized release")
        for entry in entries:
            name = entry.filename.rstrip("/")
            path = PurePosixPath(name)
            if not name or path.is_absolute() or ".." in path.parts or "\\" in name:
                raise ValueError("unsafe archive path")
            dest = target / path
            if entry.is_dir():
                dest.mkdir(exist_ok=True, parents=True)
            else:
                dest.parent.mkdir(exist_ok=True, parents=True)
                with bundle.open(entry) as src, dest.open("xb") as dst:
                    shutil.copyfileobj(src, dst)
    sums = (target / "SHA256SUMS").read_text().splitlines()
    actual = {
        f"{digest(p)}  ./{p.relative_to(target).as_posix()}"
        for p in target.rglob("*")
        if p.is_file() and p != target / "SHA256SUMS"
    }
    if sorted(sums) != sorted(actual) or (target / "REVISION").read_text().strip() != sha:
        raise ValueError("release manifest or revision mismatch")


def configured():
    credential = Path(os.environ["CREDENTIALS_DIRECTORY"]) / "github-token"
    return GitHub(
        os.environ["CD_REPOSITORY"],
        os.environ["CD_BRANCH"],
        os.environ["CD_WORKFLOW"],
        os.environ["CD_ARTIFACT_PREFIX"],
        credential.read_text().strip(),
    )
