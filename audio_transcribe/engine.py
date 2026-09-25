"""One local whisper.cpp process at a time; immutable successful run artifacts."""
from __future__ import annotations

import contextlib
import copy
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import signal
import shutil
import subprocess
import sys
import time
import uuid
import wave
from datetime import datetime, timezone

from . import __version__
from .audio import inspect_audio, prepare_audio
from .evaluation import review_flags, timestamp
from .timeline import audit_timeline, require_timeline, TimelineError, TIMELINE_VERSION
from .quality import assess_quality, QUALITY_VERSION
from .progress import DecoderProgress
from .storage import (read_doc, sha256_file, write_json, write_yaml, session_lock,
                      validate_session, managed_source_path)

TRANSFORM_VERSION = "audio-transcribe-v2-1"


def transform_identity() -> dict:
    return {"version": TRANSFORM_VERSION, "implementation_sha256": sha256_file(Path(__file__).with_name("audio.py")),
            "dependencies": dependency_versions()}


def assembly_identity() -> dict:
    return {"schema_version": 2, "engine_sha256": sha256_file(Path(__file__)),
            "timeline_sha256": sha256_file(Path(__file__).with_name("timeline.py")),
            "evaluation_sha256": sha256_file(Path(__file__).with_name("evaluation.py")),
            "quality_sha256": sha256_file(Path(__file__).with_name("quality.py"))}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()


def new_id(prefix="run") -> str:
    return f"{prefix}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:12]}"


def load_runtime(settings: dict, *, verify=True) -> dict:
    path = Path(settings["roots"]["app"]) / "runtime.json"
    if not path.is_file():
        raise ValueError("Runtime is not installed. Run scripts/setup-runtime.py first.")
    runtime = read_doc(path)
    cli = Path(runtime["runtime"]["cli"])
    if not cli.is_file() or not os.access(cli, os.X_OK):
        raise ValueError("Configured whisper-cli is missing or not executable.")
    if verify and runtime["runtime"].get("sha256") != sha256_file(cli):
        raise ValueError("Runtime executable hash changed; re-verify the runtime installation.")
    return runtime


def model_identity(runtime: dict, model_name: str) -> dict:
    if model_name not in {"large-v3", "large-v3-turbo"}:
        raise ValueError("Only large-v3 and large-v3-turbo are supported.")
    model = copy.deepcopy(runtime["models"][model_name])
    path = Path(model["path"])
    if not path.is_file() or sha256_file(path) != model["sha256"]:
        raise ValueError(f"Model verification failed: {model_name}. Run setup-runtime.py to diagnose.")
    model["name"] = model_name
    return model


@contextlib.contextmanager
def inference_lock(settings: dict):
    root = Path(settings["roots"]["app"])
    root.mkdir(parents=True, exist_ok=True)
    with (root / "inference.lock").open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another AudioTranscribe inference is running. Wait for it to finish.") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def memory_snapshot() -> dict:
    result = {"at": now()}
    for name, args in [("memory_pressure", ["/usr/bin/memory_pressure", "-Q"]),
                       ("vm_stat", ["/usr/bin/vm_stat"]),
                       ("swap", ["/usr/sbin/sysctl", "vm.swapusage"])]:
        try:
            proc = subprocess.run(args, capture_output=True, text=True, timeout=5)
            result[name] = proc.stdout.strip() if proc.returncode == 0 else "unavailable"
        except (OSError, subprocess.TimeoutExpired):
            result[name] = "unavailable"
    return result


def decoder_segments(native: dict, source_id: str, duration: float, *, source_start=0.0,
                     global_offset=0.0, frames=None, rate=None, permit_review=False) -> list[dict]:
    """whisper.cpp v1.9.4 offsets are milliseconds; preserve native output separately."""
    audit = (audit_timeline if permit_review else require_timeline)(native, duration, frames=frames, rate=rate)
    # An invalid timeline remains raw and explicitly unvalidated. No blanket
    # clamp, guessed alignment, or deletion of segments is allowed.
    normalized = {item["segment"]: item for item in audit["normalizations"]} if audit["valid"] else {}
    segments = []
    for index, item in enumerate(native["transcription"], 1):
        a, b = item["offsets"]["from"], item["offsets"]["to"]
        start, end = a / 1000, b / 1000
        if index in normalized:
            from fractions import Fraction
            end = float(Fraction(normalized[index]["normalized_end_exact_seconds"]))
        segments.append({"source_id": source_id,
                         "start_seconds": start + source_start,
                         "end_seconds": end + source_start,
                         "global_start_seconds": start + global_offset,
                         "global_end_seconds": end + global_offset,
                         "native_start_milliseconds": a, "native_end_milliseconds": b,
                         "endpoint_rounding_clamped": index in normalized,
                         "timeline_validation_version": audit["version"],
                         "timestamp_valid": audit["valid"],
                         "timestamp_normalization": normalized.get(index),
                         "text": item["text"]})
    return segments


