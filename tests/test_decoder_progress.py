"""Numeric progress is telemetry, not transcript output or completion evidence."""
from pathlib import Path
import tempfile
import unittest

from audio_transcribe.progress import DecoderProgress


class DecoderProgressTests(unittest.TestCase):
    def test_split_callback_lines_and_untrusted_text(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "stderr.log"
            path.write_bytes(b"loading model\nwhisper_print_progress_callback: progress =  3")
            progress = DecoderProgress(path)
            self.assertIsNone(progress.read())
            with path.open("ab") as stream:
                stream.write(b"5%\ntranscript text progress = 99%\n"
                             b"whisper_print_progress_callback: progress = 999%\n")
            self.assertEqual(progress.read(), 35)
            self.assertEqual(progress.read(), 35)
            with path.open("ab") as stream:
                stream.write(b"whisper_print_progress_callback: progress =  20%\n"
                             b"whisper_print_progress_callback: progress = 100%\r\n")
            self.assertEqual(progress.read(), 100)

    def test_unexpected_long_line_has_bounded_read_and_buffer(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "stderr.log"
            path.write_bytes(b"x" * 140000 + b"\nwhisper_print_progress_callback: progress =  50%\n")
            progress = DecoderProgress(path)
            self.assertIsNone(progress.read())
            self.assertEqual(progress.offset, 65536)
            self.assertLessEqual(len(progress.pending), 1024)
            self.assertIsNone(progress.read())
            self.assertEqual(progress.read(), 50)
