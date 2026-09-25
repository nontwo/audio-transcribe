# AudioTranscribe

A local macOS recording library and transcription app. Select recordings, confirm
which belong together, and produce a self-contained Markdown transcript report.
The native AppKit interface runs a Python pipeline backed by whisper.cpp and
Whisper large-v3. No transcription service or account is required.

## Everyday use

1. Open **AudioTranscribe** from `~/Applications/AudioTranscribe.app`.
2. Drop or select recordings in **New Transcription** and confirm their order.
3. Review the proposed dates and class groups. Split or combine groups as needed.
4. Click **Transcribe** and watch the actual decoder progress and elapsed time.
5. Browse the result in **Library**, search the transcript, review flagged content,
   or open the complete `transcript-report.md`.

Grouping is a suggestion, not speaker identification or proof that recordings
belong to the same class. Unknown dates remain unknown. A complete batch report
retains every selected source in order; a failed source has a visible entry.
Class sections and the library provide dated views without joining source audio.

## Quality and repetition

Long recordings can trigger Whisper feedback loops when generated text is reused
as context for subsequent windows. The default clears prior decoded text context
(`--max-context 0`). This changes decoding, not source audio. Temperature fallback
remains enabled, and repeated phrases/alternating loops are checked after decoding.

**Automatic checks are not an accuracy guarantee.** Results distinguish automatic
checks passed, review required, and failure. Suspicious text remains available;
it is never silently deleted or rewritten. Invalid timestamps are identified as
unvalidated rather than causing all recognized text to disappear. Review flagged
intervals against the original audio, especially names, numbers, technical terms
and negation. Measure WER only against a human-verified, aligned reference.

See [the quality approach](docs/QUALITY.md) for the reasoning, upstream references
and limitations. There is no universal WER score for arbitrary classroom audio.

## Install from source

The native app targets macOS 13 or newer. The primary local inference path is
Apple Silicon with Metal. Intel builds can use CPU decoding but are not the
primary performance target. Keep sufficient memory and disk space for large-v3,
working audio and immutable results.

Prerequisites: Apple command-line developer tools (Swift and C/C++ toolchain),
Python 3.12, and [uv](https://docs.astral.sh/uv/). From the cloned project:

```sh
uv venv --seed --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt -r requirements-build.txt
.venv/bin/python scripts/setup-runtime.py
./scripts/audio-transcribe configure --data-root ~/AudioTranscription --storage-mode local_alternative
.venv/bin/python scripts/build-app.py --install
```

Runtime setup downloads and checks the pinned whisper.cpp revision plus standard
F16 large-v3 and large-v3-turbo model files. The primary model is large-v3; model
weights are several GB and are not part of this repository. The local build
obtains a pinned `imageio-ffmpeg` binary in `native/vendor/` for non-WAV decoding.
Initial setup requires network access; transcription runs locally.

The app is built into `native/build/AudioTranscribe.app` and installed into
`~/Applications/AudioTranscribe.app`. This source-build app refers to its checkout
and `.venv`, so keep them in place or rebuild after moving them. It does not need
a terminal to remain open. Local ad-hoc signing is used; this is not a notarized,
self-contained binary distribution.

An existing settings file is preserved. For a different configuration, place
`--settings /path/settings.json` before the command, or set
`AUDIO_TRANSCRIBE_SETTINGS`. Choose a local data folder explicitly; if intentionally
using a synced folder, use `--storage-mode explicit_synced` after considering
that audio and text will be synced by that service.

## Supported media

WAV/WAVE, M4A, MP3, FLAC, AAC, AIFF/AIF, OGG/Vorbis, OGG/Opus and audio-bearing
MP4/MOV are accepted when the installed decoder supports their codec. The first
audio stream is used. Video frames are not processed, and separate recordings
are never concatenated before transcription. Corrupt or unsupported files fail
individually without silently disappearing from the batch.

Non-WAV sources are copied intact and decoded to a working WAV before per-file
channel selection, measured gain and resampling. Measured frames determine
duration. Original files are hash-checked and never modified. Identical selections
remain explicit entries while sharing a valid matching transcription cache.

## Dates and class groups

Recording dates and start times are parsed conservatively from recognized recorder
filenames and explicit user metadata. Their provenance is retained. File copy or
modification time is not silently promoted to the recording date. A filename does
not establish a timezone or a course identity.

Known start times and measured duration support continuity suggestions. Gaps,
overlaps and missing information remain visible for confirmation. The user can
split groups, merge adjacent recordings, and name a class. The library organizes
reports by date and time; unknown dates have their own section. Existing source
and transcript artifacts stay in place.

## Storage and privacy

The selected data root contains managed originals, session manifests, derived
working audio, exports and library metadata. Runtime/model files use
`~/Library/Application Support/AudioTranscribe`; caches and logs use the matching
macOS Library directories. The source checkout is separate from permanent data.

No recordings, transcripts, personal settings, local validation records, model
weights or compiled apps belong in Git. The public tests use generated fixtures.
See [privacy and contribution guidance](docs/PRIVACY.md). Sharing a Markdown
report deliberately shares its full transcript; review it before uploading it
elsewhere.

## Advanced CLI

```sh
./scripts/audio-transcribe doctor
./scripts/audio-transcribe report --order-confirmed /path/part2.wav /path/part10.m4a
./scripts/audio-transcribe report --order-confirmed --retry-failed /path/recording.wav
./scripts/audio-transcribe transcribe --session SESSION_ID
./scripts/audio-transcribe --help
```

`Transcribe.command` opens the installed app, with the existing CLI/native-dialog
fallback retained. Optional speaker, capture and glossary profiles are documented
in [templates/README.md](templates/README.md). Profiles are manual and do not
identify speakers automatically. Store personal profiles in the data root.

Every attempt preserves raw decoder output, source identity, processing settings
and validation. Cached results require matching audio, processing and decoder
identities. **Cancel** stops the owned processing child and retains earlier work.
There is no arbitrary maximum recording duration; whole-array preprocessing and
RIFF working-file size remain practical limits. No VAD-based speech removal is
enabled by default.

## Development

The test suite needs the private media decoder provisioned by `build-app.py`, but
not Whisper model weights. With the isolated environment above:

```sh
.venv/bin/python scripts/build-app.py
.venv/bin/python -m pytest -q
sh scripts/test-native.sh
```

CI builds the native shell and runs synthetic regression checks on macOS. Mocked
ASR and logic tests do not establish live inference or listening-based accuracy.
Use small, non-private fixtures when contributing; never upload real recordings
or machine diagnostics to explain a bug.

## License

Original project code is MIT licensed. Separately obtained dependencies and model
files retain their upstream licenses; see [third-party notices](THIRD_PARTY_NOTICES.md).
