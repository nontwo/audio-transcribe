"""Synthetic civil-clock suggestions and local library lifecycle contracts."""
import json
import os
from pathlib import Path
import tempfile
import shutil
import unittest
from unittest.mock import patch
import wave

from audio_transcribe import library, storage


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.settings = {"roots": {"data": str(self.root)}, "storage_approved": True}

    def wav(self, name, seconds=60):
        path = self.root / name
        with wave.open(str(path), "wb") as stream:
            stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            stream.writeframes(b"\0\0" * int(seconds * 16000))
        return str(path)

    def saved_report(self, name="batch-fixture", entries=None, groups=None):
        path = self.root / "exports" / name
        path.mkdir(parents=True)
        report = path / "transcript-report.md"
        report.write_text("# Transcript Report\n\nSynthetic lecture text.\n", encoding="utf-8")
        entries = entries or [{"filename": "audio_240102_090000.wav", "state": "failed"}]
        manifest = {"batch_id": name, "ordered_sources": entries, "created_at": "2024-01-03T00:00:00Z",
                    "selected": len(entries), "completed": 0, "failed": len(entries),
                    "report_sha256": storage.sha256_file(report)}
        if groups is not None:
            manifest["groups"] = groups
        storage.write_json(path / "manifest.json", manifest)
        return path

    def test_strict_filename_calendar_and_unknown_timezone(self):
        result = library.recording_time("audio_240229_235959_32bit_orig.wav")
        self.assertEqual(result["recorded_at"], "2024-02-29T23:59:59")
        self.assertEqual(result["provenance"], "filename")
        self.assertIsNone(result["timezone"])
        self.assertFalse(result["confirmed"])
        for name in ("audio_230229_090000.wav", "audio_240132_090000.wav", "audio_240101_240000.wav", "notes_240102_090000.wav"):
            with self.subTest(name=name):
                self.assertIsNone(library.recording_time(name)["date"])

    def test_explicit_metadata_has_priority_and_invalid_metadata_not_guessed(self):
        result = library.recording_time("audio_240102_090000.wav", "2024-04-05T08:00:00-04:00")
        self.assertEqual(result["date"], "2024-04-05")
        self.assertEqual(result["provenance"], "explicit_metadata")
        self.assertIsNone(library.recording_time("audio_240102_090000.wav", "invalid")["date"])

    def test_measured_end_gap_suggestion_and_overlap_split(self):
        paths = [self.wav("audio_240102_090000.wav", 60),
                 self.wav("audio_240102_090110.wav", 60),
                 self.wav("audio_240102_090130.wav", 60),
                 self.wav("audio_240102_110000.wav", 60)]
        result = library.propose_groups(paths)
        self.assertEqual([g["indices"] for g in result["groups"]], [[0, 1], [2], [3]])
        self.assertTrue(result["requires_confirmation"])
        self.assertIn("10 second", result["groups"][0]["issues"][0])
        self.assertIn("overlap", result["groups"][1]["issues"][0])
        self.assertTrue(all(g["confirmed"] is False for g in result["groups"]))

    def test_unknown_dates_and_mtimes_never_imply_class_or_recording_date(self):
        paths = [self.wav("part1.wav"), self.wav("part2.wav")]
        for path in paths:
            os.utime(path, (1704157200, 1704157200))
        result = library.propose_groups(paths)
        self.assertEqual(len(result["groups"]), 2)
        self.assertTrue(all(r["date"] is None for r in result["recordings"]))

    def test_mixed_timezone_bases_require_separate_confirmation(self):
        paths = [self.wav("audio_240102_090000.wav", 60), self.wav("audio_240102_090100.wav", 60)]
        result = library.propose_groups(paths, {paths[0]: {"recorded_at": "2024-01-02T09:00:00-04:00"}})
        self.assertEqual([g["indices"] for g in result["groups"]], [[0], [1]])
        self.assertIn("timezone", result["groups"][1]["issues"][0])

    def test_different_date_and_unknown_duration_are_not_joined(self):
        paths = [self.wav("audio_240102_235950.wav", 30),
                 self.wav("audio_240103_000020.wav", 30),
                 str(self.root / "audio_240103_001000.mp3"),
                 self.wav("audio_240103_001100.wav", 30)]
        result = library.propose_groups(paths)
        self.assertEqual([g["indices"] for g in result["groups"]], [[0], [1, 2], [3]])
        self.assertIn("unavailable", result["groups"][2]["issues"][0])

    def test_explicit_groups_partition_in_order_and_sanitize_path(self):
        entries = [{"filename": "audio_240102_090000.wav"}, {"filename": "audio_240102_093000.wav"}]
        groups = [{"indices": [0, 1], "title": "../../Course / Part A", "date": "2024-01-02", "confirmed": True}]
        normalized = library.normalize_groups(entries, groups)
        path = library.export_directory(self.root, normalized)
        self.assertTrue(path.is_relative_to(self.root / "exports" / "2024-01-02"))
        self.assertNotIn("..", path.name)
        for indices in ([0], [1, 0], [0, 0, 1], [-1, 0]):
            with self.subTest(indices=indices), self.assertRaises(ValueError):
                library.normalize_groups(entries, [{**groups[0], "indices": indices}])
        with self.assertRaises(ValueError):
            library.normalize_groups(entries, [{**groups[0], "confirmed": False}])

    def test_confirmed_group_clock_prefers_explicit_metadata(self):
        entries = [{"filename": "audio_240102_090000.wav", "recording_time": library.recording_time("unused", "2024-01-02T13:30:00")}]
        groups = library.normalize_groups(entries, [{"indices": [0], "title": "Afternoon class", "date": "2024-01-02", "confirmed": True}])
        self.assertEqual(groups[0]["start_time"], "13:30:00")
        self.assertEqual(library.export_directory(self.root, groups).name, "133000-Afternoon-class")

    def test_list_legacy_report_read_search_and_durable_rename_preserve_files(self):
        path = self.saved_report()
        before = {p.name: p.read_bytes() for p in path.iterdir()}
        with patch.object(library, "_parse", side_effect=AssertionError("Browsing must not inspect audio")):
            result = library.library_request(self.settings, {"action": "list"})
            self.assertEqual(result["dates"][0]["date"], "2024-01-02")
            self.assertEqual(result["reports"][0]["grouping_status"], "unconfirmed")
            self.assertEqual(len(library.list_library(self.settings, "LECTURE")["reports"]), 1)
            self.assertEqual(len(library.list_library(self.settings, "audio_240102")["reports"]), 1)
            self.assertEqual(library.list_library(self.settings, "not present")["reports"], [])
            detail = library.library_request(self.settings, {"action": "read", "report_id": "batch-fixture", "query": "LECTURE"})
            self.assertEqual(detail["matches"], [{"line": 3, "text": "Synthetic lecture text."}])
            self.assertEqual(detail["integrity"], "intact")
            renamed = library.library_request(self.settings, {"action": "rename", "report_id": "batch-fixture", "title": "Morning class"})
            self.assertEqual(renamed["reports"][0]["title"], "Morning class")
        self.assertEqual({p.name: p.read_bytes() for p in path.iterdir()}, before)
        self.assertEqual(storage.read_doc(self.root / "library" / "index.json")["reports"][0]["title"], "Morning class")

    def test_multidate_master_visible_under_each_date_without_duplicate_sources(self):
        entries = [{"filename": "audio_240102_090000.wav", "state": "failed"},
                   {"filename": "audio_240103_090000.wav", "state": "failed"}]
        groups = [{"indices": [i], "title": f"Class {i+1}", "date": f"2024-01-0{i+2}", "start_time": "09:00:00", "confirmed": True} for i in range(2)]
        self.saved_report(entries=entries, groups=groups)
        result = library.list_library(self.settings)
        self.assertEqual(len(result["reports"]), 1)
        self.assertEqual([d["date"] for d in result["dates"]], ["2024-01-03", "2024-01-02"])
        self.assertEqual({d["reports"][0]["id"] for d in result["dates"]}, {"batch-fixture"})

    def test_read_is_managed_id_only_and_owner_edit_is_preserved(self):
        path = self.saved_report()
        report = path / "transcript-report.md"
        report.write_text("Owner edited synthetic fixture.", encoding="utf-8")
        detail = library.read_report(self.settings, "batch-fixture")
        self.assertEqual(detail["integrity"], "modified")
        self.assertEqual(detail["markdown"], "Owner edited synthetic fixture.")
        with self.assertRaises(ValueError):
            library.read_report(self.settings, "../outside")
        with self.assertRaises(ValueError):
            library.library_request(self.settings, {"action": "rename", "report_id": "batch-fixture", "title": "\x00bad"})

    def test_owner_cleared_date_is_not_replaced_by_filename_hint(self):
        self.saved_report(groups=[{"indices": [0], "title": "Clock is incorrect", "date": None,
                                   "start_time": None, "confirmed": True}])
        report = library.list_library(self.settings)["reports"][0]
        self.assertIsNone(report["date"])
        self.assertEqual(report["dates"], [None])

    def test_known_and_unknown_confirmed_dates_both_appear(self):
        entries = [{"filename": "audio_240102_090000.wav", "state": "failed"},
                   {"filename": "audio_240103_090000.wav", "state": "failed"}]
        groups = [{"indices": [0], "title": "Known date", "date": "2024-01-02", "start_time": "09:00:00", "confirmed": True},
                  {"indices": [1], "title": "Uncertain clock", "date": None, "start_time": None, "confirmed": True}]
        self.saved_report(entries=entries, groups=groups)
        snapshot = library.list_library(self.settings)
        self.assertEqual([date["date"] for date in snapshot["dates"]], ["2024-01-02", None])
        self.assertIsNone(snapshot["reports"][0]["date"])
        self.assertIsNone(snapshot["dates"][1]["reports"][0]["start_time"])

    def test_each_date_sorts_by_its_own_group_start_time(self):
        self.saved_report(name="batch-single", groups=[{"indices": [0], "title": "Morning", "date": "2024-01-03", "start_time": "09:00:00", "confirmed": True}])
        entries = [{"filename": "audio_240102_080000.wav", "state": "failed"},
                   {"filename": "audio_240103_140000.wav", "state": "failed"}]
        groups = [{"indices": [0], "title": "First day", "date": "2024-01-02", "start_time": "08:00:00", "confirmed": True},
                  {"indices": [1], "title": "Second day", "date": "2024-01-03", "start_time": "14:00:00", "confirmed": True}]
        self.saved_report(name="batch-master", entries=entries, groups=groups)
        snapshot = library.list_library(self.settings)
        rows = next(date["reports"] for date in snapshot["dates"] if date["date"] == "2024-01-03")
        self.assertEqual([row["id"] for row in rows], ["batch-master", "batch-single"])
        self.assertEqual([row["start_time"] for row in rows], ["14:00:00", "09:00:00"])

    def test_duplicate_identity_and_external_report_symlink_are_not_read(self):
        path = self.saved_report()
        copy = self.root / "exports" / "2024-01-02" / "class" / path.name
        shutil.copytree(path, copy)
        self.assertEqual(library.list_library(self.settings)["reports"], [])
        shutil.rmtree(copy)
        outside = self.root / "not-a-report.txt"
        outside.write_text("Synthetic outside content")
        (path / "transcript-report.md").unlink()
        (path / "transcript-report.md").symlink_to(outside)
        self.assertEqual(library.list_library(self.settings)["reports"], [])


if __name__ == "__main__":
    unittest.main()
