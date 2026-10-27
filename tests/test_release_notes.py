"""Unit tests for Conventional Commit release-note generation."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.generate_release_notes import (
    ConventionalCommit,
    GitCommit,
    ReleaseNotesError,
    Version,
    build_release_notes,
    check_next_version,
    expected_version,
    parse_conventional_commit,
    relevant_commits,
    required_bump,
    validate_version,
)


def git_commit(
    subject: str,
    *,
    sha: str = "1234567890abcdef",
    body: str = "",
    parents: tuple[str, ...] = ("parent",),
) -> GitCommit:
    """Build compact Git history fixtures."""
    return GitCommit(sha=sha, parents=parents, subject=subject, body=body)


class ConventionalCommitTests(unittest.TestCase):
    def test_parses_scope_and_breaking_footer(self):
        parsed = parse_conventional_commit(
            git_commit(
                "feat(catalog): add another provider",
                body="BREAKING CHANGE: provider metadata format changed",
            )
        )

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.commit_type, "feat")
        self.assertEqual(parsed.scope, "catalog")
        self.assertTrue(parsed.is_breaking)
        self.assertEqual(
            parsed.breaking_description,
            "provider metadata format changed",
        )

    def test_ignores_merge_and_version_bump_commits(self):
        commits = relevant_commits(
            [
                git_commit(
                    "Merge pull request #1 from example/main",
                    parents=("one", "two"),
                ),
                git_commit("chore: bump plugin version to 2.8.0"),
                git_commit("fix: reuse an existing spool"),
            ]
        )

        self.assertEqual([item.description for item in commits], ["reuse an existing spool"])

    def test_ignores_manually_worded_version_commits(self):
        commits = relevant_commits(
            [
                git_commit("fix(plugin): update version to 2.8.2 in plugin manifest"),
                git_commit("fix(plugin): revert version to 2.8.1 in plugin manifest"),
                git_commit("fix(plugin): update plugin version to 2.7.4 in manifest"),
                git_commit("chore: set the version to 2.9.0"),
                git_commit("fix: reuse an existing spool"),
            ]
        )

        self.assertEqual([item.description for item in commits], ["reuse an existing spool"])

    def test_keeps_features_that_only_mention_a_version(self):
        commits = relevant_commits(
            [git_commit("feat(plugin): update version to the payload")]
        )

        self.assertEqual(len(commits), 1)
        self.assertEqual(commits[0].commit_type, "feat")

    def test_rejects_nonconventional_direct_commit(self):
        with self.assertRaisesRegex(ReleaseNotesError, "not a Conventional Commit"):
            relevant_commits([git_commit("Update driver")])


class SemanticVersionTests(unittest.TestCase):
    @staticmethod
    def parsed(subject: str, body: str = "") -> ConventionalCommit:
        result = parse_conventional_commit(git_commit(subject, body=body))
        assert result is not None
        return result

    def test_feature_requires_exact_minor_bump(self):
        bump = validate_version(
            Version.parse("2.7.3"),
            Version.parse("2.8.0"),
            [self.parsed("feat: add weight option")],
        )

        self.assertEqual(bump, "minor")

    def test_fix_requires_exact_patch_bump(self):
        bump = validate_version(
            Version.parse("2.8.0"),
            Version.parse("2.8.1"),
            [self.parsed("fix: match an existing spool")],
        )

        self.assertEqual(bump, "patch")

    def test_breaking_change_requires_exact_major_bump(self):
        bump = validate_version(
            Version.parse("2.8.1"),
            Version.parse("3.0.0"),
            [self.parsed("feat!: replace metadata schema")],
        )

        self.assertEqual(bump, "major")

    def test_derives_minor_bump_for_a_feature_after_a_bump_commit(self):
        commits = relevant_commits(
            [
                git_commit("chore: bump plugin version to 2.8.2"),
                git_commit("feat: hand each AMS unit's info flags to FilaMan"),
            ]
        )
        bump = required_bump(commits)

        self.assertEqual(bump, "minor")
        self.assertEqual(
            str(expected_version(Version.parse("2.8.1"), bump)), "2.9.0"
        )

    def test_rejects_incorrect_manifest_version(self):
        with self.assertRaisesRegex(ReleaseNotesError, "expected 2.9.0"):
            validate_version(
                Version.parse("2.8.0"),
                Version.parse("2.8.1"),
                [self.parsed("feat: add another option")],
            )


class CheckNextVersionTests(unittest.TestCase):
    @staticmethod
    def git(repository: Path, *arguments: str) -> None:
        """Run one Git command inside the throwaway fixture repository."""
        subprocess.run(
            ["git", *arguments], cwd=repository, check=True, capture_output=True
        )

    def test_reports_bump_previous_tag_and_expected_version(self):
        with tempfile.TemporaryDirectory() as folder:
            repository = Path(folder)
            self.git(repository, "init", "--quiet")
            self.git(repository, "config", "user.email", "test@example.com")
            self.git(repository, "config", "user.name", "Release Test")
            plugin = repository / "bambulab"
            plugin.mkdir()
            (plugin / "plugin.json").write_text('{"version": "2.8.1"}\n')
            self.git(repository, "add", "--all")
            self.git(repository, "commit", "--quiet", "-m", "fix: keep the manifest")
            self.git(repository, "tag", "bambulab-v2.8.1")
            (plugin / "slots.py").write_text("FLAGS = 1\n")
            self.git(repository, "add", "--all")
            self.git(
                repository, "commit", "--quiet", "-m", "feat: report the AMS info flags"
            )

            bump, previous_tag, expected = check_next_version(
                repository, "bambulab", "HEAD", ["bambulab"]
            )

        self.assertEqual(bump, "minor")
        self.assertEqual(previous_tag, "bambulab-v2.8.1")
        self.assertEqual(str(expected), "2.9.0")


class MarkdownTests(unittest.TestCase):
    @staticmethod
    def parsed(subject: str, sha: str) -> ConventionalCommit:
        result = parse_conventional_commit(git_commit(subject, sha=sha))
        assert result is not None
        return result

    def test_groups_user_facing_notes_and_adds_compare_link(self):
        notes = build_release_notes(
            "bambulab",
            Version.parse("2.8.0"),
            "bambulab-v2.7.3",
            [
                self.parsed("fix: reuse an existing spool", "a" * 40),
                self.parsed(
                    "feat(catalog): use article numbers for images",
                    "b" * 40,
                ),
            ],
            "https://github.com/example/plugin",
            "Bambu Lab integration for FilaMan.",
        )

        self.assertIn("### Features", notes)
        self.assertIn("**catalog:** Use article numbers for images", notes)
        self.assertIn("### Fixes", notes)
        self.assertIn("Reuse an existing spool", notes)
        self.assertIn("## About", notes)
        self.assertIn(
            "compare/bambulab-v2.7.3...bambulab-v2.8.0",
            notes,
        )


if __name__ == "__main__":
    unittest.main()
