"""Compile verified raw transcripts locally, without joining audio or using an LLM."""
from __future__ import annotations

import copy
import math
import os
from pathlib import Path
import re
import unicodedata

from .audio import inspect_audio
from .media import inspect_media, MediaError
from .engine import (digest, load_runtime, model_identity, new_id, now, run_session,
                     transform_identity, verify_completed_run, verify_review_run,
                     validate_run_timeline, validate_run_quality)
from .evaluation import timestamp
from .quality import assess_quality
from .library import (export_directory, export_manifests, normalize_groups,
                      rebuild_index, recording_time)
from .timeline import audit_timeline, TimelineError
from .storage import (import_sources, managed_source_path, read_doc, session_lock,
                      sha256_file, validate_id, validate_session, write_json)

EXPORT_VERSION = 3


def natural_order(paths):
    """Stable locale-independent proposal; CLI --order-confirmed retains argument order."""
    def key(value):
        text = unicodedata.normalize("NFC", Path(value).name).casefold()
        parts = tuple((0, int(part)) if part.isdigit() else (1, part)
                      for part in re.split(r"([0-9]+)", text) if part)
        return parts, str(value)
    return sorted(paths, key=key)


def markdown_text(text):
    # Escape formatting/HTML syntax only; no ASR-word editing or reconstruction.
    return re.sub(r"([\\`*_{}\[\]<>#!|])", r"\\\1", text)


def effective_settings(resolved):
    return {"asr": resolved["asr"], "preprocessing": resolved["preprocessing"],
            "glossary_sha256": resolved["glossary"]["sha256"]}


def matching_sessions(data_root, source_hash):
    root = Path(data_root)
    paths = sorted((root / "sessions").glob("*/session.yaml"))
    paths += sorted((root / "archive").glob("[0-9][0-9][0-9][0-9]/*/session.yaml"))
    for path in paths:
        if path.parent.name.startswith("."):
            continue
        try:
            record = read_doc(path)
        except (OSError, ValueError):
            continue
        if any(isinstance(s, dict) and s.get("sha256") == source_hash for s in record.get("sources", [])):
            yield path.parent


def read_source_result(session_path, source, run_path, info, *, reused):
    provisional = verify_review_run(run_path)
    if not provisional and not verify_completed_run(run_path):
        raise ValueError("Completed transcript integrity check failed; prior files were preserved.")
    try:
        timing_valid = validate_run_timeline(run_path)
    except TimelineError:
        if not provisional:
            raise
        timing_valid = False
    if not provisional and not timing_valid:
        raise ValueError("Cached presentation needs regeneration from its verified decoder result.")
    manifest = read_doc(run_path / "manifest.json")
    validate_id(manifest["run_id"], "run ID")
    if manifest["run_id"] != run_path.name or manifest["session_id"] != session_path.name:
        raise ValueError("Transcript provenance does not match its managed location.")
    document = read_doc(run_path / "transcript.json")
    coverage = [c for c in document["source_map"] if c["source_id"] == source["id"]]
    if (len(coverage) != 1 or coverage[0]["source_sha256"] != info["sha256"]
            or not coverage[0].get("decoded_input_complete")
            or abs(coverage[0]["duration_seconds"] - info["duration_seconds"]) > 1 / 16000 + 1e-9):
        raise ValueError("Source coverage does not match the selected recording.")
    segments = [s for s in document["segments"] if s["source_id"] == source["id"]]
    for s in segments:
        a, b = s["start_seconds"], s["end_seconds"]
        if (not isinstance(s["text"], str) or any(type(n) not in (int, float) or not math.isfinite(n) for n in (a, b))
                or (not provisional and not 0 <= a <= b <= info["duration_seconds"] + 0.05)):
            raise ValueError("Invalid source-relative segment in stored transcript.")
    # Reassess every returned source under current rules. A historic "completed"
    # label or an older quality policy must not make known-bad text acceptable.
    audit = audit_timeline(read_doc(run_path / "logs" / source["id"] / "native.json"),
                           frames=info["complete_frames"], rate=info["sample_rate"])
    resolved = manifest.get("resolved_config", {})
    quality = assess_quality(segments, [{"source_id": source["id"], "audit": audit}],
                             asr=resolved.get("asr"), glossary=resolved.get("glossary"))
    state = "review_required" if provisional or quality["status"] == "review_required" else "completed"
    explicit_time = source.get("recorded_at")
    if explicit_time is None:
        session = read_doc(session_path / "session.yaml")
        explicit_time = session.get("recorded_at") if len(session.get("sources", [])) == 1 else None
    return {"session_id": session_path.name, "source_id": source["id"], "run_id": run_path.name,
            "state": state, "quality": quality,
            "recording_time": recording_time(source["original_basename"], explicit_time),
            "run_manifest_sha256": sha256_file(run_path / "manifest.json"),
            "transcript_json_sha256": sha256_file(run_path / "transcript.json"),
            "segment_count": len(segments), "raw_segments_sha256": digest(segments),
            "timestamp_normalizations": [s["timestamp_normalization"] for s in segments if s.get("timestamp_normalization")],
            "review_relative_to_session": f"transcript/{run_path.name}/review.md",
            "reused_transcript": reused}, segments


