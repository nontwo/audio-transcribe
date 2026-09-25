#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
TEST_BUILD=$(mktemp -d "${TMPDIR:-/tmp}/audio-transcribe-native-tests.XXXXXX")
trap 'rm -rf "$TEST_BUILD"' EXIT HUP INT TERM
/usr/bin/xcrun swiftc -swift-version 5 -module-cache-path "$TEST_BUILD/module-cache" \
  "$ROOT/native/Queue.swift" "$ROOT/native/Progress.swift" "$ROOT/native/Library.swift" \
  "$ROOT/native/QueueTests.swift" -o "$TEST_BUILD/native-tests"
"$TEST_BUILD/native-tests"