def backend_evidence(log_path: Path) -> dict:
    # Only collect infrastructure messages, never decoded content.
    markers = []
    for line in log_path.read_text(errors="replace").splitlines():
        if line.startswith(("ggml_metal", "whisper_init", "whisper_backend", "system_info:")):
            if any(term in line.lower() for term in ("metal", "gpu", "device", "backend", "system_info")):
                markers.append(line[:500])
    text = "\n".join(markers).lower()
    metal = "metal" in text and any(s in text for s in ("using device", "gpu name", "metal backend", "found device"))
    return {"metal_observed": metal, "markers": markers,
            "meaning": "Runtime backend initialization evidence; not an accuracy measurement."}


def decode(settings: dict, runtime: dict, model: dict, wav_path: Path, output_dir: Path,
           asr: dict, glossary: dict, *, timeout=None, progress=None, permit_review=False) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    complete = output_dir / "complete.json"
    prefix = output_dir / "native"
    cli = runtime["runtime"]["cli"]
    args = [cli, "-m", model["path"], "-f", str(wav_path), "-l", asr["language"],
            "-t", str(asr["threads"]), "-bs", str(asr["beam_size"]),
            "-tp", str(asr["temperature"]), "-tpi", str(asr["temperature_increment"]),
            "-mc", str(asr.get("max_context", 0)),
            "-ojf", "-of", str(prefix)]
    if progress:
        # Logging only: the pinned CLI reports actual decoder seek progress.
        args.append("--print-progress")
    if platform.machine() != "arm64":
        args.append("-ng")
    if glossary.get("terms"):
        # Passed as one literal argument, never shell source. No inferred vocabulary.
        args.extend(["--prompt", ", ".join(glossary["terms"])])
    checkpoint_identity = digest({"input_sha256": sha256_file(wav_path), "model_sha256": model["sha256"],
                                  "runtime_sha256": runtime["runtime"]["sha256"],
                                  "asr": asr, "glossary": glossary})
    if complete.exists():
        cached = read_doc(complete)
        if (cached.get("checkpoint_identity") != checkpoint_identity
                or sha256_file(output_dir / "native.json") != cached["native_sha256"]):
            raise ValueError("Decoder checkpoint provenance/hash failed; use an explicit forced run.")
        return cached
    provisional = output_dir / "provisional.json"
    if permit_review and provisional.exists():
        cached = read_doc(provisional)
        if (cached.get("checkpoint_identity") != checkpoint_identity
                or sha256_file(output_dir / "native.json") != cached["native_sha256"]):
            raise ValueError("Provisional decoder provenance/hash failed; use an explicit forced run.")
        return cached
    unfinished = [p for p in output_dir.iterdir() if p.is_file()]
    if unfinished:
        previous_attempt = output_dir / "attempts" / new_id("attempt")
        previous_attempt.mkdir(parents=True)
        for p in unfinished:
            p.rename(previous_attempt / p.name)
    command = args
    network_isolation = "no_network_code_in_driver"
    if Path("/usr/bin/sandbox-exec").exists():
        command = ["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)(deny network*)"] + args
        network_isolation = "macOS sandbox deny network*"
    write_json(output_dir / "invocation.json", {
        "args": args, "execution_wrapper": command[:3] if command != args else [],
        "network_isolation": network_isolation, "input_sha256": sha256_file(wav_path),
        "model_sha256": model["sha256"], "created_at": now(),
        "temperature_fallback": f"Starts at {asr['temperature']}; increment {asr['temperature_increment']}; other thresholds retain installed defaults.",
    }, overwrite=True)
    with wave.open(str(wav_path), "rb") as wav:
        frames, rate = wav.getnframes(), wav.getframerate()
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or rate != 16000:
            raise ValueError("Decoder input must be mono 16 kHz PCM16 WAV.")
    before = memory_snapshot()
    samples = []
    started = time.monotonic()
    child = None
    if progress:
        progress({"stage": "waiting_for_engine"})
    with inference_lock(settings), (output_dir / "stdout.log").open("wb") as stdout, (output_dir / "stderr.log").open("wb") as stderr:
        try:
            child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                     start_new_session=True)
            next_sample = 0
            next_progress = 0
            decoder_progress = DecoderProgress(output_dir / "stderr.log") if progress else None
            while child.poll() is None:
                elapsed = time.monotonic() - started
                if progress and elapsed >= next_progress:
                    progress({"stage": "transcribing", "percent": decoder_progress.read()})
                    next_progress = elapsed + 2
                if timeout is not None and elapsed > timeout:
                    raise TimeoutError("Inference exceeded the configured deadline; local logs preserved.")
                if elapsed >= next_sample:
                    snap = memory_snapshot()
                    rss = subprocess.run(["/bin/ps", "-o", "rss=", "-p", str(child.pid)], capture_output=True, text=True)
                    snap["process_rss_kib"] = int(rss.stdout.strip()) if rss.stdout.strip().isdigit() else None
                    samples.append(snap)
                    next_sample = elapsed + 15
                time.sleep(0.2)
            if child.returncode:
                raise RuntimeError(f"whisper.cpp exited {child.returncode}; see local decoder logs at {output_dir}.")
        except BaseException:
            if child is not None and child.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
            raise
    elapsed = time.monotonic() - started
    if progress:
        progress({"stage": "validating"})
    native_path = prefix.with_suffix(".json")
    if not native_path.is_file():
        raise RuntimeError("Decoder did not create native JSON; inspect local logs.")
    native = read_doc(native_path)
    timeline = audit_timeline(native, frames=frames, rate=rate)
    write_json(output_dir / "timeline-audit.json", timeline)
    if not timeline["valid"] and not permit_review:
        raise TimelineError(timeline)
    # The CLI explicitly reports how many samples it loaded before inference.
    reported_samples = re.findall(r"processing .*?\((\d+) samples,", (output_dir / "stderr.log").read_text(errors="replace"))
    if not reported_samples or int(reported_samples[-1]) != frames:
        raise RuntimeError("Decoder input sample-count evidence is missing or mismatched; inspect local logs.")
    backend = backend_evidence(output_dir / "stderr.log")
    if platform.machine() == "arm64" and not backend["metal_observed"]:
        raise RuntimeError("Metal activation could not be verified from this native arm64 run's logs.")
    metadata = {"state": "completed" if timeline["valid"] else "review_required",
                "timestamp_valid": timeline["valid"], "timeline_audit": timeline,
                "checkpoint_identity": checkpoint_identity, "elapsed_seconds": elapsed,
                "real_time_factor": elapsed / (frames / rate) if frames else None,
                "input_frames": frames, "sample_rate": rate, "input_duration_seconds": frames / rate,
                "native_timestamp_unit": "milliseconds", "native_sha256": sha256_file(native_path),
                "backend": backend, "memory_before": before, "memory_during": samples,
                "memory_after": memory_snapshot(), "network_isolation": network_isolation,
                "ended_at": now(), "returncode": 0, "cli_reported_input_samples": int(reported_samples[-1])}
    write_json(complete if timeline["valid"] else provisional, metadata)
    return metadata


