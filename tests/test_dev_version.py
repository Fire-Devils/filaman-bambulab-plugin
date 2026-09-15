"""Verify development cycles and minimal manifest edits."""

from pathlib import Path
import tempfile
import unittest

from scripts.dev_version import next_version, update_manifest


class DevelopmentVersionTests(unittest.TestCase):
    """Dev iterations must not advance the intended stable version."""

    def test_cycle_and_finalization(self):
        self.assertEqual(next_version("2.8.0"), "2.8.1-dev.1")
        self.assertEqual(next_version("2.8.1-dev.1"), "2.8.1-dev.2")
        self.assertEqual(next_version("2.8.1-dev.9"), "2.8.1-dev.10")
        self.assertEqual(next_version("2.8.1-dev.10", finalize=True), "2.8.1")
        self.assertEqual(next_version("2.8.0", "minor"), "2.9.0-dev.1")
        self.assertEqual(next_version("2.8.0", "major"), "3.0.0-dev.1")

    def test_rejects_ambiguous_transitions(self):
        for version, options in (("2.8.0", {"finalize": True}),
                                 ("2.8.1-dev.1", {"bump": "patch"}),
                                 ("2.8.1-dev.0", {}), ("invalid", {})):
            with self.subTest(version=version, options=options):
                with self.assertRaises(ValueError):
                    next_version(version, **options)

    def test_preserves_other_manifest_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plugin.json"
            source = '{\n  "version": "2.8.0", "name": "Bambu Lab"\n}\n'
            path.write_text(source)
            update_manifest(path)
            self.assertEqual(path.read_text(), source.replace('2.8.0', '2.8.1-dev.1'))
