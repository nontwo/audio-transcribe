"""Readable session storage with byte-preserving import and process locks."""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import uuid
import unicodedata

import yaml

SCHEMA_VERSION = 1
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}\Z")


def validate_id(value: str, label: str = "identifier") -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"Invalid {label}: use 1–96 letters, digits, underscores or hyphens; begin with a letter or digit.")
    return value


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_value(value, location="document"):
    """Reject YAML dates, non-string mapping keys, NaN and other surprise types."""
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        import math
        if math.isfinite(value):
            return
    elif isinstance(value, list):
        for item in value:
            _json_value(item, location)
        return
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{location} requires string mapping keys.")
            _json_value(item, f"{location}.{key}")
        return
    raise ValueError(f"Unsupported value in {location}; use JSON-compatible scalars and quote dates.")


def read_doc(path: Path | str) -> dict:
    path = Path(path)
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle) if path.suffix.lower() == ".json" else yaml.safe_load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"Expected a mapping in {path.name}.")
    _json_value(value)
    return value


def _sync_directory(directory: Path):
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_atomic(path: Path | str, text: str, *, overwrite: bool = False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if overwrite:
            os.replace(temporary, path)
        else:
            # An atomic exclusive create: even a competing writer cannot be overwritten.
            os.link(temporary, path)
            os.unlink(temporary)
        _sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path: Path | str, data: dict, *, overwrite: bool = False):
    _json_value(data)
    _write_atomic(path, json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n", overwrite=overwrite)


def write_yaml(path: Path | str, data: dict, *, overwrite: bool = False):
    _json_value(data)
    _write_atomic(path, yaml.safe_dump(data, sort_keys=False, allow_unicode=True), overwrite=overwrite)


@contextlib.contextmanager
def _file_lock(path: Path, *, blocking: bool = False):
    with path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as error:
            raise RuntimeError("This session or import is active in another process; retry after it finishes.") from error
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def session_lock(session_path: Path | str, *, blocking: bool = False):
    session_path = Path(session_path)
    if not session_path.is_dir():
        raise FileNotFoundError("Session directory does not exist.")
    with _file_lock(session_path / ".session.lock", blocking=blocking):
        yield


def _session_paths(data_root: Path):
    yield from sorted((data_root / "sessions").glob("*/session.yaml"))
    yield from sorted((data_root / "archive").glob("[0-9][0-9][0-9][0-9]/*/session.yaml"))


def locate_session(data_root: Path | str, session_id: str) -> Path:
    validate_id(session_id, "session ID")
    data_root = Path(data_root)
    matches = []
    direct = data_root / "sessions" / session_id
    if (direct / "session.yaml").is_file():
        matches.append(direct)
    matches.extend(path.parent for path in (data_root / "archive").glob(f"[0-9][0-9][0-9][0-9]/{session_id}/session.yaml"))
    if not matches:
        raise FileNotFoundError(f"Unknown session ID: {session_id}")
    if len(matches) > 1:
        raise ValueError(f"Ambiguous session ID {session_id}; found multiple managed copies.")
    if read_doc(matches[0] / "session.yaml").get("id") != session_id:
        raise ValueError("Session directory and metadata IDs disagree.")
    return matches[0]


def managed_source_path(session_path: Path | str, source: dict) -> Path:
    """Validate a source identity and return its contained, resolved file path."""
    if not isinstance(source, dict):
        raise ValueError("Each managed source must be a mapping.")
    validate_id(source.get("id"), "source ID")
    relative_value = source.get("path")
    basename = source.get("original_basename")
    if not isinstance(relative_value, str) or not isinstance(basename, str):
        raise ValueError("Managed sources require relative path and original_basename strings.")
    relative = Path(relative_value)
    root = Path(session_path).resolve()
    resolved = (root / relative).resolve()
    if relative.is_absolute() or not relative.parts or relative.parts[0] != "source" or ".." in relative.parts or not resolved.is_relative_to(root / "source"):
        raise ValueError("Managed source path must remain inside its source directory.")
    if relative.name != basename or basename in ("", ".", ".."):
        raise ValueError("Managed source basename and source path disagree.")
    digest = source.get("sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Managed sources require a lowercase SHA-256.")
    if type(source.get("size_bytes")) is not int or source["size_bytes"] < 0:
        raise ValueError("Managed source size_bytes must be a nonnegative integer.")
    if type(source.get("order")) is not int or source["order"] < 0:
        raise ValueError("Managed source order must be a nonnegative integer.")
    if not resolved.is_file() or resolved.stat().st_size != source["size_bytes"]:
        raise ValueError("Managed source is missing or failed size integrity verification.")
    return resolved


def validate_session(session_path: Path | str, session=None, *, verify_sources=False) -> dict:
    """Reject malformed identities and path escapes before opening managed data."""
    path = Path(session_path)
    session = read_doc(path / "session.yaml") if session is None else session
    if not isinstance(session, dict) or type(session.get("schema_version")) is not int or session["schema_version"] != SCHEMA_VERSION:
        raise ValueError("Session schema_version must be 1.")
    session_id = validate_id(session.get("id"), "session ID")
    if path.name != session_id:
        raise ValueError("Session directory and metadata IDs disagree.")
    sources = session.get("sources")
    order = session.get("source_order")
    if not isinstance(sources, list) or not sources or not isinstance(order, list):
        raise ValueError("Session requires a nonempty sources list and explicit source_order.")
    ids = []
    for index, source in enumerate(sources):
        managed_source_path(path, source)
        if source["order"] != index:
            raise ValueError("Source order numbers must match the ordered sources list.")
        ids.append(source["id"])
    if len(set(ids)) != len(ids) or order != ids:
        raise ValueError("source_order must list each source ID exactly once in the documented source order.")
    if verify_sources:
        _verify_managed_sources(path, session)
    return session


def _verify_managed_sources(path: Path, session: dict):
    for source in session["sources"]:
        resolved = managed_source_path(path, source)
        if not resolved.is_file() or resolved.stat().st_size != source["size_bytes"] or sha256_file(resolved) != source["sha256"]:
            raise ValueError("An existing managed source failed integrity verification; preserve it and investigate before reuse.")


def import_sources(data_root: Path | str, paths, separate: bool = False, order_confirmed: bool = False):
    inputs = [Path(path).expanduser() for path in paths]
    if not inputs:
        raise ValueError("Select at least one existing audio file.")
    if len(inputs) > 1 and not order_confirmed:
        raise ValueError("Multiple files require explicit order confirmation; use --order-confirmed in the intended source order.")
    for path in inputs:
        if not path.is_file():
            raise FileNotFoundError(f"Input is missing or is not a regular file: {path}")
    hashes = [sha256_file(path) for path in inputs]
    if len(set(hashes)) != len(hashes):
        raise ValueError("The same audio bytes were selected more than once; remove the duplicate selection.")
    data_root = Path(data_root).expanduser()
    data_root.mkdir(parents=True, exist_ok=True)
    sessions_root = data_root / "sessions"
    sessions_root.mkdir(exist_ok=True)
    with _file_lock(data_root / ".import.lock"):
        if not separate:
            overlaps = []
            for metadata in _session_paths(data_root):
                if metadata.parent.name.startswith("."):
                    continue
                session = validate_session(metadata.parent)
                sources = session.get("sources", [])
                order = session.get("source_order", [source["id"] for source in sources])
                by_id = {source["id"]: source for source in sources}
                existing = [by_id[source_id]["sha256"] for source_id in order]
                if hashes == existing:
                    with session_lock(metadata.parent):
                        _verify_managed_sources(metadata.parent, session)
                    return metadata.parent, session, True
                if set(hashes).intersection(existing):
                    overlaps.append(session.get("id", metadata.parent.name))
            if overlaps:
                raise ValueError("Selected audio overlaps an existing session or changes its order (" + ", ".join(overlaps) + "). Select the complete original ordered source list, use its session ID, or explicitly request --separate.")
        session_id = "s-" + uuid.uuid4().hex
        final_path = sessions_root / session_id
        staging = Path(tempfile.mkdtemp(prefix=".import-", dir=sessions_root))
        try:
            (staging / "source").mkdir()
            sources = []
            used_basenames = set()
            date_hints = []
            for index, (input_path, expected_hash) in enumerate(zip(inputs, hashes)):
                source_id = "src-" + uuid.uuid4().hex
                basename = input_path.name
                relative = Path("source") / basename
                # Casefold also handles the usual case-insensitive macOS volume.
                normalized_basename = unicodedata.normalize("NFC", basename).casefold()
                if normalized_basename in used_basenames:
                    relative = Path("source") / source_id / basename
                used_basenames.add(normalized_basename)
                destination = staging / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                before = input_path.stat()
                with input_path.open("rb") as source, destination.open("xb") as target:
                    shutil.copyfileobj(source, target, 1024 * 1024)
                    target.flush()
                    os.fsync(target.fileno())
                after = input_path.stat()
                if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns or destination.stat().st_size != before.st_size or sha256_file(destination) != expected_hash or sha256_file(input_path) != expected_hash:
                    raise ValueError("Source changed or copying failed integrity verification; no session was imported.")
                sources.append({"id": source_id, "original_basename": basename, "size_bytes": before.st_size, "sha256": expected_hash, "path": relative.as_posix(), "order": index})
                date_hints.append({"source_id": source_id, "kind": "filesystem_mtime", "value": dt.datetime.fromtimestamp(before.st_mtime, tz=dt.timezone.utc).isoformat(), "provenance": "External file filesystem modification time; not a confirmed recording date or recording timezone."})
            session = {"schema_version": SCHEMA_VERSION, "id": session_id, "recorded_at": None, "date_hints": date_hints, "context": {"course": None, "institution": None, "semester": None, "event": None}, "tags": [], "language": "en", "profiles": {"speaker": None, "capture": None, "glossary": None}, "sources": sources, "source_order": [source["id"] for source in sources], "processing_status": "imported", "runs": []}
            write_yaml(staging / "session.yaml", session)
            os.rename(staging, final_path)
            _sync_directory(sessions_root)
            return final_path, session, False
        finally:
            if staging.exists():
                shutil.rmtree(staging)


def _assert_inactive(session: dict):
    active = {"running", "processing", "importing", "active", "in_progress"}
    if session.get("processing_status") in active or any(isinstance(run, dict) and run.get("status") in active for run in session.get("runs", [])):
        raise ValueError("This session has an active or interrupted run; resolve it before moving the session.")


def archive_session(data_root: Path | str, session_id: str, year=None) -> Path:
    data_root = Path(data_root)
    path = locate_session(data_root, session_id)
    if path.parent.parent.name == "archive":
        return path
    year = str(year if year is not None else dt.datetime.now(dt.timezone.utc).year)
    if not re.fullmatch(r"[0-9]{4}", year):
        raise ValueError("Archive year must contain exactly four digits.")
    with session_lock(path):
        _assert_inactive(validate_session(path))
        destination = data_root / "archive" / year / session_id
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise FileExistsError("Archive destination already exists; no session was moved.")
        os.rename(path, destination)
        _sync_directory(destination.parent)
    return destination


def restore_session(data_root: Path | str, session_id: str) -> Path:
    data_root = Path(data_root)
    path = locate_session(data_root, session_id)
    destination = data_root / "sessions" / session_id
    if path == destination:
        return path
    with session_lock(path):
        _assert_inactive(validate_session(path))
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise FileExistsError("Restore destination already exists; no session was moved.")
        os.rename(path, destination)
        _sync_directory(destination.parent)
    return destination