def derivative(session_path: Path, source: dict, policy: dict, *, settings=None) -> tuple[Path, dict]:
    source_path = managed_source_path(session_path, source)
    if sha256_file(source_path) != source["sha256"]:
        raise ValueError("Managed source hash changed; processing stopped without modifying it.")
    from .media import working_source
    if settings is None:
        from .config import load_settings
        settings = load_settings()
    working, media = working_source(settings, source_path)
    identity = {"source": source["sha256"], "policy": policy, "transform": transform_identity()}
    if media:
        identity["media_decode"] = media["identity"]
    key = digest(identity)[:24]
    target_dir = session_path / "derived" / ("recipe-" + key)
    target_dir.mkdir(parents=True, exist_ok=True)
    wav_path = target_dir / "audio.wav"
    manifest_path = target_dir / "transform.json"
    if manifest_path.exists():
        transform = read_doc(manifest_path)
        if wav_path.is_file() and sha256_file(wav_path) == transform["output_sha256"]:
            return wav_path, transform
        raise ValueError("A saved derivative failed its hash check; preserve it and use a new recipe.")
    if wav_path.exists():
        # Preserve an interrupted artifact and create a new derivative atomically.
        wav_path.rename(target_dir / ("interrupted-" + uuid.uuid4().hex + ".wav"))
    transform = prepare_audio(working, wav_path, policy=policy)
    if media:
        transform["media_decode"] = media
    transform["recipe_id"] = target_dir.name
    transform["transform_version"] = TRANSFORM_VERSION
    transform["implementation"] = transform_identity()
    write_json(manifest_path, transform)
    return wav_path, transform


