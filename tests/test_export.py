"""Report lifecycle fixtures. Decoder/dialog mocks are not live inference/UI evidence."""
import copy
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave
import struct

from audio_transcribe import cli, config, engine, export, library, storage


class ExportTests(unittest.TestCase):
    def test_explicit_failed_retry_preserves_old_attempt_and_success_cache(self):
        good=self.source("good.wav",10); retry=self.source("retry.wav",20)
        baseline=self.report([good])
        session,record,_=storage.import_sources(self.data,[retry])
        def invalid(*args,**kwargs):
            metadata=self.decode(*args,**kwargs)
            native=Path(args[4])/'native.json'
            storage.write_json(native,{'transcription':[{'offsets':{'from':100,'to':3000},'text':'Preserved failed fixture.'}]},overwrite=True)
            metadata['native_sha256']=storage.sha256_file(native)
            return metadata
        with patch.object(engine,'decode',side_effect=invalid):
            provisional=engine.run_session(self.settings,session,self.resolved)
        self.assertEqual(provisional['state'],'review_required')
        old={p:p.read_bytes() for p in session.glob('transcript/*/**/*') if p.is_file()}
        before=self.calls
        recovered=export.build_report(self.settings,[good,retry],self.resolved,retry_failed=True)
        self.assertEqual(recovered['completed'],2)
        self.assertEqual(self.calls,before+1)
        self.assertEqual(recovered['reused_transcripts'],1)
        for p,value in old.items():self.assertEqual(p.read_bytes(),value)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.data = self.root / "data"
        self.settings = {"roots": {"data": str(self.data), "app": str(self.root / "app"),
                                   "code": str(self.root / "code")}, "storage_approved": True}
        self.resolved = config.resolve_config(self.data)
        self.runtime = {"runtime": {"cli": "/fixture/whisper", "sha256": "runtime", "release": "test"}}
        self.model = {"name": "large-v3", "sha256": "model", "path": "/fixture/model", "precision": "f16"}
        self.calls = 0
        self.raw = [" Same repeated text <untrusted> *not formatted*.", " Same repeated text <untrusted> *not formatted*."]
        for module in (engine, export):
            for name, value in (("load_runtime", self.runtime), ("model_identity", self.model)):
                p = patch.object(module, name, return_value=value)
                p.start(); self.addCleanup(p.stop)
        p = patch.object(engine, "decode", side_effect=self.decode)
        p.start(); self.addCleanup(p.stop)

    def source(self, name, amplitude):
        path = self.root / "files 空间" / name
        path.parent.mkdir(exist_ok=True)
        with wave.open(str(path), "wb") as f:
            f.setparams((1, 2, 16000, 16000, "NONE", "not compressed"))
            f.writeframes(struct.pack("<h", amplitude) * 16000)
        return path

    def decode(self, settings, runtime, model, wav, output, asr, glossary, **kwargs):
        self.calls += 1
        output.mkdir(parents=True, exist_ok=True)
        storage.write_json(output / "native.json", {"transcription": [
            {"offsets": {"from": 100, "to": 400}, "text": self.raw[0]},
            {"offsets": {"from": 400, "to": 900}, "text": self.raw[1]}]})
        return {"input_frames": 16000, "input_duration_seconds": 1.0, "elapsed_seconds": .01,
                "native_sha256": storage.sha256_file(output / "native.json")}

    def report(self, paths, resolved=None):
        return export.build_report(self.settings, paths, resolved or self.resolved)

    def test_progress_is_indexed_and_does_not_expose_transcript(self):
        paths = [self.source("a.wav", 10), self.source("b.wav", 20)]
        events = []
        def decode(*args, progress=None, **kwargs):
            progress({"stage": "transcribing", "percent": 45})
            return self.decode(*args)
        with patch.object(engine, "decode", side_effect=decode):
            first = export.build_report(self.settings, paths, self.resolved, events=events.append)
        updates = [e for e in events if e["type"] == "progress" and e["stage"] == "transcribing"]
        self.assertEqual([e["index"] for e in updates], [0, 1])
        self.assertTrue(all(e["percent"] == 45 and e["total"] == 2 for e in updates))
        self.assertNotIn(self.raw[0].strip(), str(events))
        events.clear()
        with patch.object(engine, "decode", side_effect=AssertionError("Progress must not invalidate ASR cache")):
            second = export.build_report(self.settings, paths, self.resolved, events=events.append)
        self.assertEqual(first["report"], second["report"])
        self.assertEqual(second["reused_transcripts"], 2)
        self.assertFalse(any(e["type"] == "progress" for e in events))

    def test_natural_order_and_confirmed_manual_order(self):
        a, b = self.source("part2 空间.wav", 10), self.source("part10 空间.wav", 20)
        self.assertEqual(export.natural_order([b, a]), [a, b])
        result = self.report([b, a])
        doc = storage.read_doc(Path(result["report"]).with_name("manifest.json"))
        self.assertEqual([e["filename"] for e in doc["ordered_sources"]], [b.name, a.name])
        text = Path(result["report"]).read_text()
        self.assertLess(text.index("## 1. " + b.name), text.index("## 2. " + a.name))
        self.assertEqual(text.count("[00:00:00.100 – 00:00:00.400]"), 2)
        self.assertEqual(text.count(export.markdown_text(self.raw[0])), 4)
        self.assertEqual(doc["completed"], 2)
        self.assertEqual(len(list((self.data / "sessions").glob("*/session.yaml"))), 2)
        self.assertEqual(self.calls, 2)

    def test_confirmed_classes_preserve_one_complete_dated_batch_report(self):
        paths = [self.source("audio_240102_090000.wav", 10), self.source("audio_240103_110000.wav", 20)]
        groups = [{"indices": [i], "title": title, "date": date, "confirmed": True}
                  for i, title, date in [(0, "Morning class", "2024-01-02"), (1, "Next class", "2024-01-03")]]
        result = export.build_report(self.settings, paths, self.resolved, groups=groups)
        report = Path(result["report"])
        self.assertTrue(report.is_relative_to(self.data / "exports" / "multiple-dates"))
        text = report.read_text()
        self.assertLess(text.index("# Morning class"), text.index("# Next class"))
        for index, path in enumerate(paths, 1):
            self.assertEqual(text.count(f"## {index}. {export.markdown_text(path.name)}"), 1)
        snapshot = library.list_library(self.settings)
        self.assertEqual(len(snapshot["reports"]), 1)
        self.assertEqual(len(snapshot["dates"]), 2)
        detail = library.read_report(self.settings, result["batch_id"])
        self.assertEqual(len(detail["segments"]), 4)
        self.assertTrue(all(Path(s["audio_path"]).is_relative_to(self.data / "sessions") for s in detail["sources"]))
        before = self.calls
        repeated = export.build_report(self.settings, paths, self.resolved, groups=groups)
        self.assertEqual(repeated["report"], result["report"])
        self.assertEqual(before, self.calls)

    def test_current_quality_gate_reassesses_completed_cache(self):
        path = self.source("audio_240102_090000.wav", 10)
        baseline = self.report([path])
        gate = {"version": "test-stricter-rule", "status": "review_required", "accuracy_verified": False,
                "timestamp_valid": True, "reasons": ["repeated_phrase_loop"], "findings": [], "sources": []}
        with patch.object(export, "assess_quality", return_value=gate), patch.object(export, "run_session", side_effect=AssertionError("Only reassessment is required")):
            result = self.report([path])
        self.assertEqual(result["review_required"], 1)
        self.assertEqual(result["completed"], 0)
        self.assertEqual(result["failed"], 0)
        self.assertNotEqual(baseline["report"], result["report"])
        self.assertIn("REVIEW REQUIRED", Path(result["report"]).read_text())
        self.assertIn(self.raw[0].strip().replace("<", "\\<").replace(">", "\\>").replace("*", "\\*"), Path(result["report"]).read_text())
        before = self.calls
        with patch.object(export, "assess_quality", return_value=gate):
            retry = export.build_report(self.settings, [path], self.resolved, retry_failed=True)
        self.assertEqual(self.calls, before + 1)
        self.assertEqual(retry["review_required"], 1)

    def test_final_confirmed_group_clock_uses_managed_recorded_at(self):
        path = self.source("audio_240102_090000.wav", 10)
        session_path, session, _ = storage.import_sources(self.data, [path])
        session["recorded_at"] = "2024-01-02T13:30:00"
        storage.write_yaml(session_path / "session.yaml", session, overwrite=True)
        groups = [{"indices": [0], "title": "Afternoon class", "date": "2024-01-02", "confirmed": True}]
        result = export.build_report(self.settings, [path], self.resolved, groups=groups)
        self.assertEqual(result["groups"][0]["start_time"], "13:30:00")
        self.assertIn("133000-Afternoon-class", result["report"])

    def test_provisional_negative_timestamp_remains_visible_without_clamping(self):
        path = self.source("audio_240102_090000.wav", 10)
        def decode(*args, **kwargs):
            metadata = self.decode(*args, **kwargs)
            native = Path(args[4]) / "native.json"
            storage.write_json(native, {"transcription": [{"offsets": {"from": -100, "to": 900},
                                                          "text": "Synthetic negative time remains visible."}]}, overwrite=True)
            metadata["native_sha256"] = storage.sha256_file(native)
            return metadata
        with patch.object(engine, "decode", side_effect=decode):
            result = self.report([path])
        text = Path(result["report"]).read_text()
        self.assertEqual(result["review_required"], 1)
        self.assertIn("[-00:00:00.100 – 00:00:00.900]", text)
        self.assertIn("Synthetic negative time remains visible.", text)

    def test_no_context_glossary_limitation_is_visible(self):
        path = self.source("audio_240102_090000.wav", 10)
        resolved = copy.deepcopy(self.resolved)
        resolved["glossary"]["terms"] = ["synthetic classroom vocabulary"]
        resolved["glossary"]["sha256"] = engine.digest(resolved["glossary"]["terms"])
        result = self.report([path], resolved)
        self.assertEqual(result["review_required"], 1)
        text = Path(result["report"]).read_text()
        self.assertIn("glossary", text.lower())
        self.assertNotIn("synthetic classroom vocabulary", text)
        entry = storage.read_doc(Path(result["report"]).with_name("manifest.json"))["ordered_sources"][0]
        self.assertIn("glossary_context_disabled", entry["transcript"]["quality"]["reasons"])

    def test_reuses_results_and_equivalent_export_without_inference(self):
        paths = [self.source("a.wav", 10), self.source("b.wav", 20)]
        first = self.report(paths)
        before = {p: p.read_bytes() for p in self.data.glob("sessions/*/transcript/*/*") if p.is_file()}
        with patch.object(export, "run_session", side_effect=AssertionError("Cached ASR must not run")):
            second = self.report(paths)
        self.assertTrue(second["reused_report"])
        self.assertEqual(first["report"], second["report"])
        self.assertEqual(second["reused_transcripts"], 2)
        for p, value in before.items(): self.assertEqual(p.read_bytes(), value)

    def test_trashed_report_is_not_reused_but_audio_and_asr_are_preserved(self):
        paths = [self.source("audio_240102_090000.wav", 10)]
        first = self.report(paths)
        before = {p: p.read_bytes() for p in self.data.rglob("*") if p.is_file()}
        library.library_request(self.settings, {"action": "trash", "report_id": first["batch_id"]})
        with patch.object(export, "run_session", side_effect=AssertionError("Trashing a report must not discard cached ASR")):
            second = self.report(paths)
        self.assertNotEqual(first["report"], second["report"])
        self.assertFalse(second["reused_report"])
        self.assertEqual(second["reused_transcripts"], 1)
        self.assertEqual(self.calls, 1)
        self.assertEqual([r["id"] for r in library.list_library(self.settings)["reports"]], [second["batch_id"]])
        self.assertEqual([r["id"] for r in library.list_library(self.settings, view="trash")["reports"]], [first["batch_id"]])
        for path, content in before.items():
            if path.parent.name != "library":
                self.assertEqual(path.read_bytes(), content)
        library.library_request(self.settings, {"action": "restore", "report_id": first["batch_id"]})
        self.assertEqual(library.list_library(self.settings)["active_count"], 2)

    def test_renamed_report_folder_cannot_abort_export_or_be_reused(self):
        paths = [self.source("audio_240102_090000.wav", 10)]
        for renamed in ("renamed report", "batch-renamed"):
            first = self.report(paths)
            folder = Path(first["report"]).parent
            saved = {path.name: path.read_bytes() for path in folder.iterdir() if path.is_file()}
            moved = folder.with_name(renamed)
            folder.rename(moved)
            with patch.object(export, "run_session", side_effect=AssertionError("Intact ASR should still be reused")):
                replacement = self.report(paths)
            self.assertFalse(replacement["reused_report"])
            self.assertNotEqual(Path(replacement["report"]).parent, moved)
            self.assertEqual({path.name: path.read_bytes() for path in moved.iterdir() if path.is_file()}, saved)
        self.assertEqual(self.calls, 1)

    def test_existing_multi_source_session_is_not_reclassified(self):
        a, b = self.source("a.wav", 10), self.source("b.wav", 20)
        session, _, _ = storage.import_sources(self.data, [a, b], order_confirmed=True)
        run = engine.run_session(self.settings, session, self.resolved)
        before = (session / "session.yaml").read_bytes()
        with patch.object(export, "run_session", side_effect=AssertionError("No extra ASR")):
            report = self.report([b, a])
        entries = storage.read_doc(Path(report["report"]).with_name("manifest.json"))["ordered_sources"]
        self.assertEqual({e["transcript"]["run_id"] for e in entries}, {run["run_id"]})
        self.assertEqual({e["transcript"]["session_id"] for e in entries}, {session.name})
        self.assertEqual(before, (session / "session.yaml").read_bytes())

    def test_failed_input_is_retained_and_remaining_valid_input_completes(self):
        a, b = self.source("first.wav", 10), self.source("last.wav", 20)
        missing = self.root / "missing 空间.wav"
        result = self.report([a, missing, b])
        self.assertEqual((result["state"], result["completed"], result["failed"]), ("partial", 2, 1))
        self.assertEqual(result["quality"]["status"], "incomplete")
        text = Path(result["report"]).read_text()
        self.assertIn("PARTIAL REPORT", text)
        self.assertIn("## 2. missing 空间.wav", text)
        self.assertIn("**FAILED", text)
        self.assertIn("## 3. last.wav", text)
        self.assertEqual(self.calls, 2)

    def test_decoder_failure_does_not_disappear_or_stop_next_file(self):
        paths = [self.source("failed.wav", 10), self.source("good.wav", 20)]
        def decode(*args, **kwargs):
            if self.calls == 0:
                self.calls += 1
                raise RuntimeError("mock decoder failure; raw text must not appear in report")
            return self.decode(*args)
        with patch.object(engine, "decode", side_effect=decode):
            result = self.report(paths)
        self.assertEqual((result["completed"], result["failed"]), (1, 1))
        self.assertNotIn("raw text must not", Path(result["report"]).read_text())

    def test_failed_raw_timing_is_revalidated_without_new_asr(self):
        import platform
        path=self.source("invalid.wav",10)
        def invalid_decoder(*args, **kwargs):
            meta=self.decode(*args)
            settings,runtime,model,wav,out,asr,glossary=args
            storage.write_json(out/"native.json",{"transcription":[{"offsets":{"from":900,"to":1751},"text":"Raw fixture remains preserved."}]},overwrite=True)
            command=[runtime["runtime"]["cli"],"-m",model["path"],"-f",str(wav),"-l",asr["language"],"-t",str(asr["threads"]),"-bs",str(asr["beam_size"]),"-tp",str(asr["temperature"]),"-tpi",str(asr["temperature_increment"]),"-ojf","-of",str(out/"native")]
            if platform.machine() != "arm64":command.append("-ng")
            storage.write_json(out/"invocation.json",{"args":command,"input_sha256":storage.sha256_file(wav),"model_sha256":model["sha256"]})
            return meta
        with patch.object(engine,"decode",side_effect=invalid_decoder):
            first=self.report([path])
        raw=next(self.data.glob("sessions/*/transcript/*/logs/*/native.json")); before=raw.read_bytes()
        with patch.object(export,"run_session",side_effect=AssertionError("Invalid cached raw must not be retranscribed")):
            second=self.report([path])
        self.assertEqual(second["review_required"],1)
        self.assertEqual(second["failed"],0)
        manifest=storage.read_doc(Path(second["report"]).with_name("manifest.json"))
        source=manifest["ordered_sources"][0]
        self.assertEqual(source["state"], "review_required")
        self.assertIn("invalid_timestamp", source["transcript"]["quality"]["reasons"])
        self.assertIn("Raw fixture remains preserved.", Path(second["report"]).read_text())
        self.assertEqual(raw.read_bytes(),before)
        self.assertEqual(self.calls,1)

    def test_newer_failure_does_not_hide_older_verified_success(self):
        path=self.source("cached-good.wav",10)
        good=self.report([path])
        session=next(self.data.glob("sessions/*/session.yaml")).parent
        with patch.object(engine,"decode",side_effect=RuntimeError("Fixture failed retry")):
            with self.assertRaises(RuntimeError):
                engine.run_session(self.settings,session,self.resolved,force=True)
        with patch.object(export,"reject_preserved_invalid_timeline",side_effect=AssertionError("Successful cache must be preferred")),patch.object(export,"run_session",side_effect=AssertionError("No inference")):
            reused=self.report([path])
        self.assertEqual(reused["report"],good["report"])
        self.assertEqual(reused["completed"],1)

    def test_identical_selection_is_explicitly_retained_without_extra_decode(self):
        a = self.source("part2.wav", 10)
        alias = self.root / "same bytes.wav"
        alias.write_bytes(a.read_bytes())
        result = self.report([a, alias, a])
        entries = storage.read_doc(Path(result["report"]).with_name("manifest.json"))["ordered_sources"]
        self.assertEqual([e["duplicate_of"] for e in entries], [None, 1, 1])
        self.assertEqual(result["completed"], 3)
        self.assertEqual(self.calls, 1)
        self.assertEqual(Path(result["report"]).read_text().count(export.markdown_text(self.raw[0])), 6)

    def test_same_name_changed_bytes_and_changed_settings_do_not_reuse_stale_result(self):
        a = self.source("same.wav", 10)
        first = self.report([a])
        self.source("same.wav", 20)
        second = self.report([a])
        self.assertEqual(self.calls, 2)
        changed = copy.deepcopy(self.resolved)
        changed["asr"]["beam_size"] = 6
        third = self.report([a], changed)
        self.assertEqual(self.calls, 3)
        self.assertEqual(len({r["report"] for r in (first, second, third)}), 3)

    def test_source_specific_gain_is_recomputed(self):
        a, b = self.source("quiet.wav", 10), self.source("louder.wav", 10000)
        report = self.report([a, b])
        entries = storage.read_doc(Path(report["report"]).with_name("manifest.json"))["ordered_sources"]
        gains = []
        for e in entries:
            t = e["transcript"]
            d = storage.read_doc(self.data / "sessions" / t["session_id"] / "transcript" / t["run_id"] / "diagnostics.json")
            gains.append(d["sources"][0]["transform"]["gain_db"])
        self.assertGreater(gains[0], gains[1])

    def test_interrupted_publication_has_no_visible_report_and_retry_reuses_asr(self):
        paths = [self.source("a.wav", 10), self.source("b.wav", 20)]
        original = export.os.rename
        def rename(a, b):
            if str(a).endswith(".pending"): raise OSError("simulated publication interruption")
            return original(a, b)
        with patch.object(export.os, "rename", side_effect=rename), self.assertRaises(OSError):
            self.report(paths)
        self.assertFalse(library.list_library(self.settings)["reports"])
        self.assertEqual(self.calls, 2)
        result = self.report(paths)
        self.assertEqual(self.calls, 2)
        self.assertEqual(result["completed"], 2)

    def test_changed_export_is_preserved_and_versioned(self):
        a = self.source("single.wav", 10)
        first = self.report([a])
        report = Path(first["report"])
        report.write_text("Owner edited fixture", encoding="utf-8")
        second = self.report([a])
        self.assertNotEqual(first["report"], second["report"])
        self.assertEqual(report.read_text(), "Owner edited fixture")
        self.assertEqual(self.calls, 1)

    def test_unrelated_corrupt_export_does_not_block_new_report(self):
        broken = self.data / "exports" / "batch-unrelated" / "manifest.json"
        broken.parent.mkdir(parents=True)
        broken.write_text("not valid JSON")
        result = self.report([self.source("new.wav", 10)])
        self.assertEqual(result["completed"], 1)
        self.assertEqual(broken.read_text(), "not valid JSON")

    def test_native_launch_reveals_markdown_even_when_partial(self):
        paths = [str(self.source("a.wav", 10)), str(self.root / "missing.wav")]
        payload = {"files": paths, "profiles": {"speaker": None, "capture": None, "glossary": None}}
        with patch.object(cli, "native_dialog", return_value=payload), patch.object(cli.subprocess, "run") as opener, patch("sys.stdout", new_callable=io.StringIO):
            result = cli.launch(self.settings)
        self.assertEqual(result["state"], "partial")
        self.assertEqual(opener.call_args.args[0], ["/usr/bin/open", "-R", result["report"]])

    def test_cli_confirmation_cancel_correction_and_progress(self):
        a, b = self.source("part2.wav", 10), self.source("part10.wav", 20)
        args = cli.parser().parse_args(["report", str(b), str(a)])
        with patch.object(cli.sys.stdin, "isatty", return_value=False), self.assertRaises(ValueError):
            cli.report_command(self.settings, args)
        with patch.object(cli.sys.stdin, "isatty", return_value=True), patch("builtins.input", return_value=""), patch("sys.stdout", new_callable=io.StringIO):
            self.assertTrue(cli.report_command(self.settings, args)["cancelled"])
        self.assertFalse(self.data.exists())
        with patch.object(cli.sys.stdin, "isatty", return_value=True), patch("builtins.input", return_value="2,1"), patch("sys.stdout", new_callable=io.StringIO) as stdout:
            result = cli.report_command(self.settings, args)
        entries = storage.read_doc(Path(result["report"]).with_name("manifest.json"))["ordered_sources"]
        self.assertEqual([e["filename"] for e in entries], [b.name, a.name])
        self.assertIn("2 of 2", stdout.getvalue())
        self.assertNotIn(self.raw[0], stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