def reject_preserved_invalid_timeline(session_path, source, run_path, info, resolved, runtime, model, manifest):
    """Reuse a negative validation result, never promote an unverified failed run.

    Older decoder attempts may have written native JSON before validation failed,
    without a completion receipt. Matching provenance plus a reproducible timing
    violation is enough to retain failure; it is NOT proof of successful decoding
    or historical output integrity. No old file is changed and no ASR is run.
    """
    logs = run_path / "logs" / source["id"]
    if not (logs / "native.json").is_file() or not (logs / "invocation.json").is_file():
        return
    invocation = read_doc(logs / "invocation.json")
    for transform_path in (session_path / "derived").glob("*/transform.json"):
        transform = read_doc(transform_path)
        wav = transform_path.with_name("audio.wav")
        if (transform["source"]["sha256"] != info["sha256"]
                or transform["output_sha256"] not in manifest["derivatives"]
                or transform["policy"] != resolved["preprocessing"]):
            continue
        asr = resolved["asr"]
        expected_args = [runtime["runtime"]["cli"], "-m", model["path"], "-f", str(wav),
                         "-l", asr["language"], "-t", str(asr["threads"]), "-bs", str(asr["beam_size"]),
                         "-tp", str(asr["temperature"]), "-tpi", str(asr["temperature_increment"]),
                         "-mc", str(asr.get("max_context", 0)), "-ojf", "-of", str(logs / "native")]
        import platform
        if platform.machine() != "arm64":
            expected_args.append("-ng")
        if resolved["glossary"].get("terms"):
            expected_args.extend(["--prompt", ", ".join(resolved["glossary"]["terms"])])
        if (invocation.get("args") != expected_args
                or invocation.get("input_sha256") != transform["output_sha256"]
                or invocation.get("model_sha256") != model["sha256"]
                or sha256_file(wav) != transform["output_sha256"]):
            continue
        raw = logs / "native.json"
        audit = audit_timeline(read_doc(raw), frames=info["complete_frames"], rate=info["sample_rate"])
        if not audit["valid"]:
            raise TimelineError(audit, {"session_id": session_path.name, "source_id": source["id"],
                                       "run_id": run_path.name, "raw_native_relative_to_session": str(raw.relative_to(session_path)),
                                       "raw_native_sha256": sha256_file(raw), "run_manifest_sha256": sha256_file(run_path / "manifest.json"),
                                       "reused_raw_for_rejection_only": True,
                                       "completion_receipt_present": (logs / "complete.json").exists()})