def write_transcripts(run_path: Path, segments: list[dict], source_map: list[dict]):
    config_path = run_path / "resolved-config.yaml"
    resolved = read_doc(config_path) if config_path.is_file() else {"asr": {}, "glossary": {}}
    quality = assess_quality(segments, [{"source_id": item["source_id"], "audit": item["timeline_audit"]}
                                        for item in source_map if "timeline_audit" in item],
                             asr=resolved["asr"], glossary=resolved["glossary"])
    result = {"schema_version": 1, "timestamp_unit": "seconds",
              "global_time_basis": "concatenated audio time; unknown real-world gaps are not inferred",
              "source_map": source_map, "segments": segments,
              "text_status": "raw ASR, unedited and not human accepted", "quality": quality}
    write_json(run_path / "transcript.json", result)
    with (run_path / "transcript.txt").open("x", encoding="utf-8") as handle:
        for segment in segments:
            handle.write(segment["text"] + "\n")
        if not segments:
            handle.write("\n")
    by_source = {}
    for segment in segments:
        by_source.setdefault(segment["source_id"], []).append(segment)
    with (run_path / "transcript.md").open("x", encoding="utf-8") as handle:
        handle.write("# Raw English transcription\n\nUnedited ASR; accuracy acceptance pending. Times are source-relative.\n\n")
        if quality["status"] == "review_required":
            handle.write("**Review required:** " + ", ".join(quality["reasons"]) + ". All raw text is retained; automatic checks did not establish reliability.\n\n")
        if "glossary_context_disabled" in quality["reasons"]:
            handle.write("**Glossary warning:** no-context decoding disables glossary prompting in this runtime. Check terminology against the audio.\n\n")
        if quality["timestamp_valid"] is False:
            handle.write("**Unvalidated timestamps:** these are raw model predictions, including out-of-range times. The recording has no imposed length limit. No subtitle file is exported for this run.\n\n")
        for source in source_map:
            handle.write(f"## Source {source['source_id']}\n\n")
            for seg in by_source.get(source["source_id"], []):
                handle.write(f"[{timestamp(seg['start_seconds'])} – {timestamp(seg['end_seconds'])}] {seg['text']}\n\n")
    if quality["timestamp_valid"] is False:
        return
    with (run_path / "transcript.srt").open("x", encoding="utf-8") as handle:
        for i, seg in enumerate(segments, 1):
            start, end = seg["global_start_seconds"], seg["global_end_seconds"]
            handle.write(f"{i}\n{timestamp(start, srt=True)} --> {timestamp(end, srt=True)}\n{seg['text'].strip()}\n\n")


def output_hashes(path: Path) -> dict:
    return {str(p.relative_to(path)): sha256_file(p) for p in sorted(path.rglob("*"))
            if p.is_file() and p.name != "manifest.json"}


def verify_hashes(path: Path, hashes: dict) -> bool:
    base = path.resolve()
    for name, expected in hashes.items():
        item = Path(name)
        if item.is_absolute() or not (base / item).resolve().is_relative_to(base):
            return False
        if not (base / item).is_file() or sha256_file(base / item) != expected:
            return False
    return bool(hashes)


def verified_artifact(path: Path, name: str, hashes: dict) -> Path:
    base = path.resolve()
    relative = Path(name)
    item = (base / relative).resolve()
    if relative.is_absolute() or not item.is_relative_to(base) or name not in hashes:
        raise ValueError("Artifact reference must remain inside the verified output set.")
    if not item.is_file() or sha256_file(item) != hashes[name]:
        raise ValueError("Referenced artifact integrity failed.")
    return item


def verify_completed_run(path: Path) -> bool:
    try:
        manifest = read_doc(path / "manifest.json")
        required = {"transcript.txt", "transcript.md", "transcript.srt", "transcript.json",
                    "resolved-config.yaml", "diagnostics.json", "review.md"}
        hashes = manifest.get("output_hashes", {})
        return (manifest["state"] == "completed" and required <= hashes.keys()
                and verify_hashes(path, hashes))
    except (OSError, ValueError, KeyError):
        return False


def verify_review_run(path: Path) -> bool:
    """Integrity of explicitly provisional artifacts is not successful ASR."""
    try:
        manifest = read_doc(path / "manifest.json")
        required = {"transcript.txt", "transcript.md", "transcript.json",
                    "resolved-config.yaml", "diagnostics.json", "review.md"}
        hashes = manifest.get("output_hashes", {})
        return (manifest["state"] == "review_required" and required <= hashes.keys()
                and manifest.get("quality", {}).get("status") == "review_required"
                and verify_hashes(path, hashes))
    except (OSError, ValueError, KeyError):
        return False


