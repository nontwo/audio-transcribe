# Profile schema, version 1

Copying these generic examples is optional; the CLI can create a manually named speaker, capture, or glossary. Personal documents belong under DATA_ROOT/profiles, never in the code templates. Files may use YAML or JSON. Quote dates and version strings. Unknown keys, cross-domain settings, unsafe IDs, unsupported values and duplicate profile files are errors.

All profiles use `schema_version: 1`, `kind`, and a stable `id` containing 1–96 letters, digits, underscores or hyphens, beginning with a letter or digit. Optional common fields are `label`, `language` (en or null), and `notes`.

- Speaker-only fields: `terms` (manually verified short terms), `asr`, `calibration_state` (`uncalibrated`, `provisional`, or `validated_for_listed_clips`), `provenance` (text), `evaluation_refs` (list of strings), and `recipes` (list).
- Capture-only fields: `device`, `placement`, `room` (text or null), `preprocessing`, `evidence` (list of strings), and `applicability` (list of strings). Matching a capture profile is a manual assignment, not an inference from audio.
- Glossary-only fields: `terms` (at most 100 nonempty strings, each at most 200 characters and without newlines), `provenance` (text), and `revision` (text). Effective run vocabulary combines manually verified speaker terms and the selected glossary, deduplicated in that order. Its exact content hash and term sources are saved.

`asr` accepts only `model` (`large-v3` or `large-v3-turbo`), `language` (`en`), `temperature`, `temperature_increment`, `beam_size`, `threads`, and `max_context`. `preprocessing` accepts only `channel` (`mean`, `left`, `right`), `candidate` (`A`, `B`), `peak_dbfs`, `max_gain_db`, `gain_db` (numeric or null), `vad` (false), and `filtering` (`none`). Defaults, profile scopes, a matching validated recipe, then explicit overrides determine a run. Decoder preferences never enter capture preprocessing; capture fields never enter a speaker's ASR settings.

An advanced recipe contains `id`, `state` (`provisional` or `validated_for_listed_clips`), `applicability`, `asr`, `preprocessing`, `model_precision` (`f16`), `evaluation_refs`, and optional text `provenance`. Applicability contains the exact `speaker_id`, `capture_id`, effective `glossary_sha256`, and boolean `apply_to_matching_profiles`. A recipe applies only when its state is validated, evaluation references are present, that flag is explicitly true, and all three identities match. Multiple matches produce an error. The flag is a deliberate scope decision; successful decoding or an unreviewed benchmark never sets it. A validated clip result is not a claim of universal accuracy.

Resolved run configurations copy the complete selected profile documents and canonical content hashes. Editing a profile affects future resolutions only. Full model file hashes, runtime identity and actual transforms belong in each run manifest, not in a reusable teacher identity.

The default `max_context: 0` prevents prior-text propagation and also disables
glossary prompting in the pinned whisper.cpp version. Nonempty vocabulary is
retained in provenance and raises a visible review warning; it must not be
assumed to have influenced decoding. Enabling context is an explicit experiment
that can reintroduce repetition loops, not a general accuracy improvement.
