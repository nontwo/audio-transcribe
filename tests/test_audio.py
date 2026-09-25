import hashlib
import json
import struct
import wave

import numpy as np
import pytest

from audio_transcribe import audio


def wav_fixture(path, values, *, rate=48000, bits=32, floating=True, extra=True, orphan=b"", extensible=False):
    values = np.asarray(values)
    if values.ndim == 1:
        values = values[:, None]
    channels = values.shape[1]
    tag = 3 if floating else 1
    if floating:
        raw = values.astype("<f4" if bits == 32 else "<f8").tobytes()
    elif bits == 24:
        integers = np.rint(values * 8388608).astype(np.int32).ravel()
        raw = np.column_stack([integers & 255, (integers >> 8) & 255, (integers >> 16) & 255]).astype(np.uint8).tobytes()
    else:
        raw = np.rint(values * 2 ** (bits - 1)).astype("<i2" if bits == 16 else "<i4").tobytes()
    align = channels * bits // 8
    fmt = struct.pack("<HHIIHH", 0xFFFE if extensible else tag, channels, rate, rate * align, align, bits)
    if extensible:
        fmt += struct.pack("<HHI", 22, bits, 0) + struct.pack("<I", tag) + bytes.fromhex("00001000800000aa00389b71")
    def chunk(label, payload):
        return label + struct.pack("<I", len(payload)) + payload + (b"\0" if len(payload) % 2 else b"")
    body = b"WAVE" + chunk(b"fmt ", fmt)
    if extra:
        body += chunk(b"JUNK", b"metadata, not samples")
    body += chunk(b"data", raw + orphan)
    if extra:
        body += chunk(b"LIST", b"odd")
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    return path


def samples(path):
    with wave.open(str(path), "rb") as source:
        return np.frombuffer(source.readframes(source.getnframes()), dtype="<i2")


