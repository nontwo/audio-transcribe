"""Read only whisper.cpp's numeric progress callback, never transcript text."""
import re


class DecoderProgress:
    pattern = re.compile(rb"^whisper_print_progress_callback: progress =\s+(\d{1,3})%$")

    def __init__(self, path):
        self.path = path
        self.offset = 0
        self.pending = b""
        self.percent = None

    def read(self):
        # Both work and retained data are bounded even with unexpected log output.
        with self.path.open("rb") as stream:
            stream.seek(self.offset)
            chunk = stream.read(65536)
            self.offset = stream.tell()
        lines = (self.pending + chunk).split(b"\n")
        self.pending = lines.pop()[-1024:]
        for line in lines:
            match = self.pattern.fullmatch(line.rstrip(b"\r"))
            if match and 0 <= int(match[1]) <= 100:
                value = int(match[1])
                self.percent = max(self.percent or 0, value)
        return self.percent
