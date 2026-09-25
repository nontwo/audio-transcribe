"""Small explicit CLI and native file-picker workflow."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import signal
import shutil
import subprocess
import sys

from .audio import inspect_audio
from .media import inspect_media
from .benchmark import evaluate_reference, run_benchmark
from .config import create_profile, list_profiles, load_settings, resolve_config
from .engine import load_runtime, now, run_session, verify_completed_run
from .export import build_report, natural_order
from .library import library_request
from .storage import (archive_session, import_sources, locate_session, read_doc,
                      restore_session, session_lock, validate_id, write_json, write_yaml)


def parser():
    root = argparse.ArgumentParser(prog="audio-transcribe", description="Local session-based English transcription; no cloud inference.")
    root.add_argument("--settings", help="Machine-local JSON/YAML settings path (or AUDIO_TRANSCRIBE_SETTINGS).")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Inspect configured roots and local runtime without importing audio.")
    configure = commands.add_parser("configure", help="Persist an explicitly chosen storage root once.")
    configure.add_argument("--data-root", required=True)
    configure.add_argument("--storage-mode", choices=["local_alternative", "explicit_synced", "verified_local"], required=True)
    transcribe = commands.add_parser("transcribe", help="Import originals and transcribe, or reuse verified completed work.")
    transcribe.add_argument("files", nargs="*")
    transcribe.add_argument("--session", help="Process a managed session by stable ID.")
    transcribe.add_argument("--speaker", help="Manual profile ID; use '-' to clear a session assignment.")
    transcribe.add_argument("--capture", help="Manual profile ID; use '-' to clear a session assignment.")
    transcribe.add_argument("--glossary", help="Manual glossary ID; use '-' for empty vocabulary.")
    transcribe.add_argument("--model", choices=["large-v3", "large-v3-turbo"])
    transcribe.add_argument("--channel", choices=["mean", "left", "right"])
    transcribe.add_argument("--force", action="store_true", help="Create a new run; never replace earlier raw ASR.")
    transcribe.add_argument("--separate", action="store_true", help="Explicitly import a separate session even if bytes already exist.")
    transcribe.add_argument("--order-confirmed", action="store_true", help="Use multi-file positional argument order as explicitly confirmed audio order.")
    transcribe.add_argument("--import-only", action="store_true", help="Import for inspection/pilot without inference.")
    report = commands.add_parser("report", help="Transcribe/reuse selected files separately and compile one complete Markdown report.")
    report.add_argument("files", nargs="+")
    report.add_argument("--order-confirmed", action="store_true", help="Use the explicitly confirmed positional file order.")
    for kind in ("speaker", "capture", "glossary"):
        report.add_argument("--" + kind)
    report.add_argument("--model", choices=["large-v3", "large-v3-turbo"])
    report.add_argument("--channel", choices=["mean", "left", "right"])
    report.add_argument("--retry-failed", action="store_true", help="Retry failed inference while reusing intact successful sources.")
    app_report = commands.add_parser("app-report", help="Native app bridge: ordered request JSON in, progress JSON lines out.")
    app_report.add_argument("--request", required=True)
    app_library = commands.add_parser("app-library", help="Local report library JSON bridge; no transcription or network access.")
    app_library.add_argument("--request", required=True)
    for name in ("benchmark", "pilot"):
        bench = commands.add_parser(name, help="Run an explicit bounded comparison." if name == "benchmark" else "Measure a short primary-model pilot before full transcription.")
        bench.add_argument("session_id")
        bench.add_argument("--interval", action="append", help="SOURCE_ID:START_SECONDS:END_SECONDS; repeat for each exact interval.")
        bench.add_argument("--speaker")
        bench.add_argument("--capture")
        bench.add_argument("--glossary")
        bench.add_argument("--force", action="store_true")
    for name in ("archive", "restore", "inspect"):
        cmd = commands.add_parser(name)
        cmd.add_argument("session_id")
    accept = commands.add_parser("accept", help="Record your explicit accuracy acceptance after listening/review.")
    accept.add_argument("session_id")
    accept.add_argument("run_id")
    profiles = commands.add_parser("profile")
    profile_cmds = profiles.add_subparsers(dest="profile_command", required=True)
    for name in ("create", "list"):
        p = profile_cmds.add_parser(name)
        p.add_argument("kind", choices=["speaker", "capture", "glossary"])
        if name == "create":
            p.add_argument("id")
            p.add_argument("--label")
    reference = commands.add_parser("evaluate", help="Evaluate a supplied reference for one exact benchmark clip; unverified drafts never produce WER.")
    reference.add_argument("benchmark_id")
    reference.add_argument("evaluation_id")
    reference.add_argument("clip_id")
    reference.add_argument("reference")
    reference.add_argument("--verified", action="store_true", help="Assert that this is human-verified text for exactly this clip.")
    commands.add_parser("launch", help="Native file chooser and optional profiles; cancellation is a clean exit.")
    return root


def emit(value):
    print(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False), flush=True)


def require_storage(settings):
    if not settings["storage_approved"]:
        raise ValueError("Data storage is not approved. Check Documents/iCloud, then run configure --data-root PATH --storage-mode local_alternative (or explicit_synced after consent). No audio was imported.")


def configure_storage(settings, data_root, storage_mode):
    path = Path(settings["settings_path"])
    if path.exists():
        raise FileExistsError("Settings already exist; they were preserved. Use an explicit alternate --settings path for a deliberate configuration change.")
    roots = dict(settings["roots"])
    roots["data"] = str(Path(data_root).expanduser().resolve())
    if Path(roots["data"]).is_relative_to(Path(roots["code"])):
        raise ValueError("Permanent audio must not live inside CODE_ROOT.")
    value = {"schema_version": 1, "roots": roots, "runtime": {}, "models": {},
             "storage_approval": {"data_root": roots["data"], "mode": storage_mode, "approved_at": now()}}
    write_json(path, value)
    emit({"settings": str(path), "roots": roots, "storage_approval": value["storage_approval"]})


def doctor(settings):
    result = {"settings": settings["settings_path"], "settings_exists": settings["settings_exists"],
              "roots": settings["roots"], "storage_approved": settings["storage_approved"],
              "process_architecture": platform.machine(), "python": sys.version.split()[0],
              "free_bytes": shutil.disk_usage(Path(settings["roots"]["code"])).free,
              "network": "No application network operations. Downloads are a separate explicit installer.",
              "legacy_migration": "No automatic legacy deletion or relocation."}
    try:
        runtime = load_runtime(settings)
        result.update({"runtime": runtime["runtime"], "hardware": runtime.get("hardware"),
                       "models": runtime["models"]})
    except (OSError, ValueError) as error:
        result["runtime_problem"] = str(error)
    emit(result)


def assigned_config(settings, args, session=None):
    overrides = {}
    if getattr(args, "model", None):
        overrides["asr"] = {"model": args.model}
    if getattr(args, "channel", None):
        overrides["preprocessing"] = {"channel": args.channel}
    selected = {}
    for kind in ("speaker", "capture", "glossary"):
        explicit = getattr(args, kind, None)
        selected[kind] = (session or {}).get("profiles", {}).get(kind) if explicit is None else explicit
        if selected[kind] == "-":
            selected[kind] = None
    return resolve_config(settings["roots"]["data"], **selected, overrides=overrides)


def confirm_cli_order(files):
    if not sys.stdin.isatty():
        raise ValueError("Multiple files require --order-confirmed in the intended positional order, or use the native launcher.")
    print("Concatenated audio order (unknown real-world gaps will not be inferred):", flush=True)
    for i, path in enumerate(files, 1):
        print(f"{i}. {path}", flush=True)
    if input("Use this exact source order? [y/N] ").strip().lower() not in {"y", "yes"}:
        return False
    return True


def transcribe(settings, args):
    require_storage(settings)
    if args.session:
        if args.files or args.separate:
            raise ValueError("Use --session or input files, not both.")
        session_path = locate_session(settings["roots"]["data"], args.session)
        session = read_doc(session_path / "session.yaml")
        reused = True
        resolved = assigned_config(settings, args, session)
    else:
        resolved = assigned_config(settings, args)
        if not args.files:
            raise ValueError("Select at least one audio file, provide --session ID, or run launch.")
        if len(args.files) > 1 and not args.order_confirmed:
            if not confirm_cli_order(args.files):
                return {"cancelled": True}
            args.order_confirmed = True
        # Validate every input before copying anything into permanent session storage.
        for path in args.files:
            inspect_media(settings, Path(path).expanduser())
        session_path, session, reused = import_sources(settings["roots"]["data"], args.files,
                                                      separate=args.separate, order_confirmed=args.order_confirmed)
    emit({"session_id": session["id"], "session_path": str(session_path), "sources": len(session["sources"]),
          "existing_session": reused, "profiles": resolved["selected_profiles"],
          "model": resolved["asr"]["model"], "preprocessing": resolved["preprocessing"],
          "glossary_terms": len(resolved["glossary"]["terms"]), "accuracy_acceptance": "pending"})
    if args.import_only:
        with session_lock(session_path):
            session["profiles"] = resolved["selected_profiles"]
            write_yaml(session_path / "session.yaml", session, overwrite=True)
        return {"session_id": session["id"], "path": str(session_path), "state": "imported", "reused": reused}
    return run_session(settings, session_path, resolved, force=args.force)


def native_dialog(code_root: Path, payload: dict) -> dict:
    result = subprocess.run(["/usr/bin/osascript", "-l", "JavaScript", str(code_root / "scripts" / "native-dialog.js"),
                             json.dumps(payload, ensure_ascii=False)], capture_output=True, text=True)
    if result.returncode:
        # Never echo dialog stderr: it may contain user input.
        raise RuntimeError("Native dialog failed or macOS denied permission; no files were imported.")
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise ValueError("Unexpected native dialog response.")
    return value


def report_command(settings, args):
    require_storage(settings)
    paths = list(args.files)
    if len(paths) > 1 and not args.order_confirmed:
        if not sys.stdin.isatty():
            raise ValueError("Multiple files require --order-confirmed in the desired argument order, or use the native launcher.")
        paths = natural_order(paths)
        print("Proposed file order (timestamps restart for each file):", flush=True)
        for i, path in enumerate(paths, 1):
            print(f"{i}. {path}", flush=True)
        answer = input("Enter y to confirm, row numbers in the desired order (e.g. 2,1,3), or Enter to cancel: ").strip()
        if not answer:
            return {"cancelled": True, "imported": False}
        if answer.lower() not in {"y", "yes"}:
            try:
                order = [int(p) for p in answer.replace(",", " ").split()]
            except ValueError:
                raise ValueError("Order must contain each displayed row number once.") from None
            if sorted(order) != list(range(1, len(paths) + 1)):
                raise ValueError("Order must contain each displayed row number once.")
            paths = [paths[n - 1] for n in order]
    resolved = assigned_config(settings, args)
    return build_report(settings, paths, resolved, progress=lambda message: print(message, flush=True),
                        **({"retry_failed": True} if getattr(args, "retry_failed", False) else {}))


def launch(settings):
    require_storage(settings)
    data_root, code_root = Path(settings["roots"]["data"]), Path(settings["roots"]["code"])
    payload = {"action": "choose", "profiles": {
        kind: [{"id": p["id"], "label": p.get("label")} for p in list_profiles(data_root, kind)]
        for kind in ("speaker", "capture", "glossary")}}
    selection = native_dialog(code_root, payload)
    if selection.get("cancelled"):
        return {"cancelled": True, "imported": False}
    for profile in selection.get("new_profiles", []):
        create_profile(data_root, profile["kind"], profile["id"], profile.get("label"))
    profiles = selection["profiles"]
    args = argparse.Namespace(files=selection["files"], session=None, force=False, separate=False,
                              order_confirmed=True, import_only=False, model=None, channel=None, **profiles)
    result = report_command(settings, args)
    if result.get("report"):
        subprocess.run(["/usr/bin/open", "-R", result["report"]], check=True)
    return result


def app_report_command(settings, request_path):
    """No terminal dialogs, no transcript text on the native UI protocol."""
    def event(value):
        print(json.dumps(value, ensure_ascii=False), flush=True)
    try:
        request = read_doc(Path(request_path))
        paths = request.get("files")
        if not isinstance(paths, list) or not paths or any(not isinstance(p, str) or not Path(p).is_absolute() for p in paths):
            raise ValueError("Choose at least one local recording.")
        result = build_report(settings, paths, resolve_config(Path(settings["roots"]["data"])), events=event,
                              retry_failed=request.get("retry_failed") is True,
                              **({"groups": request["groups"]} if "groups" in request else {}))
        event({"type": "result", **result})
        return 1 if result["state"] == "partial" else 0
    except KeyboardInterrupt:
        event({"type": "cancelled", "message": "Cancelled. Completed files are preserved. Click Transcribe to resume."})
        return 130
    except (ValueError, OSError, RuntimeError, KeyError):
        event({"type": "error", "message": "Processing could not start or finish. Check the files and available disk space, then retry. Local logs are preserved."})
        return 2


def app_library_command(settings, request_path):
    """Read requested local text only on this separate, explicit library channel."""
    try:
        emit(library_request(settings, read_doc(Path(request_path))))
        return 0
    except (ValueError, OSError, RuntimeError, KeyError, TypeError):
        emit({"type": "error", "message": "The local library request could not be completed. Check the selected report or recording metadata."})
        return 2


def main(argv=None):
    os.umask(0o077)
    def interrupted(signum, frame):
        raise KeyboardInterrupt()
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, interrupted)
    args = parser().parse_args(argv)
    try:
        settings = load_settings(args.settings)
        if args.command == "doctor":
            doctor(settings)
            return 0
        if args.command == "configure":
            configure_storage(settings, args.data_root, args.storage_mode)
            return 0
        require_storage(settings)
        if args.command == "app-report":
            return app_report_command(settings, args.request)
        if args.command == "app-library":
            return app_library_command(settings, args.request)
        if args.command == "transcribe":
            result = transcribe(settings, args)
        elif args.command == "report":
            result = report_command(settings, args)
        elif args.command in {"pilot", "benchmark"}:
            session = locate_session(settings["roots"]["data"], args.session_id)
            intervals = None
            if args.interval:
                intervals = []
                for value in args.interval:
                    parts = value.split(":")
                    if len(parts) != 3:
                        raise ValueError("Intervals use SOURCE_ID:START_SECONDS:END_SECONDS.")
                    intervals.append((parts[0], float(parts[1]), float(parts[2])))
            result = run_benchmark(settings, session, assigned_config(settings, args, read_doc(session / "session.yaml")), intervals=intervals,
                                   pilot=args.command == "pilot", force=args.force)
        elif args.command == "profile":
            if args.profile_command == "create":
                result = {"created": str(create_profile(settings["roots"]["data"], args.kind, args.id, args.label))}
            else:
                result = {"profiles": [{"id": p["id"], "label": p.get("label")} for p in list_profiles(settings["roots"]["data"], args.kind)]}
        elif args.command == "archive":
            result = {"path": str(archive_session(settings["roots"]["data"], args.session_id))}
        elif args.command == "restore":
            result = {"path": str(restore_session(settings["roots"]["data"], args.session_id))}
        elif args.command == "inspect":
            path = locate_session(settings["roots"]["data"], args.session_id)
            session = read_doc(path / "session.yaml")
            current_path = path / "transcript" / "current.json"
            result = {"session_id": session["id"], "path": str(path), "source_count": len(session["sources"]),
                      "profiles": session["profiles"], "processing_status": session["processing_status"],
                      "current": read_doc(current_path) if current_path.exists() else None}
        elif args.command == "accept":
            validate_id(args.run_id, "run ID")
            path = locate_session(settings["roots"]["data"], args.session_id)
            with session_lock(path):
                if not verify_completed_run(path / "transcript" / args.run_id):
                    raise ValueError("Only an intact technically completed run can be accepted.")
                current_path = path / "transcript" / "current.json"
                current = read_doc(current_path)
                current.update({"accepted_run_id": args.run_id, "accepted_at": now(),
                                "acceptance_provenance": "explicit user accept command"})
                current["accuracy_acceptance"] = "accepted" if current.get("latest_successful_run_id") == args.run_id else "pending"
                write_json(current_path, current, overwrite=True)
            result = {"accepted_run_id": args.run_id, "current": str(current_path)}
        elif args.command == "evaluate":
            result = evaluate_reference(settings, args.benchmark_id, args.evaluation_id, args.clip_id,
                                        Path(args.reference).expanduser(), verified=args.verified)
        elif args.command == "launch":
            result = launch(settings)
        else:
            raise ValueError("Unknown operation.")
        emit(result)
        return 1 if result.get("state") == "partial" else 0
    except KeyboardInterrupt:
        print("Interrupted. Completed checkpoints and originals are preserved; rerun the same command to resume.", file=sys.stderr)
        return 130
    except (ValueError, OSError, RuntimeError, KeyError) as error:
        print(f"AudioTranscribe: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