@pytest.mark.parametrize("floating,bits", [(False, 16), (False, 24), (False, 32), (True, 32), (True, 64)])
@pytest.mark.parametrize("extensible", [False, True])
def test_supported_formats_ignore_metadata(tmp_path, floating, bits, extensible):
    path = wav_fixture(tmp_path / "声音 with spaces.wav", [[-0.5, 0.25], [0.5, -0.25]] * 160,
                       bits=bits, floating=floating, extensible=extensible)
    info = audio.inspect_audio(path)
    assert info["complete_frames"] == 320
    assert info["duration_seconds"] == 320 / 48000
    assert info["channel_rms_dbfs"] == pytest.approx([-6.020599913, -12.041199827])
    assert info["channel_peak_dbfs"] == pytest.approx(info["channel_rms_dbfs"])
    assert info["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert info["stereo_correlation"] == pytest.approx(-1)
    json.dumps(info, allow_nan=False)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_rejected_without_output(tmp_path, bad):
    path = wav_fixture(tmp_path / "bad.wav", [0.1, bad])
    with pytest.raises(audio.AudioError, match="Non-finite"):
        audio.prepare_audio(path, tmp_path / "output.wav")
    assert not (tmp_path / "output.wav").exists()


def test_extreme_finite_samples_fail_cleanly_instead_of_json_infinity(tmp_path):
    path = wav_fixture(tmp_path / "extreme.wav", [1e200], bits=64)
    with pytest.raises(audio.AudioError, match="finite numeric range"):
        audio.inspect_audio(path)


def test_four_orphan_bytes_require_exact_identity(tmp_path, monkeypatch):
    path = wav_fixture(tmp_path / "orphan.wav", [[0.1, 0.2]] * 300, extra=False, orphan=b"\0" * 4)
    original = path.read_bytes()
    with pytest.raises(audio.AudioError, match="exact verified"):
        audio.inspect_audio(path)
    # Substitute this bounded synthetic identity solely to exercise the exact
    # same complete-frame branch, without including the private real recording.
    monkeypatch.setattr(audio, "_KNOWN_ORPHAN_SHA256", hashlib.sha256(original).hexdigest())
    monkeypatch.setattr(audio, "_KNOWN_ORPHAN_FILE_SIZE", len(original))
    monkeypatch.setattr(audio, "_KNOWN_ORPHAN_DATA_SIZE", 300 * 8 + 4)
    result = audio.prepare_audio(path, tmp_path / "working.wav")
    assert result["source"]["complete_frames"] == 300
    assert result["derivative_only_discarded_orphan_bytes"] == 4
    assert result["output_frames"] == 100
    assert path.read_bytes() == original
    other = wav_fixture(tmp_path / "other.wav", [[0.1, 0.3]] * 300, extra=False, orphan=b"\0" * 4)
    with pytest.raises(audio.AudioError, match="exact verified"):
        audio.inspect_audio(other)


def test_other_misalignment_and_truncation_rejected(tmp_path):
    path = wav_fixture(tmp_path / "bad.wav", [[0.1, 0.2]] * 3, orphan=b"\0\0")
    with pytest.raises(audio.AudioError, match="misalignment"):
        audio.inspect_audio(path)
    path.write_bytes(path.read_bytes()[:-2])
    with pytest.raises(audio.AudioError, match="size mismatch"):
        audio.inspect_audio(path)


def test_full_sized_supplied_shape_complete_frame_coverage(tmp_path, monkeypatch):
    """Private recording is unnecessary: exact container sizes with sparse silence.

    Only its synthetic hash is substituted. All production repair dimensions,
    channel count, encoding, rate and orphan-byte checks remain unchanged.
    """
    source = tmp_path / "synthetic full-sized stereo.wav"
    frames, data_bytes, size = 31_432_698, 251_461_588, 251_461_632
    header = (b"RIFF" + struct.pack("<I", size - 8) + b"WAVEfmt "
              + struct.pack("<IHHIIHH", 16, 3, 2, 48000, 384000, 8, 32)
              + b"data" + struct.pack("<I", data_bytes))
    with source.open("wb") as handle:
        handle.write(header)
        handle.seek(44 + (frames - 1) * 8)
        handle.write(struct.pack("<ff", 0.25, 0.5))
        # A nonfinite orphan is intentionally not a complete channel pair.
        handle.write(struct.pack("<f", float("nan")))
    synthetic_hash = audio._hash(source)
    monkeypatch.setattr(audio, "_KNOWN_ORPHAN_SHA256", synthetic_hash)
    result = audio.prepare_audio(source, tmp_path / "full derivative.wav")
    assert result["source"]["size_bytes"] == size
    assert result["source"]["data_bytes"] == data_bytes
    assert result["source"]["complete_frames"] == frames
    assert result["input_frame_start"] == 0
    assert result["input_frame_end"] == frames
    assert result["source"]["duration_seconds"] == 654.847875
    assert result["output_frames"] == 10_477_566
    assert result["output_duration_seconds"] == 654.847875
    assert abs(result["duration_error_seconds"]) <= 1 / 16000
    assert samples(tmp_path / "full derivative.wav")[-1] != 0
    assert audio._hash(source) == synthetic_hash


def test_silence_is_json_safe_and_not_amplified(tmp_path):
    path = wav_fixture(tmp_path / "silence.wav", np.zeros((4801, 2)))
    result = audio.prepare_audio(path, tmp_path / "output.wav")
    assert result["gain_db"] == 0
    assert result["source"]["channel_rms_dbfs"] == [None, None]
    assert result["source"]["stereo_correlation"] is None
    assert result["output_frames"] == 1601
    assert not np.any(samples(tmp_path / "output.wav"))
    json.dumps(result, allow_nan=False)


def test_channel_cancellation_and_selection(tmp_path):
    left = np.sin(np.arange(48000) * 2 * np.pi * 1000 / 48000) * 0.3
    path = wav_fixture(tmp_path / "phase.wav", np.column_stack([left, -left]))
    info = audio.inspect_audio(path)
    assert info["cancellation_warning"]
    assert info["cancellation_interval_count"] == 1
    assert info["stereo_correlation"] == pytest.approx(-1)
    mean = audio.prepare_audio(path, tmp_path / "mean.wav")
    assert mean["output_peak_dbfs"] is None
    selected = audio.prepare_audio(path, tmp_path / "left.wav", {"channel": "left"})
    assert selected["output_peak_dbfs"] <= -3.0
    assert selected["output_peak_dbfs"] > -3.01
    mono = wav_fixture(tmp_path / "mono.wav", left)
    with pytest.raises(audio.AudioError, match="Right-channel"):
        audio.prepare_audio(mono, tmp_path / "right.wav", {"channel": "right"})


@pytest.mark.parametrize("rate", [8000, 16000, 22050, 44100, 48000, 96000])
def test_duration_preserved_and_ceiling_safe(tmp_path, rate):
    values = np.sin(np.arange(rate + 7) * 2 * np.pi * 400 / rate) * 0.003
    source = wav_fixture(tmp_path / "source.wav", values, rate=rate)
    original = source.read_bytes()
    result = audio.prepare_audio(source, tmp_path / "working.wav")
    assert result["output_frames"] == int(np.ceil(len(values) * 16000 / rate))
    assert abs(result["duration_error_seconds"]) < 1 / 16000 + 1e-12
    assert result["output_peak_dbfs"] <= -3
    assert result["gain_db"] <= 30
    assert result["limiter"]["enabled"] is False
    assert source.read_bytes() == original
    assert abs(samples(tmp_path / "working.wav").astype(np.int32)).max() < 32767


def test_resampling_rejects_alias_energy(tmp_path):
    rate = 48000
    values = np.sin(np.arange(rate) * 2 * np.pi * 12000 / rate) * 0.3
    source = wav_fixture(tmp_path / "high.wav", values, rate=rate)
    audio.prepare_audio(source, tmp_path / "low.wav", {"gain_db": 0.0})
    output = samples(tmp_path / "low.wav").astype(float) / 32768
    assert np.sqrt(np.mean(output[100:-100] ** 2)) < 0.001


def test_interval_uses_full_source_gain_and_source_frames(tmp_path):
    rate = 48000
    values = np.sin(np.arange(rate * 2) * 2 * np.pi * 500 / rate) * 0.01
    values[:rate] *= 20
    path = wav_fixture(tmp_path / "levels.wav", values, rate=rate)
    full = audio.prepare_audio(path, tmp_path / "full.wav")
    excerpt = audio.prepare_audio(path, tmp_path / "excerpt.wav", interval=(1.2, 1.8))
    assert excerpt["gain_db"] == full["gain_db"]
    assert excerpt["input_frame_start"] == 57600
    assert excerpt["input_frame_end"] == 86400
    assert excerpt["output_frames"] == 9600
    assert excerpt["output_peak_dbfs"] < full["output_peak_dbfs"] - 20


def test_slice_is_sample_exact_and_never_overwrites(tmp_path):
    source = wav_fixture(tmp_path / "source.wav", np.sin(np.arange(48000) / 17) * 0.1)
    audio.prepare_audio(source, tmp_path / "full.wav")
    result = audio.slice_pcm16(tmp_path / "full.wav", tmp_path / "slice.wav", 0.123, 0.456)
    assert result["timestamp_offset_seconds"] == 0.123
    assert result["output_frames"] == 5328
    assert np.array_equal(samples(tmp_path / "slice.wav"), samples(tmp_path / "full.wav")[1968:7296])
    with pytest.raises(FileExistsError):
        audio.slice_pcm16(tmp_path / "full.wav", tmp_path / "slice.wav", 0.1, 0.2)
    with pytest.raises(FileExistsError):
        audio.prepare_audio(source, tmp_path / "full.wav")


def test_rare_transient_limiter_and_activity_cap(tmp_path):
    rate = 16000
    values = np.sin(np.arange(rate * 10) * 2 * np.pi * 330 / rate) * 0.01
    values[rate * 5] = 0.5
    path = wav_fixture(tmp_path / "transient.wav", values, rate=rate)
    result = audio.prepare_audio(path, tmp_path / "limited.wav", {"candidate": "B", "gain_db": 21})
    limiter = result["limiter"]
    assert limiter["enabled"]
    assert limiter["max_gain_reduction_db"] > 10
    assert limiter["latency_samples"] == 0
    assert limiter["automatic_makeup_gain"] is False
    assert limiter["significantly_limited_active_fraction"] <= 0.01
    assert limiter["limited_active_fraction"] <= 0.01
    assert result["output_peak_dbfs"] <= -3
    assert result["output_frames"] == len(values)
    assert result["gain_db"] == pytest.approx(21)


def test_limiter_reduces_gain_for_sustained_activity(tmp_path):
    values = np.sin(np.arange(32000) * 2 * np.pi * 300 / 16000) * 0.2
    path = wav_fixture(tmp_path / "ordinary.wav", values, rate=16000)
    result = audio.prepare_audio(path, tmp_path / "limited.wav", {"candidate": "B", "gain_db": 21})
    assert result["gain_db"] < 13
    assert result["limiter"]["gain_reduced_for_activity"]
    assert result["limiter"]["significantly_limited_active_fraction"] <= 0.01
    assert result["limiter"]["limited_active_fraction"] <= 0.01
    assert result["output_peak_dbfs"] <= -3


def test_b_default_gain_is_not_generalized(tmp_path):
    path = wav_fixture(tmp_path / "other.wav", [0.01] * 48000)
    with pytest.raises(audio.AudioError, match="specific to the verified supplied"):
        audio.prepare_audio(path, tmp_path / "output.wav", {"candidate": "B"})


@pytest.mark.parametrize("policy", [{"vad": True}, {"filtering": "highpass"}, {"magic": 1},
                                    {"gain_db": float("nan")}, {"peak_dbfs": 0},
                                    {"max_gain_db": True}, {"channel": "auto"}])
def test_invalid_policy_rejected(tmp_path, policy):
    path = wav_fixture(tmp_path / "source.wav", [0.1] * 480)
    with pytest.raises(audio.AudioError):
        audio.prepare_audio(path, tmp_path / "output.wav", policy)
    assert not (tmp_path / "output.wav").exists()


@pytest.mark.parametrize("interval", [(-1, 1), (0, 2), (0.5, 0.4), (0, float("nan")), (0, 0.000001)])
def test_invalid_interval_rejected(tmp_path, interval):
    path = wav_fixture(tmp_path / "source.wav", [0.1] * 48000)
    with pytest.raises(audio.AudioError):
        audio.prepare_audio(path, tmp_path / "output.wav", interval=interval)
    assert not (tmp_path / "output.wav").exists()


def test_clipping_indicators_do_not_clip_float_source(tmp_path):
    path = wav_fixture(tmp_path / "hot.wav", [0.5, 1.1, -1.2] * 100, rate=16000)
    info = audio.inspect_audio(path)
    assert info["channel_full_scale_sample_count"] == [200]
    result = audio.prepare_audio(path, tmp_path / "safe.wav")
    assert result["gain_db"] < 0
    assert result["output_peak_dbfs"] <= -3


def test_no_complete_frames_rejected(tmp_path):
    path = wav_fixture(tmp_path / "empty.wav", np.zeros((0, 2)))
    with pytest.raises(audio.AudioError, match="no complete"):
        audio.inspect_audio(path)


def test_malformed_chunk_and_duplicate_data_rejected(tmp_path):
    path = wav_fixture(tmp_path / "chunk.wav", [0.1] * 10, extra=False)
    raw = bytearray(path.read_bytes())
    struct.pack_into("<I", raw, 40, 10_000)
    path.write_bytes(raw)
    with pytest.raises(audio.AudioError, match="beyond"):
        audio.inspect_audio(path)
    path = wav_fixture(path, [0.1] * 10, extra=False)
    raw = bytearray(path.read_bytes())
    raw += b"data" + struct.pack("<I", 4) + b"\0" * 4
    struct.pack_into("<I", raw, 4, len(raw) - 8)
    path.write_bytes(raw)
    with pytest.raises(audio.AudioError, match="Multiple"):
        audio.inspect_audio(path)
