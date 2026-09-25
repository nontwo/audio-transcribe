#!/bin/sh
audio_code_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
if [ -d "$HOME/Applications/AudioTranscribe.app" ]; then
    exec /usr/bin/open "$HOME/Applications/AudioTranscribe.app"
fi
if [ -d "$audio_code_root/native/build/AudioTranscribe.app" ]; then
    exec /usr/bin/open "$audio_code_root/native/build/AudioTranscribe.app"
fi
exec "$audio_code_root/scripts/audio-transcribe" launch
