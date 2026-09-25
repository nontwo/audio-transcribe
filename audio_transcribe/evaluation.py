"""Explicit reference evaluation; never use model output as ground truth."""
from __future__ import annotations

import re
import unicodedata

NORMALIZATION = {
    "version": "wer-nfkc-casefold-whitespace-v1",
    "rules": "Unicode NFKC; casefold; split on whitespace. Punctuation, negatives, quantities and technical words are retained.",
}


def tokens(text: str) -> list[str]:
    return unicodedata.normalize("NFKC", text).casefold().split()


def word_error_rate(reference: str | None, hypothesis: str, *, verified: bool = False) -> dict:
    if reference is None or not verified:
        return {"reference_status": "not_provided" if reference is None else "unverified",
                "WER": None, "normalization": NORMALIZATION.copy(), "N": None,
                "S": None, "D": None, "I": None}
    ref, hyp = tokens(reference), tokens(hypothesis)
    # Entries carry edit counts; deterministic tie order substitution, deletion, insertion.
    prev = [(j, 0, 0, j) for j in range(len(hyp) + 1)]
    for i, word in enumerate(ref, 1):
        row = [(i, 0, i, 0)]
        for j, other in enumerate(hyp, 1):
            if word == other:
                row.append(prev[j - 1])
            else:
                c, s, d, ins = prev[j - 1]
                a = (c + 1, s + 1, d, ins)
                c, s, d, ins = prev[j]
                b = (c + 1, s, d + 1, ins)
                c, s, d, ins = row[j - 1]
                z = (c + 1, s, d, ins + 1)
                row.append(min((a, b, z), key=lambda x: x[0]))
        prev = row
    errors, substitutions, deletions, insertions = prev[-1]
    return {"reference_status": "verified", "WER": errors / len(ref) if ref else None,
            "undefined_reason": "empty_reference" if not ref else None,
            "N": len(ref), "S": substitutions, "D": deletions, "I": insertions,
            "normalization": NORMALIZATION.copy()}


def timestamp(seconds: float, *, srt: bool = False) -> str:
    # Provisional Markdown may show invalid raw negative model times. Do not
    # silently present them as zero. SRT is written only for validated timelines.
    sign = "-" if seconds < 0 and not srt else ""
    ms = max(0, round((abs(seconds) if sign else seconds) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{sign}{h:02}:{m:02}:{s:02}{',' if srt else '.'}{ms:03}"


def review_flags(segments: list[dict]) -> list[dict]:
    """Heuristics indicate listening targets, never calibrated error probabilities."""
    flags = []
    previous = None
    for seg in segments:
        normalized = " ".join(tokens(seg["text"]))
        reasons = []
        if previous and normalized and (seg["source_id"], normalized) == previous:
            reasons.append("identical_adjacent_segment")
        words = tokens(seg["text"])
        if len(words) >= 12 and len(set(words)) / len(words) < 0.3:
            reasons.append("high_internal_repetition")
        if seg["end_seconds"] <= seg["start_seconds"]:
            reasons.append("zero_length_timestamp")
        if re.search(r"\[(?:blank_audio|music|silence)\]|\(inaudible\)", seg["text"], re.I):
            reasons.append("non_speech_or_inaudible_marker")
        if reasons:
            flags.append({"source_id": seg["source_id"], "start_seconds": seg["start_seconds"],
                          "end_seconds": seg["end_seconds"], "reasons": reasons})
        previous = (seg["source_id"], normalized)
    return flags
