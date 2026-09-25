"""Exact source-timeline validation; decoder window padding is not recorded audio.

whisper.cpp v1.9.4 CLI offsets are integer milliseconds (internal 10 ms ticks;
timestamp tokens advance in 20 ms steps). Predicted timestamps are not a
guarantee of word alignment. Only the first 10 ms tick enclosing exact source
EOF can represent the final endpoint; no other overshoot is accepted. There is
no total-duration limit here. This representation rule is not accuracy approval.
"""
from fractions import Fraction

TIMELINE_VERSION = "source-frames-eof-tick-v1"
DECODER_TICK_MS = 10  # pinned v1.9.4 CLI: offsets = internal int64 t * 10


class TimelineError(ValueError):
    def __init__(self, audit, provenance=None):
        self.audit = audit
        self.provenance = provenance or {}
        first = audit["violations"][0]
        detail = first['reason'].replace('_', ' ')
        if first['reason'] == 'end_after_eof':
            detail = (f"ends at {first['end_ms']/1000:.3f} seconds, beyond the measured source end "
                      f"by {float(Fraction(first['end_minus_eof_exact_seconds'])):.6f} seconds")
        super().__init__(f"Stored decoder timeline requires review: segment {first['segment']} "
                         f"{detail}. Raw output was preserved. This source has no validated transcript.")


def duration_fraction(duration=None, *, frames=None, rate=None):
    if frames is not None or rate is not None:
        if type(frames) is not int or type(rate) is not int or frames <= 0 or rate <= 0:
            raise ValueError("A timeline requires positive integer frame count and sample rate.")
        return Fraction(frames, rate)
    # Compatibility for callers providing seconds; production whole-file paths
    # supply the actual frame count and rate, avoiding float comparison entirely.
    if isinstance(duration, bool):
        raise ValueError("Invalid timeline duration.")
    value = Fraction(str(duration))
    if value <= 0:
        raise ValueError("Invalid timeline duration.")
    return value


def audit_timeline(native, duration=None, *, frames=None, rate=None):
    end = duration_fraction(duration, frames=frames, rate=rate)
    if not isinstance(native.get("transcription"), list):
        raise ValueError("Decoder JSON lacks a transcription array.")
    violations, normalizations = [], []
    # The sole permitted endpoint representation is the immediately enclosing
    # decoder tick, not an arbitrary time margin. An exact tick-aligned EOF has
    # no allowance; intermediate segments and starts never receive this rule.
    tick = Fraction(DECODER_TICK_MS, 1000)
    eof_ticks = end / tick
    upper_tick = (eof_ticks.numerator + eof_ticks.denominator - 1) // eof_ticks.denominator
    previous_start = previous_end = None
    for index, segment in enumerate(native["transcription"], 1):
        offsets = segment.get("offsets", {})
        a, b = offsets.get("from"), offsets.get("to")
        if type(a) is not int or type(b) is not int or not isinstance(segment.get("text"), str):
            raise ValueError("Expected integer millisecond offsets and string ASR text.")
        start, finish = Fraction(a, 1000), Fraction(b, 1000)
        reasons = []
        if start < 0: reasons.append("negative_start")
        if finish < start: reasons.append("end_before_start")
        if previous_start is not None and start < previous_start: reasons.append("nonmonotonic_start")
        if previous_end is not None and finish < previous_end: reasons.append("nonmonotonic_end")
        if start > end: reasons.append("start_after_eof")
        if finish > end:
            if (index == len(native["transcription"]) and 0 <= start < end
                    and b % DECODER_TICK_MS == 0 and finish == upper_tick * tick):
                normalizations.append({"segment": index, "raw_end_ms": b,
                                       "normalized_end_exact_seconds": str(end),
                                       "reason": "final_end_is_first_decoder_tick_enclosing_source_eof",
                                       "decoder_tick_ms": DECODER_TICK_MS,
                                       "source_duration_basis": "complete frames / sample rate" if frames is not None else "supplied seconds",
                                       "version": TIMELINE_VERSION})
            else:
                reasons.append("end_after_eof")
        for reason in reasons:
            violations.append({"segment": index, "reason": reason, "start_ms": a, "end_ms": b,
                               "start_minus_eof_exact_seconds": str(start - end),
                               "end_minus_eof_exact_seconds": str(finish - end)})
        previous_start, previous_end = start, finish
    return {"version": TIMELINE_VERSION, "duration_exact_seconds": str(end),
            "duration_basis": "complete source frames / source sample rate" if frames is not None else "supplied seconds",
            "frames": frames, "sample_rate": rate, "raw_timestamp_unit": "integer milliseconds",
            "segment_count": len(native["transcription"]), "violations": violations,
            "normalizations": normalizations, "valid": not violations}


def require_timeline(native, duration=None, *, frames=None, rate=None):
    audit = audit_timeline(native, duration, frames=frames, rate=rate)
    if not audit["valid"]:
        raise TimelineError(audit)
    return audit
