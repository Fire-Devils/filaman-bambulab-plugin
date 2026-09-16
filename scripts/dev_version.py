"""Advance development versions in plugin.json; finalize only on explicit request."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re


def next_version(current: str, bump: str | None = None, finalize: bool = False) -> str:
    """Start a SemVer development cycle, increment its counter, or finalize it."""
    match = re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-dev\.([1-9]\d*))?", current)
    if not match:
        raise ValueError(f"Unsupported version: {current}")
    major, minor, patch = map(int, match.groups()[:3])
    counter = match[4]
    if finalize:
        if not counter or bump:
            raise ValueError("Finalize requires a dev version and no --bump")
        return f"{major}.{minor}.{patch}"
    if counter:
        if bump:
            raise ValueError("Development cycle already started; omit --bump")
        return f"{major}.{minor}.{patch}-dev.{int(counter) + 1}"
    if bump == "major":
        major, minor, patch = major + 1, 0, 0
    elif bump == "minor":
        minor, patch = minor + 1, 0
    elif bump in (None, "patch"):
        patch += 1
    else:
        raise ValueError("Choose patch, minor or major")
    return f"{major}.{minor}.{patch}-dev.1"


def update_manifest(path: Path, bump: str | None = None, finalize: bool = False) -> str:
    """Change only the version string, preserving manifest formatting."""
    source = path.read_text(encoding="utf-8")
    current = json.loads(source)["version"]
    version = next_version(current, bump, finalize)
    updated, count = re.subn(r'("version"\s*:\s*)' + re.escape(json.dumps(current)),
                            lambda m: m[1] + json.dumps(version), source)
    if count != 1:
        raise ValueError("Expected exactly one matching version field")
    path.write_text(updated, encoding="utf-8")
    return version


def main() -> None:
    """Update the manifest without committing, tagging, or publishing."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("bambulab/plugin.json"))
    parser.add_argument("--bump", choices=("patch", "minor", "major"))
    parser.add_argument("--finalize", action="store_true")
    args = parser.parse_args()
    try:
        print(update_manifest(args.manifest, args.bump, args.finalize))
    except ValueError as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
