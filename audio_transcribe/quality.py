"""Conservative, source-local ASR review gates; never a measure of accuracy.

No text is removed or rewritten. The findings contain locations and numeric
evidence, so callers can show review targets without copying private speech to
logs. A clean result means only that these limited automatic checks passed.
"""
from __future__ import annotations

from collections import defaultdict
import re
import unicodedata
import zlib

QUALITY_VERSION = "source-local-repetition-review-v2"
MAX_LOOP_WORDS = 64
MIN_LOOP_WORDS = 24


def _words(text):
    return re.findall(r"\w+(?:['’]\w+)*", unicodedata.normalize("NFKC", text).casefold())


def _loop_ranges(words):
    """Find periodic word runs, including alternating sentences and split lines.

    Comparing each word to the word one period earlier is O(words * 64), not
    an unbounded search over phrase pairs. Three full cycles and 24 words are
    required. Overlapping detections of the same run are merged, not counted
    as independent evidence. This is a listening flag, not proof of a loop.
    """
    candidates = []
    for period in range(1, min(MAX_LOOP_WORDS, len(words) // 3) + 1):
        run_start = None
        for index in range(period, len(words) + 1):
            equal = index < len(words) and words[index] == words[index - period]
            if equal and run_start is None:
                run_start = index
            if not equal and run_start is not None:
                start, end = run_start - period, index
                if end - start >= max(3 * period, MIN_LOOP_WORDS):
                    candidates.append({"start": start, "end": end, "period": period})
                run_start = None
    candidates.sort(key=lambda item: (item["start"], -item["end"], item["period"]))
    merged = []
    for item in candidates:
        if merged and item["start"] < merged[-1]["end"]:
            previous = merged[-1]
            if item["end"] > previous["end"]:
                previous["end"] = item["end"]
                previous["period"] = None  # More than one overlapping pattern.
        else:
            merged.append(dict(item))
    return merged


def assess_quality(segments: list[dict], timeline_audits: list[dict] | None = None, *,
                   asr: dict | None = None, glossary: dict | None = None) -> dict:
    """Return an explicit review status without claiming calibrated confidence.

    ``timeline_audits`` entries have ``source_id`` and an ``audit`` produced by
    audit_timeline. Omitting them means timing is not assessed, not that it is
    valid. Segment text and source identity are never inferred or corrected.
    """
    findings = []
    sources = defaultdict(list)
    for segment in segments:
        sources[segment["source_id"]].append(segment)
    audited = {item["source_id"]: item["audit"] for item in (timeline_audits or [])}
    source_ids = list(dict.fromkeys([*sources, *audited]))
    source_summaries = []
    for source_id in source_ids:
        current = sources[source_id]
        words, locations = [], []
        for index, segment in enumerate(current):
            tokenized = _words(segment["text"])
            words.extend(tokenized)
            locations.extend([index] * len(tokenized))
        first_finding = len(findings)
        if (glossary or {}).get("terms") and (asr or {}).get("max_context", 0) == 0:
            findings.append({"source_id": source_id, "start_seconds": 0,
                             "end_seconds": 0, "category": "glossary_context_disabled",
                             "severity": "warning",
                             "message": "Glossary prompting is disabled by the no-context policy; check terminology against the audio."})
        if not words:
            findings.append({"source_id": source_id, "start_seconds": 0,
                             "end_seconds": 0, "category": "empty_transcript", "severity": "warning"})
        for item in _loop_ranges(words):
            first, last = locations[item["start"]], locations[item["end"] - 1]
            findings.append({"source_id": source_id, "start_seconds": current[first]["start_seconds"],
                             "end_seconds": current[last]["end_seconds"],
                             "category": "repeated_phrase_loop", "severity": "warning",
                             "repeated_word_count": item["end"] - item["start"],
                             "phrase_word_count": item["period"],
                             "segment_count": last - first + 1})
        # OpenAI Whisper uses a zlib compression-ratio threshold of 2.4 for
        # decoding fallback. Here it is only a review signal on ~30 s windows;
        # it is not numerically equivalent to the decoder's internal window.
        windows = defaultdict(list)
        for segment in current:
            windows[max(0, int(segment["start_seconds"] // 30))].append(segment)
        for window in windows.values():
            text = " ".join(segment["text"] for segment in window)
            if len(_words(text)) < 40:
                continue
            encoded = text.encode("utf-8")
            ratio = len(encoded) / len(zlib.compress(encoded))
            if ratio > 2.4:
                findings.append({"source_id": source_id, "start_seconds": window[0]["start_seconds"],
                                 "end_seconds": window[-1]["end_seconds"],
                                 "category": "high_compression_ratio", "severity": "warning",
                                 "compression_ratio": round(ratio, 4), "threshold": 2.4})
        audit = audited.get(source_id)
        if audit:
            for violation in audit["violations"]:
                findings.append({"source_id": source_id,
                                 "start_seconds": violation["start_ms"] / 1000,
                                 "end_seconds": violation["end_ms"] / 1000,
                                 "category": "invalid_timestamp", "severity": "warning",
                                 "reason": violation["reason"], "segment": violation["segment"]})
        own = findings[first_finding:]
        source_summaries.append({"source_id": source_id,
                                 "status": "review_required" if own else "passed_checks",
                                 "timestamp_valid": audit["valid"] if audit else None,
                                 "segment_count": len(current), "word_count": len(words),
                                 "finding_count": len(own)})
    if not source_ids:
        findings.append({"category": "empty_transcript", "severity": "warning",
                         "source_id": None, "start_seconds": 0, "end_seconds": 0})
    timestamp_valid = (all(item["timestamp_valid"] for item in source_summaries)
                       if source_summaries and all(item["timestamp_valid"] is not None for item in source_summaries)
                       else None)
    return {"version": QUALITY_VERSION,
            "status": "review_required" if findings else "passed_checks",
            "accuracy_verified": False, "timestamp_valid": timestamp_valid,
            "reasons": sorted({item["category"] for item in findings}),
            "findings": findings, "sources": source_summaries,
            "meaning": "Automatic review checks only. Listening or a verified reference is required to establish accuracy; raw ASR text is retained."}
