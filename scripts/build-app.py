#!/usr/bin/env python3
"""Build the local AppKit shell; optionally place a copy in ~/Applications."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import plistlib
import platform
import shutil
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument("--install", action="store_true")
args = parser.parse_args()
vendor = root / "native/vendor"
binaries = list((vendor / "imageio_ffmpeg/binaries").glob("ffmpeg-macos-*"))
if not binaries:
    subprocess.run([sys.executable, "-m", "pip", "install", "--only-binary=:all:", "--no-deps",
                    "--target", str(vendor), "imageio-ffmpeg==0.6.0"], check=True)
    binaries = list((vendor / "imageio_ffmpeg/binaries").glob("ffmpeg-macos-*"))
if len(binaries) != 1:
    raise SystemExit("Expected exactly one local macOS FFmpeg binary.")
ffmpeg = binaries[0]
version = subprocess.run([str(ffmpeg), "-version"], capture_output=True, text=True, check=True).stdout.splitlines()[0]
(root / "native/media-runtime.json").write_text(json.dumps({"package": "imageio-ffmpeg", "package_version": "0.6.0",
    "relative_path": str(ffmpeg.relative_to(root)), "sha256": hashlib.sha256(ffmpeg.read_bytes()).hexdigest(), "version": version}, indent=2) + "\n")
app = root / "native/build/AudioTranscribe.app"
contents = app / "Contents"
(contents / "MacOS").mkdir(parents=True, exist_ok=True)
(contents / "Resources").mkdir(exist_ok=True)
extensions = ["wav", "wave", "m4a", "mp3", "flac", "aac", "aiff", "aif", "aifc", "ogg", "oga", "opus", "mp4", "mov"]
metadata = {"CFBundleName": "AudioTranscribe", "CFBundleDisplayName": "AudioTranscribe",
    "CFBundleIdentifier": "local.audio-transcribe.desktop", "CFBundleVersion": "1", "CFBundleShortVersionString": "2.1",
    "CFBundleExecutable": "AudioTranscribe", "CFBundlePackageType": "APPL", "LSMinimumSystemVersion": "13.0",
    "NSHighResolutionCapable": True, "NSPrincipalClass": "NSApplication", "AudioTranscribeCodeRoot": str(root),
    "CFBundleDocumentTypes": [{"CFBundleTypeName": "Audio and audio-bearing media", "CFBundleTypeRole": "Viewer",
        "LSHandlerRank": "Alternate", "CFBundleTypeExtensions": extensions}],
    "NSHumanReadableCopyright": "Local AudioTranscribe utility. Audio stays on this Mac."}
with (contents / "Info.plist").open("wb") as f:
    plistlib.dump(metadata, f)
subprocess.run(["/usr/bin/xcrun", "swiftc", "-swift-version", "5", "-O", "-framework", "Cocoa",
    "-target", platform.machine() + "-apple-macos13.0",
    "-module-cache-path", str(root / "native/build/module-cache"),
    "-framework", "UniformTypeIdentifiers", *[str(root / "native" / name) for name in
        ["Queue.swift", "Progress.swift", "Library.swift", "GroupingSheet.swift", "LibraryView.swift", "main.swift"]],
    "-o", str(contents / "MacOS/AudioTranscribe")], check=True)
subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(app)], check=True)
if args.install:
    destination = Path.home() / "Applications/AudioTranscribe.app"
    destination.parent.mkdir(exist_ok=True)
    pending = destination.with_name(".AudioTranscribe-install.app")
    if pending.exists():
        shutil.rmtree(pending)
    shutil.copytree(app, pending)
    if destination.exists():
        old = destination.with_name(".AudioTranscribe-previous.app")
        if old.exists():
            shutil.rmtree(old)
        destination.rename(old)
    pending.rename(destination)
    print(destination)
print(app)
