from __future__ import annotations

import importlib.util
import os
import runpy
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


@unittest.skipUnless(importlib.util.find_spec("build"), "release build dependency required")
class SourceDistributionTests(unittest.TestCase):
    def test_failed_atomic_publish_never_leaves_raw_owner_metadata(self) -> None:
        from setuptools import Distribution

        root = Path(__file__).resolve().parents[1]
        with patch("setuptools.setup"):
            namespace = runpy.run_path(str(root / "setup.py"))
        command = namespace["PublicSourceDistribution"](Distribution())
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input"
            source.mkdir()
            (source / "example.txt").write_text("synthetic release fixture")
            destination = Path(directory) / "release"
            with patch("os.replace", side_effect=OSError("synthetic atomic publish failure")):
                with self.assertRaises(OSError):
                    command.make_archive(str(destination), "gztar", root_dir=directory, base_dir="input")
            self.assertFalse(destination.with_suffix(".tar.gz").exists())

    def test_failed_source_build_does_not_leave_an_unfiltered_archive(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-m", "build", "--sdist", "--no-isolation", "--outdir", directory, str(root)],
                env={**os.environ, "SOURCE_DATE_EPOCH": "invalid"},
                capture_output=True, text=True, timeout=60,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(list(Path(directory).glob("*.tar.gz")), [])

    def test_source_archive_does_not_disclose_local_owner_metadata(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-m", "build", "--sdist", "--no-isolation", "--outdir", directory, str(root)],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])
            archives = list(Path(directory).glob("*.tar.gz"))
            self.assertEqual(len(archives), 1)
            with tarfile.open(archives[0]) as archive:
                members = archive.getmembers()
                self.assertTrue(members)
                self.assertEqual({(m.uid, m.gid, m.uname, m.gname) for m in members}, {(0, 0, "", "")})
                self.assertTrue(all(not (m.issym() or m.islnk()) for m in members))
                self.assertTrue(any(m.name.endswith("/skills/telegram-search/SKILL.md") for m in members))
