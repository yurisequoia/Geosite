"""Compare a built GeoSite file with the latest published release."""

import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path


SHA256 = r"[0-9a-fA-F]{64}"


def github_api(endpoint: str, *, accept: str = "application/vnd.github+json",
               allow_not_found: bool = False) -> str | None:
    result = subprocess.run(
        ["gh", "api", "--include", "--header", f"Accept: {accept}", endpoint],
        capture_output=True, encoding="utf-8", timeout=60,
    )
    headers, separator, body = result.stdout.partition("\n\n")
    status = re.match(r"HTTP/\S+ (\d{3})(?: |$)", headers)
    if not separator or status is None:
        raise RuntimeError(f"{endpoint}: {result.stderr.strip() or 'missing HTTP response'}")
    code = int(status[1])
    if code == 404 and allow_not_found:
        return None
    if result.returncode or code != 200:
        raise RuntimeError(f"{endpoint}: HTTP {code}: {result.stderr.strip()}")
    return body


def validate_release(release: object) -> dict:
    if (not isinstance(release, dict)
            or type(release.get("draft")) is not bool
            or type(release.get("prerelease")) is not bool):
        raise ValueError("invalid release metadata")
    return release


def latest_release(repository: str) -> dict | None:
    endpoint = f"repos/{repository}/releases"
    body = github_api(f"{endpoint}/latest", allow_not_found=True)
    if body is not None:
        release = validate_release(json.loads(body))
        if release["draft"] or release["prerelease"]:
            raise ValueError("Latest is not a published stable release")
        return release

    # A 404 alone can also mean an inaccessible repository. Confirm that the
    # release list is accessible and contains no published stable releases.
    page = 1
    while True:
        releases = json.loads(github_api(f"{endpoint}?per_page=100&page={page}"))
        if not isinstance(releases, list):
            raise ValueError("invalid release list")
        for item in releases:
            release = validate_release(item)
            if not release["draft"] and not release["prerelease"]:
                raise ValueError("Latest returned 404, but a published stable release exists")
        if len(releases) < 100:
            return None
        page += 1


def release_digest(repository: str, release: dict) -> str:
    assets = release.get("assets")
    if (not isinstance(assets, list)
            or any(not isinstance(asset, dict) or not isinstance(asset.get("name"), str)
                   for asset in assets)):
        raise ValueError("invalid release assets")
    data_assets = [asset for asset in assets if asset["name"] == "geosite.dat"]
    if len(data_assets) != 1:
        raise ValueError("Latest must contain exactly one geosite.dat asset")
    digest = data_assets[0].get("digest")
    if digest is not None:
        if not isinstance(digest, str) or not re.fullmatch(f"sha256:{SHA256}", digest):
            raise ValueError("invalid geosite.dat asset digest")
        return digest.split(":", 1)[1].lower()

    checksum_assets = [asset for asset in assets if asset["name"] == "geosite.dat.sha256"]
    if len(checksum_assets) != 1:
        raise ValueError("Latest has no digest or unique geosite.dat.sha256 asset")
    asset_id = checksum_assets[0].get("id")
    if type(asset_id) is not int or asset_id <= 0:
        raise ValueError("invalid checksum asset ID")
    checksum = github_api(
        f"repos/{repository}/releases/assets/{asset_id}",
        accept="application/octet-stream",
    )
    match = re.fullmatch(rf"({SHA256}) [ *]geosite\.dat\n?", checksum)
    if match is None:
        raise ValueError("invalid geosite.dat.sha256 contents")
    return match[1].lower()


def check_release(repository: str, data: Path, output: Path, summary: Path) -> None:
    with data.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    release = latest_release(repository)
    previous_digest = None
    if release is None:
        baseline = "No previous stable release"
        result = "First release; publishing."
    else:
        tag = release.get("tag_name")
        if not isinstance(tag, str) or not tag or "\n" in tag or "\r" in tag:
            raise ValueError("invalid Latest release tag")
        baseline = f"`{tag}`"
        previous_digest = release_digest(repository, release)
        result = ("Unchanged; release skipped." if digest == previous_digest
                  else "Changed; publishing.")

    report = (
        "## GeoSite release comparison\n\n"
        f"**{result}**\n\n"
        f"- Baseline: {baseline}\n"
        f"- Current SHA-256: `{digest}`\n"
    )
    if previous_digest is not None:
        report += f"- Previous SHA-256: `{previous_digest}`\n"
    print(report)
    with summary.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(report)
    # Do not emit a publishing decision until all requests and validation pass.
    with output.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(f"publish={str(digest != previous_digest).lower()}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, help="GitHub owner/repository")
    parser.add_argument("--file", type=Path, required=True, help="built geosite.dat")
    args = parser.parse_args()
    try:
        check_release(
            args.repository, args.file,
            Path(os.environ["GITHUB_OUTPUT"]), Path(os.environ["GITHUB_STEP_SUMMARY"]),
        )
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