def validate_run_quality(path: Path) -> bool:
    """Reassess saved text against the current rules before cache reuse."""
    document = read_doc(path / "transcript.json")
    audits = []
    for source in document["source_map"]:
        native = read_doc(path / "logs" / source["source_id"] / "native.json")
        audit = audit_timeline(native, frames=source["source_complete_frames"], rate=source["source_sample_rate"])
        audits.append({"source_id": source["source_id"], "audit": audit})
        expected = decoder_segments(native, source["source_id"], source["source_duration_seconds"],
                                    frames=source["source_complete_frames"], rate=source["source_sample_rate"],
                                    global_offset=source["global_offset_seconds"], permit_review=True)
        actual = [segment for segment in document["segments"] if segment["source_id"] == source["source_id"]]
        keys = ("text", "start_seconds", "end_seconds", "global_start_seconds", "global_end_seconds",
                "native_start_milliseconds", "native_end_milliseconds")
        if [tuple(s[k] for k in keys) for s in expected] != [tuple(s[k] for k in keys) for s in actual]:
            return False
    resolved = read_doc(path / "resolved-config.yaml")
    return document.get("quality") == assess_quality(document["segments"], audits,
                                                     asr=resolved["asr"], glossary=resolved["glossary"])


def validate_run_timeline(path: Path):
    """Revalidate intact cached output after a presentation-code change, without ASR."""
    document = read_doc(path / "transcript.json")
    for source in document["source_map"]:
        native = read_doc(path / "logs" / source["source_id"] / "native.json")
        expected = decoder_segments(native, source["source_id"], source["source_duration_seconds"],
                                    frames=source["source_complete_frames"], rate=source["source_sample_rate"],
                                    global_offset=source["global_offset_seconds"])
        stored = [s for s in document["segments"] if s["source_id"] == source["source_id"]]
        keys = ("text", "start_seconds", "end_seconds", "global_start_seconds", "global_end_seconds",
                "native_start_milliseconds", "native_end_milliseconds")
        if ([tuple(s[k] for k in keys) for s in expected]
                != [tuple(s[k] for k in keys) for s in stored]):
            return False
        if any(a.get("timestamp_normalization") and a["timestamp_normalization"] != b.get("timestamp_normalization")
               for a, b in zip(expected, stored)):
            return False
    return True


def reuse_verified_decoder_logs(candidates, source_id, output_dir):
    """Copy immutable verified decoder receipts for presentation-only regeneration."""
    if output_dir.exists() and any(output_dir.iterdir()):
        return any((output_dir / name).is_file() for name in ("complete.json", "provisional.json")) and (output_dir / "cache-provenance.json").is_file()
    for candidate in candidates:
        source_dir = candidate / "logs" / source_id
        if not any((source_dir / name).is_file() for name in ("complete.json", "provisional.json")):
            continue
        # Candidates have matching inference/preprocessing identities and all
        # output hashes were verified under the session lock before selection.
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        staging = output_dir.parent / (".cache-copy-" + uuid.uuid4().hex)
        staging.mkdir()
        for item in source_dir.iterdir():
            if item.is_file() and item.name != "cache-provenance.json":
                shutil.copyfile(item, staging / item.name)
        write_json(staging / "cache-provenance.json", {
            "reused_from_run_id": candidate.name, "source_id": source_id,
            "source_manifest_sha256": sha256_file(candidate / "manifest.json"),
            "raw_native_sha256": sha256_file(source_dir / "native.json"),
            "operation": "Revalidate and regenerate presentation only; no ASR."})
        # A crash during copying leaves a hidden staging directory, never a
        # truncated completion receipt in the decoder's published log directory.
        os.rename(staging, output_dir)
        return True
    return False


def finalize_pointers(session_path: Path, session: dict):
    """Recover a crash between completed artifacts and the mutable current pointer."""
    transcript_root = session_path / "transcript"
    completed = []
    for path in transcript_root.glob("*/manifest.json"):
        manifest = read_doc(path)
        if manifest.get("state") == "completed" and verify_completed_run(path.parent):
            completed.append(manifest)
    if not completed:
        return
    latest = max(completed, key=lambda m: m["completed_at"])
    current_path = transcript_root / "current.json"
    current = read_doc(current_path) if current_path.exists() else {"schema_version": 1, "accepted_run_id": None}
    current.update({"latest_successful_run_id": latest["run_id"], "updated_at": now(),
                    "accuracy_acceptance": "accepted" if current.get("accepted_run_id") == latest["run_id"] else "pending"})
    session["processing_status"] = "completed"
    session["profiles"] = latest["resolved_config"]["selected_profiles"]
    session.setdefault("runs", [])
    for item in completed:
        if item["run_id"] not in session["runs"]:
            session["runs"].append(item["run_id"])
    write_yaml(session_path / "session.yaml", session, overwrite=True)
    # Publish the pointer last: no later metadata failure may expose a failed run.
    write_json(current_path, current, overwrite=current_path.exists())


