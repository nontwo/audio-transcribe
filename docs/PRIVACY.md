# Local data and public contributions

Audio processing and report browsing run locally. There is no telemetry,
transcription upload, cloud account or hosted inference endpoint. Initial setup
downloads software and model files from their public upstream sources. The
Whisper and FFmpeg subprocesses deny network access on macOS.

Local data can be sensitive: recordings, filenames, transcript text, manual
course labels, local paths, glossary terms and diagnostic manifests. Keep the
data root outside this checkout. Library grouping and report exports stay under
the selected data root. The app stores its queue, processing states and last report path in local
macOS preferences. Recently Deleted is reversible library metadata: original
audio, reports and ASR artifacts remain on disk until separately managed. Open Report or Open Audio uses the user's chosen application;
that application's privacy behavior is separate.

The source repository excludes all personal recordings, transcriptions, local
settings, machine validation records, models, private environments and compiled
apps. Tests generate synthetic fixtures. Ignore rules reduce accidental staging;
they do not make arbitrary attachments or issue text safe to publish.

Before filing an issue, replace names, paths, identifiers and transcript excerpts
with a minimal synthetic example. Do not attach private audio, settings files,
complete logs, credentials or authorizing URLs. A public GitHub account and its
repository ownership remain publicly associated even when commit email uses
GitHub's noreply address.
