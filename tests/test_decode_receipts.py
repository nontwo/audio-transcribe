"""Mocked decoder protocol, not model-accuracy evidence."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

from audio_transcribe import config, engine, storage
from audio_transcribe.timeline import TimelineError


class DecodeReceiptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.wav = self.root / "synthetic.wav"
        with wave.open(str(self.wav), "wb") as stream:
            stream.setparams((1, 2, 16000, 16000, "NONE", "not compressed"))
            stream.writeframes(b"\0\0" * 16000)
        self.output = self.root / "result"
        self.settings = {"roots": {"app": str(self.root / "app")}}
        self.runtime = {"runtime": {"cli": "/synthetic/whisper-cli", "sha256": "synthetic-runtime"}}
        self.model = {"path": "/synthetic/model", "sha256": "synthetic-model"}
        self.end_ms, self.reported_frames = 1000, 16000
        self.invocations = []

    def fake_process(self, args, **kwargs):
        self.invocations.append(args)
        prefix = Path(args[args.index("-of") + 1])
        storage.write_json(prefix.with_suffix(".json"), {"transcription": [
            {"offsets": {"from": 0, "to": self.end_ms}, "text": "A synthetic output fixture."}]})
        kwargs["stderr"].write(f"processing synthetic.wav ({self.reported_frames} samples, 1.0 sec)\n".encode())
        class Process:
            returncode = 0
            def poll(self):
                return 0
        return Process()

    def decode(self, **kwargs):
        with patch.object(engine.subprocess, "Popen", side_effect=self.fake_process), \
                patch.object(engine, "memory_snapshot", return_value={}), \
                patch.object(engine.platform, "machine", return_value="synthetic-cpu"):
            return engine.decode(self.settings, self.runtime, self.model, self.wav, self.output,
                                 config.defaults()["asr"], {"terms": []}, **kwargs)

    def test_default_disables_context_in_actual_cli_and_has_valid_receipt(self):
        result = self.decode()
        args = self.invocations[0]
        self.assertEqual(args[args.index("-mc") + 1], "0")
        self.assertTrue(result["timestamp_valid"])
        self.assertTrue((self.output / "complete.json").exists())
        self.assertFalse((self.output / "provisional.json").exists())

    def test_invalid_timing_only_returns_explicit_provisional_receipt(self):
        self.end_ms = 1040
        result = self.decode(permit_review=True)
        self.assertEqual(result["state"], "review_required")
        self.assertFalse(result["timestamp_valid"])
        self.assertFalse((self.output / "complete.json").exists())
        self.assertTrue((self.output / "provisional.json").exists())
        self.assertEqual(storage.read_doc(self.output / "native.json")["transcription"][0]["offsets"]["to"], 1040)
        self.assertEqual(self.decode(permit_review=True), result)
        self.assertEqual(len(self.invocations), 1)

    def test_strict_decoder_call_still_rejects_invalid_timing(self):
        self.end_ms = 1040
        with self.assertRaises(TimelineError):
            self.decode()
        self.assertFalse((self.output / "complete.json").exists())
        self.assertFalse((self.output / "provisional.json").exists())

    def test_provisional_permission_never_bypasses_complete_sample_evidence(self):
        self.end_ms = 1040
        self.reported_frames = 15999
        with self.assertRaisesRegex(RuntimeError, "sample-count"):
            self.decode(permit_review=True)
        self.assertFalse((self.output / "complete.json").exists())
        self.assertFalse((self.output / "provisional.json").exists())

    def test_child_exit_race_does_not_replace_cancellation_with_failure(self):
        original_process = self.fake_process
        waits = []
        def running_process(args, **kwargs):
            original_process(args, **kwargs)
            class Process:
                pid, returncode = 123456789, None
                def poll(self):
                    return None
                def wait(self, timeout=None):
                    waits.append(timeout)
                    return 0
            return Process()
        self.fake_process = running_process
        def cancel_at_inference(event):
            if event["stage"] == "transcribing":
                raise KeyboardInterrupt()
        with patch.object(engine.os, "killpg", side_effect=ProcessLookupError()), self.assertRaises(KeyboardInterrupt):
            self.decode(progress=cancel_at_inference)
        self.assertEqual(waits, [8])
        self.assertFalse((self.output / "complete.json").exists())