def resolve_source(settings, path, info, resolved, runtime, model, *, on_processing=None, retry_failed=False, on_progress=None):
    """Reuse one source from any intact run, including a multi-source session."""
    data_root = settings["roots"]["data"]
    existing = list(matching_sessions(data_root, info["sha256"]))
    failed_candidates = []
    for session_path in existing:
        with session_lock(session_path):
            session = validate_session(session_path)
            source = next(s for s in session["sources"] if s["sha256"] == info["sha256"])
            if sha256_file(managed_source_path(session_path, source)) != info["sha256"]:
                raise ValueError("Managed original failed its hash check; no replacement was made.")
            for mpath in sorted((session_path / "transcript").glob("*/manifest.json"), reverse=True):
                m = read_doc(mpath)
                if info.get("media_decode") and m.get("state") in {"completed", "review_required"}:
                    diagnostics = read_doc(mpath.parent / "diagnostics.json")
                    saved_media = next((d["transform"].get("media_decode") for d in diagnostics["sources"]
                                        if d["source_id"] == source["id"]), None)
                    if not saved_media or saved_media["identity"] != info["media_decode"]["identity"]:
                        continue
                if (effective_settings(m["resolved_config"]) == effective_settings(resolved)
                        and m["model"]["sha256"] == model["sha256"]
                        and m["runtime"]["sha256"] == runtime["runtime"]["sha256"]
                        and m.get("transform_identity") == transform_identity()):
                    covered = any(c.get("source_id") == source["id"] and c.get("source_sha256") == info["sha256"]
                                  for c in m.get("coverage", []))
                    if m.get("state") == "completed" and covered:
                        if not verify_completed_run(mpath.parent):
                            raise ValueError("Completed transcript integrity check failed; prior files were preserved.")
                        if validate_run_timeline(mpath.parent):
                            candidate = read_source_result(session_path, source, mpath.parent, info, reused=True)
                            if not (retry_failed and candidate[0]["state"] == "review_required"):
                                return candidate
                    if (m.get("state") == "review_required" and not retry_failed
                            and verify_review_run(mpath.parent) and validate_run_quality(mpath.parent)):
                        return read_source_result(session_path, source, mpath.parent, info, reused=True)
                    if m.get("state") == "failed":
                        failed_candidates.append((session_path, source, mpath, m))
    # A newer failed attempt must never hide an older matching successful run.
    for session_path, source, mpath, manifest in ([] if retry_failed else failed_candidates):
        with session_lock(session_path):
            if read_doc(mpath) != manifest:
                raise ValueError("Stored attempt changed during validation; retry with a stable session.")
            reject_preserved_invalid_timeline(session_path, source, mpath.parent, info,
                                              resolved, runtime, model, manifest)
    if existing:
        # Never transcribe unselected sources merely to create an export.
        singles = [p for p in existing if len(validate_session(p)["sources"]) == 1]
        if not singles:
            raise ValueError("No matching completed result for this source in its multi-source session. Transcribe that existing session with the requested settings first, then export it.")
        session_path = singles[0]
    else:
        session_path, _, _ = import_sources(data_root, [path])
    # The original engine handles per-file gain, native decoding, checkpoints and integrity.
    if on_processing:
        on_processing()
    result = run_session(settings, session_path, resolved, **({"force": True} if retry_failed else {}),
                         **({"progress": on_progress} if on_progress else {}))
    with session_lock(session_path):
        session = validate_session(session_path)
        source = next(s for s in session["sources"] if s["sha256"] == info["sha256"])
        return read_source_result(session_path, source, Path(result["path"]), info,
                                  reused=result.get("reused", False) or result.get("reused_asr", False))


def report_lines(entries, contents, language, created_at=None, groups=None):
    completed = sum(e["state"] == "completed" for e in entries)
    review = sum(e["state"] == "review_required" for e in entries)
    failed = sum(e["state"] == "failed" for e in entries)
    duplicates = sum(e.get("duplicate_of") is not None for e in entries)
    lines = ["# Transcript Report", "",
             "**PARTIAL REPORT — one or more selected recordings failed.**" if failed else ("**REVIEW REQUIRED — provisional transcription includes flagged passages.**" if review else "**Complete transcript compilation.**"),
             "", f"Selected files: {len(entries)} · Completed: {completed} · Review required: {review} · Failed: {failed}",
             f"Created: {created_at}" if created_at else "",
             f"Language: {language} (English). Machine transcription; accuracy review pending.", "",
             "Timestamps restart at zero for each source. File order does not imply a shared lecture, speaker or continuous real-world timeline.",
             "", "All stored ASR text is retained. Existing listening-review flags remain separate from the raw transcript.", ""]
    yield from lines
    if duplicates:
        yield from [f"Identical selections: {duplicates}. Each is retained as its own numbered entry, reusing the same verified transcript without extra inference.", ""]
    group_starts = {group["indices"][0]: group for group in (groups or [])}
    for source_index, (entry, segments) in enumerate(zip(entries, contents)):
        if source_index in group_starts:
            group = group_starts[source_index]
            yield from [f"# {markdown_text(group['title'])}", "",
                        f"Recording date: {group['date'] or 'unknown'} · Grouping: {'owner confirmed' if group.get('confirmed') else 'unconfirmed compilation'}.", ""]
        yield from [f"## {entry['position']}. {markdown_text(entry['filename'])}", ""]
        clock = entry.get("recording_time", recording_time(entry["filename"]))
        yield from [f"Recording clock: {clock['recorded_at'] or 'unknown'} · Evidence: {clock['provenance']} · Timezone: {clock['timezone'] or 'unknown'}.", ""]
        duration = entry.get("duration_seconds")
        yield from [f"Measured duration: {timestamp(duration)} ({duration:.6f} seconds)." if duration is not None else "Measured duration: unavailable.", ""]
        if entry["state"] == "failed":
            yield from ["**FAILED — no transcript is included for this selected source.**", "",
                      markdown_text(entry["failure_message"]), ""]
            continue
        if entry["state"] == "review_required":
            quality = entry.get("transcript", {}).get("quality", {})
            reasons = quality.get("reasons", [])
            yield from ["**REVIEW REQUIRED — provisional text is shown below; flagged timestamps or repeated passages must be checked against the audio.**", "",
                        "Review signals: " + ", ".join(markdown_text(reason.replace("_", " ")) for reason in reasons) + ".", ""]
            for finding in quality.get("findings", []):
                message = finding.get("message") or finding['category'].replace('_', ' ')
                yield f"- Review [{timestamp(finding['start_seconds'])} – {timestamp(finding['end_seconds'])}]: {markdown_text(message)}."
            yield ""
        if entry.get("duplicate_of") is not None:
            yield from [f"Identical audio to selection {entry['duplicate_of']}; its complete transcript is intentionally repeated here.", ""]
        if entry.get("transcript", {}).get("timestamp_normalizations"):
            yield from ["Timestamp warning: the final decoder tick enclosing the measured source end was normalized to the exact source boundary. Raw timestamps and the reason are preserved in provenance; text is unchanged.", ""]
        if not segments:
            yield from ["The completed ASR run returned no text segments.", ""]
        for segment in segments:
            yield from [f"[{timestamp(segment['start_seconds'])} – {timestamp(segment['end_seconds'])}] {markdown_text(segment['text'])}", ""]
