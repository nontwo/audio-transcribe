# Quality approach

## Why repetition can propagate

Whisper can feed earlier decoded text into its next audio window. A mistaken
phrase can then reinforce itself across later windows. The official
[OpenAI transcription implementation](https://github.com/openai/whisper/blob/main/whisper/transcribe.py)
documents disabling previous-text conditioning as a way to reduce repetition
loops and timestamp drift, with a tradeoff in consistency between windows.
This explains a possible mechanism, not the initial cause of every ASR mistake.

[faster-whisper](https://github.com/SYSTRAN/faster-whisper/blob/master/faster_whisper/transcribe.py)
also exposes previous-text conditioning, prompt resets and fallback thresholds.
These established controls inform this application's approach; they do not imply
that changing engines or enabling every filter improves every recording.

## Implemented safeguards

- Default `asr.max_context=0` maps to the pinned whisper.cpp `-mc 0` control.
  Audio, model and other decoding settings remain independently traceable.
  In the pinned decoder, this also disables glossary prompting. A selected
  nonempty glossary produces a visible warning; terminology still needs review.
  Explicitly restoring context may restore prompting but also reintroduce loops.
- The exact context setting participates in run/cache identity. An older result
  from a different context policy cannot silently satisfy the new request.
- Source-local checks detect repeated word sequences, including alternating
  phrases split across multiple transcript segments. Short ordinary repetition
  is not automatically rewritten or removed.
- Compression ratio on approximate 30-second transcript windows is an additional
  review signal. It is not the same measurement as a decoder's internal window
  and is not a calibrated confidence score.
- Temperature fallback remains enabled in the decoder. It is insufficient by
  itself to prove the absence of loops, so post-decode checks remain necessary.
- Source hashes, submitted sample counts, per-file gain, runtime/model identity
  and raw timestamps remain recorded. A technical completion receipt does not
  establish word accuracy.

## Review states and timestamps

`passed_checks` means only that the configured automatic checks did not flag the
result. `review_required` retains every raw word, supplies the affected intervals
and keeps `accuracy_verified=false`. A failure indicates that usable output could
not be safely assembled; it is not silently omitted from a batch.

Timing validation uses each source's actual frame count and rate. A narrowly
defined final decoder-tick representation can be normalized to exact EOF, with
the raw value retained. Other invalid predictions stay visible as unvalidated
timestamps in review output. Such output is not published as a verified subtitle
or promoted to a successful-run pointer. There is no user-imposed duration cap.

No automatic word deletion, generated replacement text or summary is used to
make an output look cleaner. Existing raw attempts remain available for a
controlled comparison. No VAD speech-removal filter is enabled by default.

## How to evaluate accuracy

Choose representative excerpts from different speakers and recording conditions,
including the beginning, middle, end and flagged intervals. Have a person check
the reference against the audio before calculating word error rate with the
existing `evaluate` workflow. Compare the same intervals and preprocessing,
record the exact settings, and keep references and audio private.

Review names, terminology, numerical values and negation separately; one aggregate
WER can hide consequential mistakes. Complete-frame submission does not prove
that every spoken word was recognized. Agreement between models is not ground
truth. Do not publish a universal accuracy percentage from unreviewed examples.

The [Whisper model card](https://github.com/openai/whisper/blob/main/model-card.md)
describes hallucination and uneven accuracy across conditions. Context isolation
reduces one observed failure mechanism; it cannot eliminate those limitations.
If a recording still fails the gates, retain its review state and investigate
the flagged audio before changing channel selection, adding filtering, or
attempting segmentation with explicit coverage checks.

## Deliberately deferred

Automatic re-decoding of flagged intervals and VAD-guided segmentation are not
implemented. They require bounded retries, complete source-time mapping,
retained original/retry outputs and listening-based validation before replacing
a result. A review flag currently preserves the text and directs manual review;
it does not claim that lost speech has been automatically recovered.
