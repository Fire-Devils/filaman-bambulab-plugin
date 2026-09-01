#!/usr/bin/env python3
"""Generate validated GitHub release notes from Conventional Commits.

The plugin manifest remains the authoritative version source. This script finds
the preceding plugin tag, validates that the requested SemVer bump matches the
commits since that tag, and writes grouped Markdown suitable for ``gh release``.
It uses only the Python standard library and the local Git repository.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


CONVENTIONAL_SUBJECT_RE = re.compile(
    r"^(?P<type>[a-z][a-z0-9-]*)(?:\((?P<scope>[^)]+)\))?"
    r"(?P<breaking>!)?: (?P<description>.+)$"
)
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
BREAKING_FOOTER_RE = re.compile(
    r"(?im)^BREAKING(?: |-)?CHANGE:\s*(?P<description>.+)$"
)
VERSION_BUMP_RE = re.compile(
    r"^bump (?:the )?(?:plugin )?version to\b", re.IGNORECASE
)

CATEGORY_ORDER = (
    "Breaking Changes",
    "Features",
    "Fixes",
    "Performance",
    "Documentation",
    "Refactoring",
    "Build & CI",
    "Tests",
    "Maintenance",
    "Reverts",
    "Other Changes",
)
TYPE_CATEGORIES = {
    "feat": "Features",
    "fix": "Fixes",
    "perf": "Performance",
    "docs": "Documentation",
    "refactor": "Refactoring",
    "build": "Build & CI",
    "ci": "Build & CI",
    "test": "Tests",
    "chore": "Maintenance",
    "style": "Maintenance",
    "revert": "Reverts",
}


class ReleaseNotesError(RuntimeError):
    """Raised when commit history cannot produce a safe release."""


@dataclass(frozen=True, order=True)
class Version:
    """Represent a stable three-part semantic version."""

    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, value: str) -> "Version":
        """Parse ``MAJOR.MINOR.PATCH`` or raise a release configuration error."""
        match = SEMVER_RE.fullmatch(value.strip())
        if not match:
            raise ReleaseNotesError(
                f"Unsupported version {value!r}; expected MAJOR.MINOR.PATCH"
            )
        return cls(*(int(part) for part in match.groups()))

    def __str__(self) -> str:
        """Return the canonical semantic-version string."""
        return f"{self.major}.{self.minor}.{self.patch}"


@dataclass(frozen=True)
class GitCommit:
    """Contain the Git metadata needed for release-note generation."""

    sha: str
    parents: tuple[str, ...]
    subject: str
    body: str = ""

    @property
    def is_merge(self) -> bool:
        """Return whether Git records multiple parents or a merge subject."""
        return len(self.parents) > 1 or self.subject.startswith("Merge ")


@dataclass(frozen=True)
class ConventionalCommit:
    """Represent one parsed Conventional Commit."""

    source: GitCommit
    commit_type: str
    scope: str | None
    description: str
    breaking_description: str | None

    @property
    def is_breaking(self) -> bool:
        """Return whether the subject or body declares a breaking change."""
        return self.breaking_description is not None


def _run_git(arguments: Sequence[str], cwd: Path) -> str:
    """Run a read-only Git command and return decoded standard output."""
    result = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ReleaseNotesError(f"git {' '.join(arguments)} failed: {detail}")
    return result.stdout


def parse_conventional_commit(commit: GitCommit) -> ConventionalCommit | None:
    """Parse one direct commit, returning ``None`` for ignored release bumps."""
    match = CONVENTIONAL_SUBJECT_RE.fullmatch(commit.subject.strip())
    if not match:
        raise ReleaseNotesError(
            f"Commit {commit.sha[:8]} is not a Conventional Commit: "
            f"{commit.subject}"
        )

    commit_type = match.group("type")
    description = match.group("description").strip()
    if commit_type == "chore" and VERSION_BUMP_RE.match(description):
        return None

    footer = BREAKING_FOOTER_RE.search(commit.body or "")
    breaking_description = None
    if match.group("breaking"):
        breaking_description = description
    elif footer:
        breaking_description = footer.group("description").strip()

    return ConventionalCommit(
        source=commit,
        commit_type=commit_type,
        scope=match.group("scope"),
        description=description,
        breaking_description=breaking_description,
    )


def parse_git_log(raw_log: str) -> list[GitCommit]:
    """Parse the record/unit-separated format emitted by :func:`load_commits`."""
    commits: list[GitCommit] = []
    for record in raw_log.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        parts = record.split("\x1f", 3)
        if len(parts) != 4:
            raise ReleaseNotesError("Git log returned an unexpected record format")
        sha, parents, subject, body = parts
        commits.append(
            GitCommit(
                sha=sha.strip(),
                parents=tuple(parents.split()),
                subject=subject.strip(),
                body=body.strip(),
            )
        )
    return commits


def load_commits(
    repository: Path,
    previous_tag: str | None,
    target: str,
    paths: Sequence[str],
) -> list[GitCommit]:
    """Read commits in the release range, optionally limited to plugin paths."""
    revision = f"{previous_tag}..{target}" if previous_tag else target
    arguments = [
        "log",
        "--topo-order",
        "--format=%H%x1f%P%x1f%s%x1f%b%x1e",
        revision,
    ]
    if paths:
        arguments.extend(["--", *paths])
    return parse_git_log(_run_git(arguments, repository))


def find_previous_tag(repository: Path, plugin: str, version: Version) -> str | None:
    """Return the highest stable plugin tag whose version precedes ``version``."""
    prefix = f"{plugin}-v"
    candidates: list[tuple[Version, str]] = []
    for tag in _run_git(["tag", "--list", f"{prefix}*"], repository).splitlines():
        value = tag.removeprefix(prefix)
        try:
            parsed = Version.parse(value)
        except ReleaseNotesError:
            continue
        if parsed < version:
            candidates.append((parsed, tag))
    return max(candidates)[1] if candidates else None


def relevant_commits(commits: Iterable[GitCommit]) -> list[ConventionalCommit]:
    """Ignore merges/version bumps and validate every other commit subject."""
    parsed: list[ConventionalCommit] = []
    for commit in commits:
        if commit.is_merge:
            continue
        conventional = parse_conventional_commit(commit)
        if conventional is not None:
            parsed.append(conventional)
    if not parsed:
        raise ReleaseNotesError("No release-note commits remain after filtering")
    return parsed


def required_bump(commits: Iterable[ConventionalCommit]) -> str:
    """Return the highest SemVer impact declared by the supplied commits."""
    commits = list(commits)
    if any(commit.is_breaking for commit in commits):
        return "major"
    if any(commit.commit_type == "feat" for commit in commits):
        return "minor"
    return "patch"


def expected_version(previous: Version, bump: str) -> Version:
    """Calculate the exact next stable version for a Conventional Commit bump."""
    if bump == "major":
        return Version(previous.major + 1, 0, 0)
    if bump == "minor":
        return Version(previous.major, previous.minor + 1, 0)
    return Version(previous.major, previous.minor, previous.patch + 1)


def validate_version(
    previous: Version, current: Version, commits: Iterable[ConventionalCommit]
) -> str:
    """Require the manifest version to match the Conventional Commit impact."""
    bump = required_bump(commits)
    expected = expected_version(previous, bump)
    if current != expected:
        raise ReleaseNotesError(
            f"Version {current} does not match the required {bump} bump from "
            f"{previous}; expected {expected}"
        )
    return bump


def _display_description(value: str) -> str:
    """Capitalize a commit description for a readable Markdown bullet."""
    return value[:1].upper() + value[1:] if value else value


def build_release_notes(
    plugin: str,
    version: Version,
    previous_tag: str | None,
    commits: Sequence[ConventionalCommit],
    repository_url: str,
    description: str = "",
) -> str:
    """Build grouped Markdown release notes with commit and comparison links."""
    categories: dict[str, list[str]] = {}
    base_url = repository_url.rstrip("/")
    for commit in reversed(commits):
        category = (
            "Breaking Changes"
            if commit.is_breaking
            else TYPE_CATEGORIES.get(commit.commit_type, "Other Changes")
        )
        detail = (
            commit.breaking_description
            if commit.is_breaking and commit.breaking_description
            else commit.description
        )
        scope = f"**{commit.scope}:** " if commit.scope else ""
        commit_link = f"[{commit.source.sha[:8]}]({base_url}/commit/{commit.source.sha})"
        categories.setdefault(category, []).append(
            f"- {scope}{_display_description(detail)} ({commit_link})"
        )

    lines = [f"## What's Changed in {plugin} {version}", ""]
    for category in CATEGORY_ORDER:
        entries = categories.get(category)
        if not entries:
            continue
        lines.extend([f"### {category}", "", *entries, ""])

    if description.strip():
        lines.extend(["## About", "", description.strip(), ""])

    current_tag = f"{plugin}-v{version}"
    if previous_tag:
        compare_url = f"{base_url}/compare/{previous_tag}...{current_tag}"
        lines.append(f"**Full Changelog:** [{previous_tag}...{current_tag}]({compare_url})")
    else:
        lines.append(f"**Release tag:** `{current_tag}`")
    return "\n".join(lines).rstrip() + "\n"


def detect_repository_url(repository: Path) -> str:
    """Convert the origin remote into a public HTTPS URL for Markdown links."""
    value = _run_git(["remote", "get-url", "origin"], repository).strip()
    if value.startswith("git@github.com:"):
        value = f"https://github.com/{value.removeprefix('git@github.com:')}"
    return value.removesuffix(".git")


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line interface used locally and by GitHub Actions."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin", required=True, help="Plugin/tag prefix")
    parser.add_argument("--version", required=True, help="Manifest version")
    parser.add_argument("--target", default="HEAD", help="Release target revision")
    parser.add_argument("--previous-tag", help="Override previous tag discovery")
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--repository-url", default="")
    parser.add_argument("--description", default="")
    parser.add_argument("--path", action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Generate notes, returning a nonzero status with a concise error message."""
    args = build_parser().parse_args(argv)
    try:
        version = Version.parse(args.version)
        repository = args.repository.resolve()
        previous_tag = args.previous_tag or find_previous_tag(
            repository, args.plugin, version
        )
        commits = relevant_commits(
            load_commits(repository, previous_tag, args.target, args.path)
        )
        if previous_tag:
            previous_prefix = f"{args.plugin}-v"
            previous_version = Version.parse(
                previous_tag.removeprefix(previous_prefix)
            )
            bump = validate_version(previous_version, version, commits)
            print(
                f"Validated {bump} release: {previous_version} -> {version}",
                file=sys.stderr,
            )
        repository_url = args.repository_url or detect_repository_url(repository)
        notes = build_release_notes(
            args.plugin,
            version,
            previous_tag,
            commits,
            repository_url,
            args.description,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(notes, encoding="utf-8")
    except ReleaseNotesError as exc:
        print(f"Release notes error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