def report_text(entries, contents, language):
    """Convenience for small callers/tests; publication streams lines to disk."""
    return "\n".join(report_lines(entries, contents, language))


def build_report(settings, paths, resolved, *, progress=None, events=None, retry_failed=False, groups=None):
    if not settings.get("storage_approved"):
        raise ValueError("Data storage approval is required before importing or exporting recordings.")
    if not paths:
        raise ValueError("Select at least one recording.")
    grouping = normalize_groups([{"filename": Path(p).name} for p in paths], groups)
    runtime = load_runtime(settings)
    model = model_identity(runtime, resolved["asr"]["model"])
    entries, contents, seen = [], [], {}
    for position, value in enumerate(paths, 1):
        path = Path(value).expanduser().absolute()
        entry = {"position": position, "filename": path.name, "selected_path": str(path),
                 "state": "failed", "sha256": None, "duration_seconds": None, "duplicate_of": None,
                 "recording_time": recording_time(path.name)}
        segments = []
        if progress:
            progress(f"{position} of {len(paths)}: {path.name} — checking")
        def event(state, **extra):
            if events:
                events({"type": "file", "index": position - 1, "total": len(paths), "state": state, **extra})
        event("checking")
        try:
            info = inspect_media(settings, path)
            entry.update(sha256=info["sha256"], size_bytes=info["size_bytes"], duration_seconds=info["duration_seconds"])
            if info.get("media_decode"):
                entry["media_decode"] = info["media_decode"]
            if info["sha256"] in seen:
                earlier = seen[info["sha256"]]
                result = copy.deepcopy(entries[earlier]["transcript"])
                result["reused_transcript"] = True
                segments = contents[earlier]
                entry["duplicate_of"] = earlier + 1
            else:
                result, segments = resolve_source(settings, path, info, resolved, runtime, model,
                                                  on_processing=lambda: event("processing"), retry_failed=retry_failed,
                                                  on_progress=(lambda detail: events({"type": "progress", "index": position - 1,
                                                                                     "total": len(paths), **detail})) if events else None)
                seen[info["sha256"]] = position - 1
            # Detect mutable external selection even when reusing a managed transcript.
            if sha256_file(path) != info["sha256"]:
                raise ValueError("Selected file changed during processing; retry after recording/copying finishes.")
            entry.update(state=result.get("state", "completed"), transcript=result)
            if result.get("recording_time", {}).get("provenance") != "filename":
                entry["recording_time"] = result.get("recording_time", entry["recording_time"])
        except (OSError, ValueError, RuntimeError, KeyError) as error:
            # No decoder text or arbitrary exception payload reaches the report/stdout.
            entry.update(state="failed", failure_category=type(error).__name__,
                         failure_message="This file could not be validated or transcribed. Check that the recording is intact, then retry. Originals and completed results were preserved.")
            if isinstance(error, MediaError):
                entry["failure_message"] = str(error)
            elif isinstance(error, FileNotFoundError):
                entry["failure_message"] = "File is missing or unavailable. Reconnect its drive or choose it again."
            if isinstance(error, TimelineError):
                entry.update(failure_message=str(error), timeline_audit=error.audit,
                             preserved_decoder=error.provenance)
            segments = []
            seen.pop(entry.get("sha256"), None)
        entries.append(entry)
        contents.append(segments)
        event(entry["state"], message=entry.get("failure_message"),
              quality_status=entry.get("transcript", {}).get("quality", {}).get("status", "unknown"),
              reused=entry.get("transcript", {}).get("reused_transcript", False))
        if progress:
            suffix = "failed (will be listed in partial report)" if entry["state"] == "failed" else (
                "identical selection retained; cached" if entry.get("duplicate_of") else
                "cached" if entry["transcript"]["reused_transcript"] else "transcribed")
            progress(f"{position} of {len(paths)}: {path.name} — {suffix}")
    grouping = normalize_groups(entries, groups)
    stable_entries = copy.deepcopy(entries)
    for entry in stable_entries:
        if "transcript" in entry:
            entry["transcript"].pop("reused_transcript", None)
    fingerprint = digest({"version": EXPORT_VERSION, "entries": stable_entries,
                          "effective_settings": effective_settings(resolved), "groups": grouping})
    exports = Path(settings["roots"]["data"]) / "exports"
    exports.mkdir(parents=True, exist_ok=True)
    completed = sum(e["state"] == "completed" for e in entries)
    review = sum(e["state"] == "review_required" for e in entries)
    failed = sum(e["state"] == "failed" for e in entries)
    status = "partial" if failed else ("review_required" if review else "completed")
    quality = {"status": "incomplete" if failed else ("review_required" if review else "passed_checks"),
               "accuracy_verified": False, "reasons": (["missing_transcripts"] if failed else []) + (["flagged_transcripts"] if review else [])}
    reused_count = sum(e.get("transcript", {}).get("reused_transcript", False) for e in entries)
    for mpath in export_manifests(settings["roots"]["data"]):
        try:
            old = read_doc(mpath)
        except (OSError, ValueError):
            # An unrelated damaged export is preserved, not a reason to lose this report.
            continue
        report = mpath.parent / "transcript-report.md"
        if (old.get("fingerprint") == fingerprint and report.is_file()
                and sha256_file(report) == old.get("report_sha256")):
            return {"state": status, "report": str(report), "batch_id": mpath.parent.name,
                    "selected": len(entries), "completed": completed, "failed": failed,
                    "review_required": review, "quality": quality, "groups": grouping,
                    "reused_transcripts": reused_count, "reused_report": True}
    batch_id = new_id("batch")
    destination = export_directory(settings["roots"]["data"], grouping)
    destination.mkdir(parents=True, exist_ok=True)
    staging, final = destination / ("." + batch_id + ".pending"), destination / batch_id
    staging.mkdir(mode=0o700)
    report = staging / "transcript-report.md"
    created_at = now()
    with report.open("x", encoding="utf-8") as handle:
        for index, line in enumerate(report_lines(entries, contents, resolved["asr"]["language"], created_at, grouping)):
            if index:
                handle.write("\n")
            handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    write_json(staging / "manifest.json", {
        "schema_version": EXPORT_VERSION, "batch_id": batch_id, "fingerprint": fingerprint,
        "created_at": created_at, "state": status, "selected": len(entries), "completed": completed,
        "failed": failed, "language": resolved["asr"]["language"], "accuracy_acceptance": "pending",
        "review_required": review, "quality": quality, "groups": grouping,
        "ordered_sources": entries, "resolved_config": resolved,
        "model_sha256": model["sha256"], "runtime_sha256": runtime["runtime"]["sha256"],
        "duplicate_policy": "Retain all selected entries; reuse the same transcript for identical audio.",
        "timestamp_basis": "source-relative; restart for every numbered source",
        "text_policy": "All stored ASR segments, in order; Markdown escaping only; no LLM or text editing.",
        "report_sha256": sha256_file(report)})
    os.rename(staging, final)
    descriptor = os.open(destination, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    rebuild_index(settings)
    return {"state": status, "report": str(final / report.name), "batch_id": batch_id,
            "selected": len(entries), "completed": completed, "failed": failed,
            "review_required": review, "quality": quality, "groups": grouping,
            "reused_transcripts": reused_count, "reused_report": False}
