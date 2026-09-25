# Third-party components

The repository's original orchestration, interface and tests use the MIT license
in LICENSE. Upstream projects retain their own licenses. No model weights,
Whisper source tree, FFmpeg binary, Python environment or compiled application
is included in this source repository.

| Component | How it is used | Upstream licensing information |
| --- | --- | --- |
| whisper.cpp | Separately downloaded and built local CLI | [MIT license](https://github.com/ggml-org/whisper.cpp/blob/master/LICENSE) |
| OpenAI Whisper | Model architecture and weights, distributed separately | [MIT license](https://github.com/openai/whisper/blob/main/LICENSE) |
| imageio-ffmpeg | Local build helper obtains a platform-specific FFmpeg executable | [Wrapper license](https://github.com/imageio/imageio-ffmpeg/blob/main/LICENSE) |
| FFmpeg | Separate local media-decoder process | [Build-dependent licensing](https://ffmpeg.org/legal.html) |
| NumPy, SciPy, PyYAML, pytest, CMake | Installed Python/build dependencies | Licenses included in their upstream distributions |

The wrapper's license does not replace the license of its bundled FFmpeg
executable. If distributing a compiled app or bundled dependencies, inspect
the exact binaries, retain their notices and meet their source-distribution
requirements. The source-only release here does not redistribute those binaries.
