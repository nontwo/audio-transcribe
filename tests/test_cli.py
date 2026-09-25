"""CLI workflow fixtures; dialog/decoder mocks are not actual launcher evidence."""
import io
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import wave

from audio_transcribe import cli, config, storage


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.data = self.root / "managed data 空间"
        self.settings = config.load_settings(self.root / "fixture-settings.json")
        self.settings["roots"] = {
            name: str(self.data if name == "data" else self.root / name)
            for name in ("code", "data", "app", "cache", "log")
        }
        self.settings["storage_approved"] = True
        self.emit_patcher = patch("audio_transcribe.cli.emit")
        self.emit = self.emit_patcher.start()
        self.addCleanup(self.emit_patcher.stop)
        never_infer = patch("audio_transcribe.engine.decode", side_effect=AssertionError(
            "CLI fixture tests must never invoke inference."
        ))
        never_infer.start()
        self.addCleanup(never_infer.stop)

    def source(self, name="recording with 空间.wav", amplitude=10):
        path = self.root / "external files" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as output:
            output.setparams((1, 2, 16000, 1600, "NONE", "not compressed"))
            output.writeframes(struct.pack("<h", amplitude) * 1600)
        return path

    def profiles(self):
        selected = {kind: "fixture-" + kind for kind in ("speaker", "capture", "glossary")}
        for kind, identifier in selected.items():
            config.create_profile(self.data, kind, identifier, "Fixture " + kind)
        return selected

    def assigned_session(self):
        selected = self.profiles()
        path, session, _ = storage.import_sources(self.data, [self.source()])
        session["profiles"] = selected
        storage.write_yaml(path / "session.yaml", session, overwrite=True)
        return path, session, selected

    def transcribe(self, *arguments):
        args = cli.parser().parse_args(["transcribe", *map(str, arguments)])
        return cli.transcribe(self.settings, args)

    def test_existing_session_inherits_manual_profiles(self):
        path, session, selected = self.assigned_session()
        before = (path / "session.yaml").read_bytes()
        with patch("audio_transcribe.cli.run_session", return_value={"state": "fixture"}) as runner:
            self.transcribe("--session", session["id"])
        self.assertEqual(runner.call_args.args[2]["selected_profiles"], selected)
        self.assertEqual(runner.call_args.args[1], path)
        self.assertEqual((path / "session.yaml").read_bytes(), before)

    def test_pilot_and_benchmark_inherit_manual_profiles(self):
        path, session, selected = self.assigned_session()
        for operation in ("pilot", "benchmark"):
            with self.subTest(operation=operation):
                with patch("audio_transcribe.cli.load_settings", return_value=self.settings), \
                     patch("audio_transcribe.cli.signal.signal"), \
                     patch("audio_transcribe.cli.run_benchmark", return_value={"state": "fixture"}) as runner:
                    self.assertEqual(cli.main([operation, session["id"]]), 0)
                self.assertEqual(runner.call_args.args[1], path)
                self.assertEqual(runner.call_args.args[2]["selected_profiles"], selected)
                self.assertEqual(runner.call_args.kwargs["pilot"], operation == "pilot")

    def test_import_only_persists_selected_profiles_without_inference(self):
        selected = self.profiles()
        source = self.source()
        original = source.read_bytes()
        with patch("audio_transcribe.cli.run_session") as runner:
            result = self.transcribe("--import-only", "--speaker", selected["speaker"],
                                     "--capture", selected["capture"], "--glossary", selected["glossary"], source)
        runner.assert_not_called()
        session = storage.read_doc(Path(result["path"]) / "session.yaml")
        self.assertEqual(session["profiles"], selected)
        self.assertEqual(result["state"], "imported")
        self.assertEqual(source.read_bytes(), original)

    def test_explicit_dash_clears_only_requested_profile(self):
        path, session, selected = self.assigned_session()
        self.transcribe("--session", session["id"], "--import-only", "--speaker", "-")
        expected = {**selected, "speaker": None}
        self.assertEqual(storage.read_doc(path / "session.yaml")["profiles"], expected)
        self.transcribe("--session", session["id"], "--import-only", "--capture", "-", "--glossary", "-")
        self.assertEqual(storage.read_doc(path / "session.yaml")["profiles"],
                         {kind: None for kind in selected})

    def test_explicit_profile_overrides_session_and_new_files_start_unassigned(self):
        _, session, selected = self.assigned_session()
        config.create_profile(self.data, "speaker", "replacement")
        with patch("audio_transcribe.cli.run_session", return_value={"state": "fixture"}) as runner:
            self.transcribe("--session", session["id"], "--speaker", "replacement")
        self.assertEqual(runner.call_args.args[2]["selected_profiles"], {**selected, "speaker": "replacement"})
        result = self.transcribe("--import-only", self.source("new unrelated.wav", amplitude=20))
        self.assertEqual(storage.read_doc(Path(result["path"]) / "session.yaml")["profiles"],
                         {kind: None for kind in selected})

    def test_invalid_and_missing_files_do_not_import_any_selection(self):
        valid = self.source()
        invalid = self.root / "invalid.wav"
        invalid.write_bytes(b"This synthetic fixture is not a WAV.")
        for bad in (invalid, self.root / "missing.wav"):
            with self.subTest(path=bad.name):
                with patch("audio_transcribe.cli.import_sources") as importer:
                    with self.assertRaises((ValueError, OSError)):
                        self.transcribe("--import-only", "--order-confirmed", valid, bad)
                importer.assert_not_called()
        self.assertFalse((self.data / "sessions").exists())

    def test_multiple_files_require_order_and_preserve_confirmed_position(self):
        first, second = self.source("first.wav", 10), self.source("second 空间.wav", 20)
        with patch.object(cli.sys.stdin, "isatty", return_value=False), \
             patch("audio_transcribe.cli.import_sources") as importer:
            with self.assertRaisesRegex(ValueError, "--order-confirmed"):
                self.transcribe("--import-only", second, first)
        importer.assert_not_called()
        result = self.transcribe("--import-only", "--order-confirmed", second, first)
        session = storage.read_doc(Path(result["path"]) / "session.yaml")
        by_id = {source["id"]: source for source in session["sources"]}
        self.assertEqual([by_id[sid]["original_basename"] for sid in session["source_order"]],
                         [second.name, first.name])

    def test_interactive_order_decline_is_a_clean_cancellation(self):
        first, second = self.source("first.wav", 10), self.source("second.wav", 20)
        with patch.object(cli.sys.stdin, "isatty", return_value=True), \
             patch("builtins.input", return_value="n"), patch("sys.stdout", new_callable=io.StringIO), \
             patch("audio_transcribe.cli.import_sources") as importer:
            self.assertEqual(self.transcribe("--import-only", first, second), {"cancelled": True})
        importer.assert_not_called()

    def test_native_dialog_mock_cancellation_and_literal_json_argument(self):
        # This checks the process boundary only; it does not claim a macOS UI test.
        payload = {"action": "choose", "label": '空间 "quoted" $(touch should-not-exist)'}
        completed = subprocess.CompletedProcess([], 0, stdout='{"cancelled": true}\n', stderr="")
        with patch("audio_transcribe.cli.subprocess.run", return_value=completed) as process:
            self.assertEqual(cli.native_dialog(self.root / "code path", payload), {"cancelled": True})
        arguments = process.call_args.args[0]
        self.assertEqual(arguments[:3], ["/usr/bin/osascript", "-l", "JavaScript"])
        self.assertEqual(arguments[3], str(self.root / "code path/scripts/native-dialog.js"))
        self.assertEqual(json.loads(arguments[4]), payload)
        self.assertFalse(process.call_args.kwargs.get("shell", False))

    def test_cancelled_launcher_mock_never_creates_profiles_or_imports(self):
        # Extra fields must not matter once cancellation has been returned.
        selection = {"cancelled": True, "new_profiles": [{"kind": "speaker", "id": "do-not-create"}],
                     "files": [str(self.source())]}
        with patch("audio_transcribe.cli.native_dialog", return_value=selection), \
             patch("audio_transcribe.cli.create_profile") as creator, \
             patch("audio_transcribe.cli.transcribe") as transcriber, \
             patch("audio_transcribe.cli.subprocess.run") as opener:
            self.assertEqual(cli.launch(self.settings), {"cancelled": True, "imported": False})
        creator.assert_not_called()
        transcriber.assert_not_called()
        opener.assert_not_called()
        self.assertFalse(self.data.exists())

    def test_native_dialog_failure_does_not_echo_untrusted_stderr(self):
        completed = subprocess.CompletedProcess([], 1, stdout="", stderr="synthetic private dialog text")
        with patch("audio_transcribe.cli.subprocess.run", return_value=completed):
            with self.assertRaises(RuntimeError) as caught:
                cli.native_dialog(self.root, {"action": "choose"})
        self.assertNotIn(completed.stderr, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
