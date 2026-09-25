#!/usr/bin/env python3
"""Explicit, resumable installation of the pinned local whisper.cpp backend.

This is the only application operation that needs network access. It never reads
audio or user profiles. Run with the project's isolated Python environment.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import sys
import tarfile
import time

RELEASE = "v1.9.4"
COMMIT = "927cfce34f31707e17f2bff35c349632fb9e2c3a"
HF_REVISION = "5359861c739e955e79d9a303bcbc70fb988958b1"
MODELS = {
    "large-v3": {
        "bytes": 3095033483,
        "sha256": "64d182b440b98d5203c4f9bd541544d84c605196c4f7b845dfa11fb23594d1e2",
        "sha1": "ad82bf6a9043ceed055076d0fd39f5f186ff8062",
    },
    "large-v3-turbo": {
        "bytes": 1624555275,
        "sha256": "1fc70f774d38eb169993ac391eea357ef47c88757ef72ee5943879b7e8e2bc69",
        "sha1": "4af2b29d7ec73d781377bfd1758ca957a807e941",
    },
}


def hashes(path: Path) -> dict:
    algorithms = {"sha256": hashlib.sha256(), "sha1": hashlib.sha1()}
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            for digest in algorithms.values():
                digest.update(block)
    return {name: digest.hexdigest() for name, digest in algorithms.items()}


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".pending")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def capture(arguments: list[str]) -> str:
    result = subprocess.run(arguments, capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else "unavailable"


def download(url: str, target: Path, *, expected: dict | None = None) -> dict:
    """Resume a partial transfer. Never expose signed redirect URLs or headers."""
    if target.exists():
        actual = hashes(target)
        if expected and (target.stat().st_size != expected["bytes"] or any(
            actual[name] != expected[name] for name in ("sha256", "sha1")
        )):
            raise RuntimeError(f"Existing download has an unexpected checksum: {target.name}; preserved.")
        return actual
    partial = target.with_suffix(target.suffix + ".partial")
    # All hosts are official upstream or upstream-documented public model sources.
    command = ["/usr/bin/curl", "--fail", "--location", "--silent", "--show-error",
               "--connect-timeout", "30", "--max-time", "3600", "--proto", "=https",
               "--proto-redir", "=https", "--continue-at", "-", "--output", str(partial), url]
    process = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if process.returncode:
        # The code suffices for diagnosis and cannot disclose a redirected token URL.
        raise RuntimeError(f"Download stopped (curl exit {process.returncode}); partial file retained: {partial.name}")
    actual = hashes(partial)
    if expected and (partial.stat().st_size != expected["bytes"] or any(
        actual[name] != expected[name] for name in ("sha256", "sha1")
    )):
        raise RuntimeError(f"Downloaded checksum mismatch: {partial.name}; preserved for diagnosis.")
    partial.replace(target)
    return actual


def install(app_root: Path, jobs: int, models_only: bool, build_only: bool) -> None:
    app_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    logs = app_root / "install-logs"
    logs.mkdir(exist_ok=True)
    checkpoint_path = app_root / "install-checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text()) if checkpoint_path.exists() else {"schema_version": 1, "completed": {}}
    manifest_path = app_root / "runtime.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {"schema_version": 1, "models": {}}

    def mark(step: str, details: dict) -> None:
        checkpoint["completed"][step] = details
        checkpoint["updated_at_unix"] = time.time()
        atomic_json(checkpoint_path, checkpoint)
        print(f"Verified: {step}", flush=True)

    machine = platform.machine()
    translated = capture(["/usr/sbin/sysctl", "-in", "sysctl.proc_translated"])
    if translated == "1":
        raise RuntimeError("Rosetta is active. Use a native Python interpreter to build the runtime.")
    manifest["hardware"] = {
        "architecture": machine,
        "rosetta_translated": translated == "1",
        "cpu": capture(["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"]),
        "physical_memory_bytes": int(capture(["/usr/sbin/sysctl", "-n", "hw.memsize"])),
        "macos_version": capture(["/usr/bin/sw_vers", "-productVersion"]),
        "macos_build": capture(["/usr/bin/sw_vers", "-buildVersion"]),
        "disk_free_bytes_at_setup": shutil.disk_usage(app_root).free,
        "xcode": capture(["/usr/bin/xcodebuild", "-version"]),
    }
    manifest["dependencies"] = {
        "python": platform.python_version(),
        **{name: importlib.metadata.version(name) for name in ("numpy", "scipy", "PyYAML", "cmake")},
    }

    if not models_only:
        runtime = app_root / "runtime"
        runtime.mkdir(exist_ok=True)
        archive = runtime / f"whisper.cpp-{RELEASE}-{COMMIT[:12]}.tar.gz"
        archive_hashes = download(f"https://codeload.github.com/ggml-org/whisper.cpp/tar.gz/{COMMIT}", archive)
        mark("runtime-source", {"release": RELEASE, "commit": COMMIT, **archive_hashes})
        source = runtime / f"whisper.cpp-{COMMIT}"
        if not source.exists():
            # Publish a complete source directory, never a half-extracted tree.
            # An interrupted staging directory is safely reused on the next run.
            staging = runtime / f".extracting-{COMMIT}"
            staging.mkdir(exist_ok=True)
            with tarfile.open(archive) as package:
                package.extractall(staging, filter="data")
            (staging / source.name).rename(source)
            staging.rmdir()
        build = source / "build"
        cmake = Path(sys.executable).parent / "cmake"
        configure = [str(cmake), "-S", str(source), "-B", str(build), "-DCMAKE_BUILD_TYPE=Release",
                     f"-DCMAKE_OSX_ARCHITECTURES={machine}", f"-DGGML_METAL={'ON' if machine == 'arm64' else 'OFF'}",
                     "-DWHISPER_BUILD_TESTS=OFF", "-DWHISPER_BUILD_EXAMPLES=ON"]
        compile_command = [str(cmake), "--build", str(build), "--config", "Release", "--target", "whisper-cli", "-j", str(jobs)]
        with (logs / "build.log").open("ab") as stream:
            for command in (configure, compile_command):
                result = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, timeout=1800)
                if result.returncode:
                    raise RuntimeError("Runtime build failed. See install-logs/build.log; all checkpoints retained.")
        cli = build / "bin" / "whisper-cli"
        cli_info = capture(["/usr/bin/file", str(cli)])
        if machine not in cli_info:
            raise RuntimeError("Built CLI does not report the expected native architecture.")
        result = subprocess.run([str(cli), "--help"], capture_output=True, text=True, timeout=30)
        help_text = result.stdout + result.stderr
        (logs / "whisper-cli-help.txt").write_text(help_text, encoding="utf-8")
        required = ("--output-json-full", "--output-file", "--beam-size", "--temperature", "--temperature-inc", "--language")
        if result.returncode or any(flag not in help_text for flag in required):
            raise RuntimeError("Installed CLI help does not match the supported decoder interface.")
        manifest["runtime"] = {
            "release": RELEASE, "commit": COMMIT, "cli": str(cli), "path": str(source),
            "sha256": hashes(cli)["sha256"],
            "build_info": {"architecture": machine, "metal_compiled": machine == "arm64",
                           "metal_execution_verified": False, "configure_arguments": configure,
                           "build_arguments": compile_command, "file_description": cli_info},
            "source_archive_sha256": archive_hashes["sha256"],
            "help_path": str(logs / "whisper-cli-help.txt"),
        }
        mark("runtime-build", manifest["runtime"])
        atomic_json(manifest_path, manifest)

    if not build_only:
        model_root = app_root / "models"
        model_root.mkdir(exist_ok=True)
        for name, expected in MODELS.items():
            path = model_root / f"ggml-{name}.bin"
            print(f"Downloading/verifying {name} (resumes existing partial bytes)...", flush=True)
            actual = download(f"https://huggingface.co/ggerganov/whisper.cpp/resolve/{HF_REVISION}/{path.name}", path, expected=expected)
            with path.open("rb") as stream:
                header = struct.unpack("<12i", stream.read(48))
            # GGML Whisper header: magic then eleven hyperparameters; ftype is the last.
            if header[0] != 0x67676d6c or header[-1] != 1:
                raise RuntimeError(f"Expected standard GGML F16 model header for {name}.")
            manifest["models"][name] = {
                "path": str(path), "sha256": actual["sha256"], "sha1": actual["sha1"],
                "bytes": path.stat().st_size, "precision": "standard GGML F16 (non-quantized; mixed F16/F32 tensors)",
                "ggml_ftype": header[-1], "model_id": name, "repository_revision": HF_REVISION,
                "upstream": "https://huggingface.co/ggerganov/whisper.cpp",
                "hash_verification": "SHA-256 from upstream Hugging Face LFS metadata and SHA-1 from pinned whisper.cpp models/README.md",
            }
            mark(f"model-{name}", manifest["models"][name])
            atomic_json(manifest_path, manifest)
    print(f"Runtime manifest: {manifest_path}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-root", type=Path, default=Path.home() / "Library/Application Support/AudioTranscribe")
    parser.add_argument("--jobs", type=int, default=4)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--models-only", action="store_true")
    group.add_argument("--build-only", action="store_true")
    arguments = parser.parse_args()
    if not 1 <= arguments.jobs <= 8:
        parser.error("--jobs must be between 1 and 8")
    try:
        install(arguments.app_root.expanduser().resolve(), arguments.jobs, arguments.models_only, arguments.build_only)
    except Exception as error:
        print(f"Setup incomplete: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
