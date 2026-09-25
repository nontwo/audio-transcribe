#!/usr/bin/env python3
"""Read-only artifact validation; stdout and reports never contain transcript text.

No inference, transforms, source writes, or run-directory writes are performed.
An optional report is created exclusively at an explicitly supplied path.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import wave

import numpy as np
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audio_transcribe.audio import inspect_audio  # noqa: E402


class ValidationFailure(Exception):
    pass


checks = 0
verified_models = set()


def require(condition, code):
    global checks
    checks += 1
    if not condition:
        raise ValidationFailure(code)


def document(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for data in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            value.update(data)
    return value.hexdigest()


def contained(base, name):
    result = (base / name).resolve()
    require(not Path(name).is_absolute() and result.is_relative_to(base.resolve()), "artifact_path_outside_root")
    return result


def hash_set(root, manifest):
    entries = manifest["output_hashes"]
    require(bool(entries), "missing_artifact_hashes")
    for name, expected in entries.items():
        path = contained(root, name)
        require(path.is_file() and sha(path) == expected, "artifact_hash_mismatch")
    return len(entries)


def stamp(seconds, separator="."):
    millis = int(round(seconds * 1000))
    hours, rest = divmod(millis, 3600000)
    minutes, rest = divmod(rest, 60000)
    sec, ms = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{sec:02d}{separator}{ms:03d}"


def same(a, b, tolerance=1e-9):
    return abs(a - b) <= tolerance


def pcm(path):
    with wave.open(str(path), "rb") as wav:
        require((wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000), "invalid_working_wav_format")
        count = wav.getnframes()
        values = np.frombuffer(wav.readframes(count), dtype="<i2")
    require(len(values) == count, "incomplete_working_wav")
    return values


def validate_model(model):
    identity = model["sha256"]
    if identity not in verified_models:
        require(sha(Path(model["path"])) == identity, "model_file_hash_mismatch")
        verified_models.add(identity)
    require(model["ggml_ftype"] == 1 and "non-quantized" in model["precision"], "model_precision_mismatch")


def validate_decode(logs, metadata, model, wav_path, segments, *, source_offset=0.0, global_offset=0.0,
                    source_frames=None, source_rate=None):
    from fractions import Fraction
    native_path = logs / "native.json"
    native = document(native_path)
    invocation = document(logs / "invocation.json")
    checkpoint = document(logs / "complete.json")
    require(checkpoint == metadata, "decoder_checkpoint_metadata_mismatch")
    require(sha(native_path) == metadata["native_sha256"], "native_json_hash_mismatch")
    require(metadata["state"] == "completed" and metadata["returncode"] == 0, "decoder_not_successful")
    require(metadata["native_timestamp_unit"] == "milliseconds", "unexpected_native_timestamp_unit")
    require(metadata["input_frames"] == metadata["cli_reported_input_samples"], "cli_reported_sample_count_mismatch")
    require(metadata["sample_rate"] == 16000, "decoder_rate_mismatch")
    require(invocation["input_sha256"] == sha(wav_path), "invocation_input_hash_mismatch")
    require(invocation["model_sha256"] == model["sha256"], "invocation_model_hash_mismatch")
    args = invocation["args"]
    require(args[args.index("-m") + 1] == model["path"], "invocation_model_path_mismatch")
    require(args[args.index("-l") + 1] == "en", "decoder_language_not_english")
    require(args[args.index("-bs") + 1] == "5" and float(args[args.index("-tp") + 1]) == 0, "decoder_baseline_settings_mismatch")
    require(not ({"-tr", "--translate", "-ot", "--offset-t", "-d", "--duration", "-vad", "--vad"} & set(args)), "unexpected_decode_timeline_or_translation_flag")
    require(native["params"]["model"] == model["path"] and native["params"]["language"] == "en" and native["params"]["translate"] is False, "native_model_language_or_mode_mismatch")
    require(native["model"]["ftype"] == 1 and native["model"]["mels"] == 128, "native_model_shape_or_precision_mismatch")
    expected_layers = 4 if model.get("name", model.get("model_id")) == "large-v3-turbo" else 32
    require(native["model"]["text"]["layer"] == expected_layers, "native_decoder_layer_count_mismatch")
    require(invocation["network_isolation"] == "macOS sandbox deny network*", "network_isolation_record_missing")
    require(invocation["execution_wrapper"] == ["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)(deny network*)"], "network_isolation_wrapper_mismatch")
    # Infrastructure-only lines are examined; their content is never emitted.
    infrastructure = [line for line in (logs / "stderr.log").read_text(errors="replace").splitlines()
                      if line.startswith(("ggml_metal", "whisper_backend", "whisper_init", "system_info:"))]
    require(metadata["backend"]["metal_observed"] is True, "metal_flag_not_observed")
    require(any("GPU name:" in line for line in infrastructure)
            and any("using MTL0 backend" in line for line in infrastructure), "metal_log_evidence_missing")
    duration = metadata["input_frames"] / 16000
    require(same(metadata["input_duration_seconds"], duration), "decoder_duration_mismatch")
    require(len(native["transcription"]) == len(segments), "native_assembled_segment_count_mismatch")
    exact_end = Fraction(source_frames, source_rate) if source_frames is not None else Fraction(metadata["input_frames"], 16000)
    ticks = exact_end * 100
    enclosing_tick = Fraction((ticks.numerator + ticks.denominator - 1) // ticks.denominator, 100)
    previous = previous_end = Fraction(-1)
    clamped = 0
    for index, (item, assembled) in enumerate(zip(native["transcription"], segments)):
        offsets = item["offsets"]
        require(type(offsets["from"]) is int and type(offsets["to"]) is int, "native_timestamp_units_not_integer_ms")
        a, b = Fraction(offsets["from"], 1000), Fraction(offsets["to"], 1000)
        final_tick = (index == len(segments) - 1 and a < exact_end < b == enclosing_tick
                      and offsets["to"] % 10 == 0)
        require(0 <= a <= b and a <= exact_end and (b <= exact_end or final_tick), "native_timestamp_out_of_bounds")
        require(a >= previous and b >= previous_end, "native_timestamp_not_monotonic")
        previous, previous_end = a, b
        start, end = float(a), float(exact_end if final_tick else b)
        require(assembled["text"] == item["text"], "assembled_text_differs_from_native")
        require(same(assembled["start_seconds"], start + source_offset)
                and same(assembled["end_seconds"], end + source_offset), "source_timestamp_mapping_mismatch")
        require(same(assembled["global_start_seconds"], start + global_offset)
                and same(assembled["global_end_seconds"], end + global_offset), "global_timestamp_mapping_mismatch")
        require(assembled["native_start_milliseconds"] == item["offsets"]["from"]
                and assembled["native_end_milliseconds"] == item["offsets"]["to"], "native_timestamp_preservation_mismatch")
        require(assembled["endpoint_rounding_clamped"] == final_tick, "endpoint_rounding_record_mismatch")
        if final_tick:
            reason = assembled.get("timestamp_normalization", {})
            require(reason.get("normalized_end_exact_seconds") == str(exact_end)
                    and reason.get("raw_end_ms") == offsets["to"], "normalization_provenance_missing")
        clamped += bool(assembled["endpoint_rounding_clamped"])
    return {"segments": len(segments), "clamped_endpoints": clamped,
            "metal_verified": True, "native_text_preserved": True,
            "model": model.get("name", model.get("model_id")), "input_frames": metadata["input_frames"],
            "elapsed_seconds": metadata["elapsed_seconds"], "real_time_factor": metadata["real_time_factor"]}


def validate(args):
    session_path = args.session.expanduser().resolve()
    run_path = contained(session_path / "transcript", args.run)
    session = yaml.safe_load((session_path / "session.yaml").read_text())
    manifest = document(run_path / "manifest.json")
    diagnostics = document(run_path / "diagnostics.json")
    assembled = document(run_path / "transcript.json")
    current = document(session_path / "transcript/current.json")
    required = {"transcript.txt", "transcript.md", "transcript.srt", "transcript.json",
                "resolved-config.yaml", "manifest.json", "diagnostics.json", "review.md"}
    require(all((run_path / name).is_file() for name in required), "missing_required_run_artifact")
    require(manifest["state"] == "completed", "run_not_completed")
    artifact_count = hash_set(run_path, manifest)
    require(manifest["reference_status"] == "not_provided" and manifest["WER"] is None, "unexpected_accuracy_score")
    require(manifest["accuracy_acceptance"] == "pending", "unexpected_accuracy_acceptance")
    require(current["latest_successful_run_id"] == args.run, "current_successful_run_mismatch")
    if args.require_unaccepted:
        require(current["accepted_run_id"] is None, "run_unexpectedly_accepted")
    require(assembled["timestamp_unit"] == "seconds", "assembled_timestamp_unit_mismatch")
    require(assembled["source_map"] == manifest["coverage"] == diagnostics["source_map"], "coverage_snapshot_mismatch")
    require(assembled["segments"] and len(assembled["segments"]) == manifest["segment_count"] == diagnostics["segment_count"], "assembled_segment_count_mismatch")
    model = manifest["model"]
    validate_model(model)
    require(sha(Path(manifest["runtime"]["cli"])) == manifest["runtime"]["sha256"], "runtime_hash_mismatch")
    sources = {source["id"]: source for source in session["sources"]}
    report_sources = []
    offset = 0.0
    for coverage, entry in zip(manifest["coverage"], diagnostics["sources"], strict=True):
        source = sources[coverage["source_id"]]
        managed = contained(session_path, source["path"])
        measured = inspect_audio(managed)
        transform, decoder = entry["transform"], entry["decoder"]
        require(measured["sha256"] == source["sha256"] == coverage["source_sha256"] == transform["source"]["sha256"], "managed_source_hash_mismatch")
        if args.expected_source_sha:
            require(measured["sha256"] == args.expected_source_sha, "expected_source_hash_mismatch")
        require(coverage["input_frame_start"] == 0 and coverage["input_frame_end"] == measured["complete_frames"], "incomplete_source_frame_coverage")
        require(transform["input_frame_start"] == 0 and transform["input_frame_end"] == measured["complete_frames"], "incomplete_transform_frame_coverage")
        if args.expected_source_frames is not None:
            require(measured["complete_frames"] == args.expected_source_frames, "expected_source_frame_count_mismatch")
        wav = contained(session_path, coverage["derived_path"])
        require(sha(wav) == transform["output_sha256"], "full_derivative_hash_mismatch")
        values = pcm(wav)
        require(len(values) == transform["output_frames"] == decoder["input_frames"] == coverage["submitted_pcm_frames"], "full_pcm_frame_count_mismatch")
        if args.expected_pcm_frames is not None:
            require(len(values) == args.expected_pcm_frames, "expected_pcm_frame_count_mismatch")
        require(abs(len(values) / 16000 - measured["duration_seconds"]) <= 1 / 16000, "full_duration_not_preserved")
        require(same(coverage["duration_seconds"], len(values) / 16000) and same(coverage["global_offset_seconds"], offset), "full_source_map_timing_mismatch")
        require(coverage["decoded_input_complete"] is True and coverage["process_exit_code"] == 0, "coverage_not_successful")
        require(transform["derivative_only_discarded_orphan_bytes"] == measured["orphan_bytes"], "orphan_repair_record_mismatch")
        require(transform["policy"]["candidate"] == "A" and transform["limiter"]["enabled"] is False, "full_run_not_minimal_candidate_A")
        peak = np.abs(values.astype(np.int32)).max() / 32768
        require(peak <= 10 ** (transform["policy"]["peak_dbfs"] / 20), "PCM_peak_ceiling_exceeded")
        segments = [segment for segment in assembled["segments"] if segment["source_id"] == source["id"]]
        decode_result = validate_decode(run_path / "logs" / source["id"], decoder, model, wav, segments, global_offset=offset,
                                        source_frames=measured["complete_frames"], source_rate=measured["sample_rate"])
        tail = coverage["tail"]
        tail_frame = round(tail["start_seconds"] * 16000)
        require(tail["frames"] == len(values) - tail_frame and same(tail["duration_seconds"], tail["frames"] / 16000), "tail_frame_diagnostics_mismatch")
        tail_values = values[tail_frame:].astype(float) / 32768
        tail_peak = float(np.max(np.abs(tail_values))) if len(tail_values) else 0.0
        tail_rms = float(np.sqrt(np.mean(tail_values ** 2))) if len(tail_values) else 0.0
        for key, amplitude in (("sample_peak_dbfs", tail_peak), ("rms_dbfs", tail_rms)):
            require(tail[key] is None if amplitude == 0 else same(tail[key], 20 * math.log10(amplitude)), "tail_level_diagnostics_mismatch")
        report_sources.append({"source_sha256": measured["sha256"], "complete_source_frames": measured["complete_frames"],
                               "source_sample_rate": measured["sample_rate"], "duration_seconds": measured["duration_seconds"],
                               "orphan_bytes": measured["orphan_bytes"], "managed_source_unchanged": True,
                               "gain_db": transform["gain_db"], "output_peak_dbfs": transform["output_peak_dbfs"],
                               "tail_frames": tail["frames"], **decode_result})
        offset += len(values) / 16000
    segments = assembled["segments"]
    expected_text = "\n".join(segment["text"] for segment in segments) + "\n"
    require((run_path / "transcript.txt").read_text() == expected_text, "plain_text_artifact_mismatch")
    expected_md = "# Raw English transcription\n\nUnedited ASR; accuracy acceptance pending. Times are source-relative.\n\n"
    for source in manifest["coverage"]:
        expected_md += f"## Source {source['source_id']}\n\n"
        for segment in segments:
            if segment["source_id"] == source["source_id"]:
                expected_md += f"[{stamp(segment['start_seconds'])} – {stamp(segment['end_seconds'])}] {segment['text']}\n\n"
    require((run_path / "transcript.md").read_text() == expected_md, "markdown_text_or_timestamps_mismatch")
    expected_srt = "".join(f"{i}\n{stamp(segment['global_start_seconds'], ',')} --> {stamp(segment['global_end_seconds'], ',')}\n{segment['text'].strip()}\n\n"
                           for i, segment in enumerate(segments, 1))
    actual_srt = (run_path / "transcript.srt").read_text()
    require(actual_srt == expected_srt, "srt_text_or_timestamps_mismatch")
    blocks = actual_srt.rstrip("\n").split("\n\n")
    require(len(blocks) == len(segments), "srt_block_count_mismatch")
    for index, block in enumerate(blocks, 1):
        lines = block.splitlines()
        require(len(lines) >= 3 and lines[0] == str(index) and re.fullmatch(r"\d{2,}:\d{2}:\d{2},\d{3} --> \d{2,}:\d{2}:\d{2},\d{3}", lines[1]), "srt_syntax_invalid")
    results = []
    benchmark_hash_count = 0
    if args.benchmark:
        benchmark = args.benchmark.expanduser().resolve()
        bm = document(benchmark / "manifest.json")
        require(bm["state"] == "completed" and bm["WER"] is None and bm["reference_status"] == "not_provided", "benchmark_completion_or_accuracy_state_invalid")
        require(bm["preference"]["state"] == "provisional", "benchmark_preference_not_provisional")
        benchmark_hash_count = hash_set(benchmark, bm)
        candidates_by_clip = {}
        for item in bm["results"]:
            result_path = contained(benchmark, item["result_path"])
            result = document(result_path)
            transform, slicing, clip = result["transform"], result["slicing"], result["clip"]
            require(result["evaluation"]["WER"] is None and result["evaluation"]["reference_status"] == "not_provided", "benchmark_result_has_unmeasured_score")
            require(clip["source_sha256"] in {source["sha256"] for source in session["sources"]}, "benchmark_source_identity_mismatch")
            full_path = contained(session_path, "derived/" + transform["recipe_id"] + "/audio.wav")
            excerpt = result_path.parent / "excerpt.wav"
            require(sha(full_path) == transform["output_sha256"] == slicing["source_sha256"], "benchmark_transform_hash_mismatch")
            require(sha(excerpt) == slicing["output_sha256"], "benchmark_excerpt_hash_mismatch")
            full_samples, excerpt_samples = pcm(full_path), pcm(excerpt)
            first, last = slicing["input_frame_start"], slicing["input_frame_end"]
            require(np.array_equal(full_samples[first:last], excerpt_samples), "benchmark_excerpt_not_sample_exact")
            require(first == round(clip["start_seconds"] * 16000) and last == round(clip["end_seconds"] * 16000), "benchmark_clip_sample_bounds_mismatch")
            require(same(slicing["timestamp_offset_seconds"], first / 16000), "benchmark_source_offset_mismatch")
            require(len(excerpt_samples) == slicing["output_frames"] == result["decoder"]["input_frames"], "benchmark_excerpt_frame_count_mismatch")
            candidate = result["candidate"]
            model_name = "large-v3-turbo" if candidate.startswith("large-v3-turbo") else "large-v3"
            candidate_model = {**bm["inputs"]["models"][model_name], "name": model_name}
            validate_model(candidate_model)
            decoded = validate_decode(result_path.parent / "logs", result["decoder"], candidate_model, excerpt,
                                      result["segments"], source_offset=first / 16000)
            require(contained(benchmark, result["hypothesis_path"]).read_text() == "\n".join(s["text"] for s in result["segments"]) + "\n", "benchmark_hypothesis_text_mismatch")
            if candidate.endswith("-B"):
                limiter = transform["limiter"]
                require(limiter["automatic_makeup_gain"] is False and limiter["latency_samples"] == 0, "limiter_makeup_or_latency_invalid")
                require(limiter["limited_active_fraction"] <= .01 and limiter["significantly_limited_active_fraction"] <= .01, "limiter_activity_not_rare")
                limiter_report = {key: limiter[key] for key in ("requested_gain_db", "applied_gain_db", "limited_active_fraction", "significantly_limited_active_fraction", "max_gain_reduction_db", "limited_sample_fraction")}
            else:
                require(transform["limiter"]["enabled"] is False, "candidate_A_limiter_enabled")
                limiter_report = None
            candidates_by_clip.setdefault(clip["id"], []).append((candidate, first, last, slicing["output_sha256"]))
            results.append({"candidate": candidate, "clip_id": clip["id"], "source_start_seconds": first / 16000,
                            "source_end_seconds": last / 16000, "gain_db": transform["gain_db"],
                            "limiter": limiter_report, "WER": None, **decoded})
        for candidates in candidates_by_clip.values():
            require(len({(first, last) for _, first, last, _ in candidates}) == 1, "candidates_compare_different_intervals")
            a_hashes = {digest for name, _, _, digest in candidates if name.endswith("-A")}
            require(len(a_hashes) == 1, "model_comparison_A_audio_differs")
        require((benchmark / "review.md").is_file(), "benchmark_local_review_missing")
    return {"state": "PASS", "validation": "Read-only technical artifact validation; no inference or listening accuracy evaluation.",
            "validated_at": datetime.now(timezone.utc).isoformat(), "session_id": session["id"], "run_id": args.run,
            "run_artifact_hashes_verified": artifact_count, "benchmark_artifact_hashes_verified": benchmark_hash_count,
            "plain_text_markdown_srt_native_equivalence": True, "assertions_passed": checks,
            "sources": report_sources, "benchmark_results": results,
            "human_reference_status": "not_provided", "WER": None,
            "accepted_run_id": current["accepted_run_id"], "accuracy_acceptance": "pending",
            "limitations": ["Managed source validated; removable original was not accessed.",
                            "No audio listening or human-reference accuracy evaluation was performed.",
                            "Submitting every PCM frame and decoder success establish technical coverage, not recognition of every spoken word.",
                            "Limiter activity uses an amplitude proxy, not isolated speech identification."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True, type=Path)
    parser.add_argument("--run", required=True)
    parser.add_argument("--benchmark", type=Path)
    parser.add_argument("--expected-source-sha")
    parser.add_argument("--expected-source-frames", type=int)
    parser.add_argument("--expected-pcm-frames", type=int)
    parser.add_argument("--require-unaccepted", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        report = validate(args)
    except Exception as error:
        # Never print exception payloads that might contain user document text.
        report = {"state": "FAIL", "assertions_completed": checks,
                  "failure_code": str(error) if isinstance(error, ValidationFailure) else type(error).__name__}
    rendered = json.dumps(report, indent=2, allow_nan=False) + "\n"
    if args.report:
        with args.report.expanduser().open("x", encoding="utf-8") as output:
            output.write(rendered)
    print(rendered, end="")
    return 0 if report["state"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