def run_session(settings: dict, session_path: Path, resolved: dict, *, force=False, progress=None) -> dict:
    if progress:
        progress({"stage": "preparing"})
    runtime = load_runtime(settings)
    model = model_identity(runtime, resolved["asr"]["model"])
    with session_lock(session_path):
        session = validate_session(session_path)
        source_by_id = {s["id"]: s for s in session["sources"]}
        sources = [source_by_id[sid] for sid in session["source_order"]]
        derivatives = [derivative(session_path, source, resolved["preprocessing"], settings=settings) for source in sources]
        identity = {"schema_version": 1, "application_version": __version__, "sources": [s["sha256"] for s in sources],
                    "derivatives": [t["output_sha256"] for _, t in derivatives],
                    "resolved_config": resolved, "model": model,
                    "runtime": runtime["runtime"], "transform_version": TRANSFORM_VERSION,
                    "transform_identity": transform_identity(), "assembly_identity": assembly_identity()}
        fingerprint = digest(identity)
        transcript_root = session_path / "transcript"
        transcript_root.mkdir(exist_ok=True)
        resume_path = None
        cached_paths = []
        if not force:
            for previous in sorted(transcript_root.glob("*/manifest.json"), reverse=True):
                old = read_doc(previous)
                # A validator/renderer edit must not trigger fresh inference of
                # already verified audio. Reuse only if every inference and
                # preprocessing identity matches and raw/presentation data pass
                # the current exact timeline rules. Old files remain unchanged.
                same_input = all(old.get(k) == v for k, v in identity.items() if k != "assembly_identity")
                if same_input and old.get("state") in {"completed", "review_required"} and old.get("fingerprint") != fingerprint:
                    verifier = verify_completed_run if old["state"] == "completed" else verify_review_run
                    if not verifier(previous.parent):
                        raise ValueError("Completed output changed; preserve it and use --force for a new run.")
                    if validate_run_quality(previous.parent):
                        if old["state"] == "completed":
                            finalize_pointers(session_path, session)
                        return {"session_id": session["id"], "run_id": old["run_id"],
                                "path": str(previous.parent), "reused": True, "state": old["state"],
                                "quality": old["quality"], "timestamp_valid": old["quality"]["timestamp_valid"]}
                    cached_paths.append(previous.parent)
                if old.get("fingerprint") == fingerprint:
                    if old.get("state") in {"completed", "review_required"}:
                        verifier = verify_completed_run if old["state"] == "completed" else verify_review_run
                        if verifier(previous.parent):
                            if not validate_run_quality(previous.parent):
                                raise ValueError("Cached presentation or quality assessment changed relative to its native output.")
                            if old["state"] == "completed":
                                finalize_pointers(session_path, session)
                            return {"session_id": session["id"], "run_id": old["run_id"],
                                    "path": str(previous.parent), "reused": True, "state": old["state"],
                                    "quality": old["quality"], "timestamp_valid": old["quality"]["timestamp_valid"]}
                        raise ValueError("Completed output changed; preserve it and use --force for a new run.")
                    if old.get("state") in {"running", "failed", "interrupted"} and not resume_path:
                        resume_path = previous.parent
        run_path = resume_path or transcript_root / new_id()
        run_path.mkdir(exist_ok=True)
        manifest_path = run_path / "manifest.json"
        if resume_path:
            manifest = read_doc(manifest_path)
            if read_doc(run_path / "resolved-config.yaml") != resolved:
                raise ValueError("Interrupted run's configuration snapshot changed; preserve it and use --force.")
            manifest.setdefault("resumed_at", []).append(now())
        else:
            write_yaml(run_path / "resolved-config.yaml", resolved)
            manifest = {**identity, "run_id": run_path.name, "session_id": session["id"],
                        "fingerprint": fingerprint, "started_at": now(), "state": "running",
                        "source_order": session["source_order"], "accuracy_acceptance": "pending",
                        "dependencies": dependency_versions(), "coverage": [],
                        "reference_status": "not_provided", "WER": None}
        manifest["state"] = "running"
        write_json(manifest_path, manifest, overwrite=manifest_path.exists())
        session["processing_status"] = "running"
        session["profiles"] = resolved["selected_profiles"]
        session.setdefault("runs", [])
        if run_path.name not in session["runs"]:
            session["runs"].append(run_path.name)
        write_yaml(session_path / "session.yaml", session, overwrite=True)
        started = time.monotonic()
        try:
            all_segments, source_map, diagnostics = [], [], []
            reused_decoders = 0
            global_offset = 0.0
            for source, (wav_path, transform) in zip(sources, derivatives):
                output_dir = run_path / "logs" / source["id"]
                reused_decoders += reuse_verified_decoder_logs(cached_paths, source["id"], output_dir)
                meta = decode(settings, runtime, model, wav_path, output_dir, resolved["asr"], resolved["glossary"],
                              permit_review=True,
                              **({"progress": progress} if progress else {}))
                duration = meta["input_duration_seconds"]
                original = transform["source"]
                expected_frames = (original["complete_frames"] * 16000 + original["sample_rate"] - 1) // original["sample_rate"]
                if (transform["input_frame_start"] != 0
                        or transform["input_frame_end"] != original["complete_frames"]
                        or meta["input_frames"] != transform["output_frames"]
                        or meta["input_frames"] != expected_frames
                        or duration != expected_frames / 16000):
                    raise RuntimeError("Complete-frame coverage/duration assertion failed; current was not updated.")
                native = read_doc(output_dir / "native.json")
                segments = decoder_segments(native, source["id"], duration, global_offset=global_offset,
                                            frames=original["complete_frames"], rate=original["sample_rate"], permit_review=True)
                timeline = audit_timeline(native, frames=original["complete_frames"], rate=original["sample_rate"])
                all_segments.extend(segments)
                coverage = {"source_id": source["id"], "source_sha256": source["sha256"],
                            "source_path": source["path"], "derived_path": str(wav_path.relative_to(session_path)),
                            "global_offset_seconds": global_offset, "duration_seconds": duration,
                            "input_frame_start": transform["input_frame_start"],
                            "input_frame_end": transform["input_frame_end"],
                            "source_complete_frames": original["complete_frames"],
                            "source_sample_rate": original["sample_rate"],
                            "source_duration_seconds": original["duration_seconds"],
                            "source_duration_basis": "complete source frames / source sample rate",
                            "timeline_validation_version": TIMELINE_VERSION,
                            "timestamp_valid": timeline["valid"], "timeline_audit": timeline,
                            "duration_tolerance_seconds": 1 / 16000,
                            "submitted_pcm_frames": meta["input_frames"],
                            "decoded_input_complete": True, "process_exit_code": 0,
                            "last_segment_end_seconds": segments[-1]["end_seconds"] if segments else None,
                            "coverage_evidence": "All derivative frames submitted in a whole-file invocation without offset/duration/VAD; decoder returned success. Segment end is not a coverage boundary.",
                            "tail": tail_metrics(wav_path, segments[-1]["end_seconds"] if segments else 0)}
                source_map.append(coverage)
                diagnostics.append({"source_id": source["id"], "transform": transform, "decoder": meta})
                global_offset += duration
            # If interruption occurred while final artifacts were written, preserve them.
            for name in ("transcript.txt", "transcript.md", "transcript.srt", "transcript.json", "diagnostics.json", "review.md"):
                existing = run_path / name
                if existing.exists():
                    recovery = run_path / "logs" / ("interrupted-finalization-" + uuid.uuid4().hex)
                    recovery.mkdir(parents=True)
                    existing.rename(recovery / name)
            write_transcripts(run_path, all_segments, source_map)
            quality = assess_quality(all_segments, [{"source_id": item["source_id"], "audit": item["timeline_audit"]}
                                                    for item in source_map],
                                     asr=resolved["asr"], glossary=resolved["glossary"])
            flags = review_flags(all_segments)
            normalizations = [dict(s["timestamp_normalization"], source_id=s["source_id"])
                              for s in all_segments if s.get("timestamp_normalization")]
            write_json(run_path / "diagnostics.json", {"sources": diagnostics, "review_flags": flags,
                                                       "quality": quality,
                                                       "timestamp_normalizations": normalizations,
                                                       "source_map": source_map, "segment_count": len(all_segments)})
            with (run_path / "review.md").open("x", encoding="utf-8") as handle:
                handle.write("# Listening review\n\nTechnical completion is not accuracy acceptance. Human reference not provided; WER is null.\n\n")
                handle.write("Raw text is retained, including repetitions. No inferred speaker, accent, course, names or formulas have been added. Decoder scores are heuristics.\n\n")
                handle.write(f"Automatic quality status: {quality['status']}. Accuracy verified: false.\n\n")
                for finding in quality["findings"]:
                    handle.write(f"- {finding['source_id']} {timestamp(finding['start_seconds'])}–{timestamp(finding['end_seconds'])}: {finding['category']}.\n")
                    if finding.get("message"):
                        handle.write(f"  {finding['message']}\n")
                if quality["timestamp_valid"] is False:
                    handle.write("\nTimestamps are raw unvalidated model predictions. No timing correction or subtitle export was applied. Exact violations are retained in diagnostics.json.\n\n")
                for item in normalizations:
                    handle.write(f"Timestamp warning: {item['source_id']} segment {item['segment']} used the first 10 ms decoder tick enclosing source EOF; its presentation end was normalized to exact source EOF. Raw milliseconds and the normalization reason remain in transcript.json. This does not establish word accuracy.\n\n")
                handle.write("Inspect the central benchmark's side-by-side comparison for model/processing disagreements. Model agreement is not ground truth.\n\n")
                if flags:
                    for flag in flags:
                        handle.write(f"- {flag['source_id']} {timestamp(flag['start_seconds'])}–{timestamp(flag['end_seconds'])}: {', '.join(flag['reasons'])}.\n")
                else:
                    handle.write("No configured repetition/marker heuristic fired. This does not establish correctness; review the audio alongside the full transcript.\n")
                handle.write("\nInspect unclear words, technical terms, quantities, negations and conditions by listening. Keep corrections in separate files with run/interval provenance.\n")
                for src in source_map:
                    handle.write(f"\nSource {src['source_id']}: all {src['submitted_pcm_frames']} derivative frames were submitted; last segment end {src['last_segment_end_seconds']}. Trailing-region measurements are in diagnostics.json and do not by themselves prove silence or omission.\n")
            elapsed = time.monotonic() - started
            result_state = "review_required" if quality["status"] == "review_required" else "completed"
            manifest.update({"state": result_state, "completed_at": now(), "coverage": source_map, "quality": quality,
                             "wall_seconds_this_attempt": elapsed,
                             "inference_elapsed_seconds": sum(d["decoder"]["elapsed_seconds"] for d in diagnostics),
                             "real_time_factor": sum(d["decoder"]["elapsed_seconds"] for d in diagnostics) / global_offset if global_offset else None,
                             "segment_count": len(all_segments), "output_hashes": output_hashes(run_path)})
            manifest["reused_decoder_count"] = reused_decoders
            write_json(manifest_path, manifest, overwrite=True)
            verifier = verify_completed_run if result_state == "completed" else verify_review_run
            if not verifier(run_path):
                raise RuntimeError("Final artifact integrity validation failed; current pointer was not changed.")
            if result_state == "completed":
                finalize_pointers(session_path, session)
            else:
                session["processing_status"] = "review_required"
                write_yaml(session_path / "session.yaml", session, overwrite=True)
            return {"session_id": session["id"], "run_id": run_path.name, "path": str(run_path),
                    "reused": False, "resumed": bool(resume_path), "state": result_state,
                    "quality": quality, "timestamp_valid": quality["timestamp_valid"],
                    "reused_asr": reused_decoders == len(sources),
                    "segment_count": len(all_segments), "inference_elapsed_seconds": manifest["inference_elapsed_seconds"]}
        except BaseException as error:
            manifest["state"] = "interrupted" if isinstance(error, (KeyboardInterrupt, SystemExit)) else "failed"
            manifest["failure_category"] = type(error).__name__
            manifest["attempt_ended_at"] = now()
            write_json(manifest_path, manifest, overwrite=True)
            session["processing_status"] = manifest["state"]
            write_yaml(session_path / "session.yaml", session, overwrite=True)
            raise


def dependency_versions() -> dict:
    import numpy, scipy, yaml
    return {"python": sys.version.split()[0], "numpy": numpy.__version__,
            "scipy": scipy.__version__, "PyYAML": yaml.__version__, "application": __version__}


def tail_metrics(path: Path, start: float) -> dict:
    import numpy as np
    with wave.open(str(path), "rb") as wav:
        frame = max(0, min(wav.getnframes(), round(start * wav.getframerate())))
        wav.setpos(frame)
        count, peak, squares = 0, 0.0, 0.0
        while block := wav.readframes(262144):
            samples = np.frombuffer(block, dtype="<i2").astype(float) / 32768
            count += len(samples)
            peak = max(peak, float(np.max(np.abs(samples))))
            squares += float(np.dot(samples, samples))
    rms = math.sqrt(squares / count) if count else 0.0
    return {"start_seconds": frame / 16000, "frames": count, "duration_seconds": count / 16000,
            "sample_peak_dbfs": 20 * math.log10(peak) if peak else None,
            "rms_dbfs": 20 * math.log10(rms) if rms else None,
            "interpretation": "Level measurements only; listen to determine whether trailing speech exists."}
