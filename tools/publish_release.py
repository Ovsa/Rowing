"""Verify prepared APK assets, then atomically publish them as a GitHub Release.

Default/--verify is offline. --publish uses only the job-scoped GITHUB_TOKEN.
Existing published assets are checked, never deleted or overwritten.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]


def verify():
    folder = ROOT / "release"
    value = json.loads((folder / "update.json").read_text(encoding="utf-8"))
    repository = os.environ.get("GITHUB_REPOSITORY", "Ovsa/Rowing")
    if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid repository")
    version = value["versionName"]
    if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}", version):
        raise ValueError("Invalid version name")
    if value.get("schemaVersion") != 1 or value.get("packageName") != "app.row360.android":
        raise ValueError("Unexpected manifest schema or package")
    for key, maximum in (("versionCode", 2147483647), ("minSdk", 1000), ("sizeBytes", 1073741824)):
        if type(value.get(key)) is not int or not 1 <= value[key] <= maximum:
            raise ValueError("Invalid " + key)
    tag = "v" + version
    apk = folder / ("Row360-" + version + ".apk")
    expected_url = f"https://github.com/{repository}/releases/download/{tag}/{apk.name}"
    if value.get("apkUrl") != expected_url:
        raise ValueError("APK URL must match this repository, release tag and filename")
    if not isinstance(value.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", value["sha256"]):
        raise ValueError("Invalid SHA-256")
    assets = [apk, folder / "update.json", folder / "release-notes.md"]
    if set(path.name for path in folder.iterdir()) != set(path.name for path in assets):
        raise ValueError("Release directory must contain exactly the APK, update.json and release-notes.md")
    for asset in assets:
        if not asset.is_file() or asset.is_symlink():
            raise ValueError("Expected a regular release asset: " + asset.name)
    if apk.stat().st_size != value["sizeBytes"] or digest(apk) != value["sha256"]:
        raise ValueError("APK checksum or size does not match update.json")
    notes = assets[-1].read_text(encoding="utf-8").strip()
    if value.get("notes") != notes or not notes:
        raise ValueError("Manifest notes must match release-notes.md")
    print(f"Verified {tag}, versionCode={value['versionCode']}, SHA-256={value['sha256']}")
    return repository, tag, notes, assets


def digest(path):
    with path.open("rb") as source:
        result = hashlib.sha256()
        for block in iter(lambda: source.read(1024 * 1024), b""):
            result.update(block)
        return result.hexdigest()


class GitHub:
    def __init__(self, token):
        if not token:
            raise ValueError("--publish requires a job-scoped GITHUB_TOKEN")
        self.token = token

    def request(self, method, url, value=None, binary=None, content_type=None):
        headers = {"Authorization": "Bearer " + self.token, "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2026-03-10", "User-Agent": "Row360-Release"}
        data = None
        if value is not None:
            data = json.dumps(value, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if binary is not None:
            data = binary
            headers["Content-Type"] = content_type or "application/octet-stream"
        try:
            with urlopen(Request(url, data=data, headers=headers, method=method), timeout=90) as response:
                return json.load(response)
        except HTTPError as error:
            if error.code == 404 and method == "GET":
                return None
            raise RuntimeError(f"GitHub {method} failed: HTTP {error.code}") from None


def publish(repository, tag, notes, assets):
    commit = os.environ.get("GITHUB_SHA", "")
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("Publication requires the workflow's exact GITHUB_SHA")
    api = GitHub(os.environ.get("GITHUB_TOKEN", ""))
    base = f"https://api.github.com/repos/{repository}/releases"
    release = api.request("GET", base + "/tags/" + quote(tag, safe=""))
    if release is None:
        release = api.request("POST", base, {"tag_name": tag, "target_commitish": commit,
            "name": "Гребля 360 · " + tag[1:], "body": notes, "draft": True, "prerelease": False})
    uploaded = {asset["name"]: asset for asset in release.get("assets", [])}
    for path in assets:
        existing = uploaded.get(path.name)
        if existing is not None:
            if existing.get("size") != path.stat().st_size or existing.get("digest") != "sha256:" + digest(path):
                raise ValueError("Existing asset differs; publish a new version: " + path.name)
            continue
        if not release["draft"]:
            raise ValueError("Published release is missing an asset; refusing to change it")
        upload = f"https://uploads.github.com/repos/{repository}/releases/{release['id']}/assets?name=" + quote(path.name, safe="")
        content_type = "application/vnd.android.package-archive" if path.suffix == ".apk" else "application/json" if path.suffix == ".json" else "text/markdown"
        result = api.request("POST", upload, binary=path.read_bytes(), content_type=content_type)
        if result.get("size") != path.stat().st_size or result.get("digest") != "sha256:" + digest(path):
            raise ValueError("Uploaded asset checksum mismatch: " + path.name)
    if release["draft"]:
        release = api.request("PATCH", base + "/" + str(release["id"]),
                              {"draft": False, "prerelease": False, "make_latest": "true"})
    print("Published and verified: " + release["html_url"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--verify", action="store_true")
    mode.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    info = verify()
    if args.publish:
        publish(*info)
