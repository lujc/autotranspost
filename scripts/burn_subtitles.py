#!/usr/bin/env python3
"""Burn one ASS subtitle track into a high-quality H.264 (default) / H.265(HEVC) / AV1 MP4."""

from __future__ import annotations

import argparse
from collections import deque
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any, Sequence


DEFAULT_ENCODER = "h264_nvenc"
PROGRESS_BAR_WIDTH = 20
PROGRESS_STEP_PERCENT = 5
MP4_COPY_AUDIO_CODECS = frozenset({"aac", "ac3", "alac", "eac3", "mp3", "opus"})
HDR_TRANSFERS = frozenset({"arib-std-b67", "smpte2084"})
HDR_SIDE_DATA = (
    "content light level",
    "dolby vision",
    "dovi",
    "dynamic hdr",
    "hdr10+",
    "mastering display",
)
FFMPEG_FULL_CANDIDATES = (
    Path("/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg"),
    Path("/usr/local/opt/ffmpeg-full/bin/ffmpeg"),
)


class BurnError(RuntimeError):
    """A user-actionable burn or verification failure."""


def _positive_crf(value: str) -> int:
    try:
        crf = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("CRF must be an integer from 0 to 51") from exc
    if not 0 <= crf <= 51:
        raise argparse.ArgumentTypeError("CRF must be an integer from 0 to 51")
    return crf


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Burn an ASS subtitle file exactly once into an H.264 (default) / H.265(HEVC) / "
            "AV1 MP4 while preserving the source dimensions and frame timing."
        )
    )
    parser.add_argument("video", type=Path, help="input video")
    parser.add_argument("subtitle", type=Path, help="input ASS subtitle file")
    parser.add_argument("output", type=Path, help="output MP4")
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace OUTPUT if it already exists",
    )
    parser.add_argument(
        "--crf",
        type=_positive_crf,
        default=18,
        help="constant-rate-factor quality (H.264: 0-51, SVT-AV1: 0-63) (default: 18)",
    )
    parser.add_argument(
        "--preset",
        default="slow",
        help="encoder preset (default: slow; for SVT-AV1 use integer 0-13)",
    )
    parser.add_argument(
        "--encoder",
        default=DEFAULT_ENCODER,
        help=f"FFmpeg video encoder (default: {DEFAULT_ENCODER})",
    )
    parser.add_argument(
        "--validation-report",
        type=Path,
        help="subtitle validation JSON (default: validation.json next to the ASS file)",
    )
    parser.add_argument(
        "--allow-missing-font",
        action="store_true",
        help="continue with libass font substitution when the validated font is not installed",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help=(
            "After a successful burn, automatically upload the MP4 to Bilibili "
            "(login via QR if no cached credential). This is the default/recommended "
            "behavior; omit only when you want a local-only burn."
        ),
    )
    parser.add_argument(
        "--cn-title",
        default="",
        help="Chinese title for the Bilibili upload (used with --publish).",
    )
    return parser


def _required_executables() -> tuple[str, str]:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    missing = [name for name, path in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)) if not path]
    if missing:
        raise BurnError(f"required executable not found in PATH: {', '.join(missing)}")
    assert ffmpeg is not None and ffprobe is not None
    ffmpeg = _select_libass_ffmpeg(ffmpeg)
    sibling_ffprobe = Path(ffmpeg).with_name("ffprobe")
    if sibling_ffprobe.is_file():
        ffprobe = str(sibling_ffprobe)
    return ffmpeg, ffprobe


def _ffmpeg_has_subtitles_filter(ffmpeg: str | Path) -> bool:
    result = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-filters"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    return result.returncode == 0 and any(
        len(fields := line.split()) >= 2 and fields[1] == "subtitles"
        for line in result.stdout.splitlines()
    )


def _select_libass_ffmpeg(
    default: str,
    *,
    candidates: Sequence[Path] = FFMPEG_FULL_CANDIDATES,
) -> str:
    for candidate in (Path(default), *candidates):
        if candidate.is_file() and _ffmpeg_has_subtitles_filter(candidate):
            return str(candidate)
    return default


def _require_libass_subtitles_filter(ffmpeg: str) -> None:
    if not _ffmpeg_has_subtitles_filter(ffmpeg):
        raise BurnError(
            "FFmpeg has no usable 'subtitles' filter; install an FFmpeg build "
            "with libass support"
        )


def _resolve_encoder(ffmpeg: str, requested: str) -> str:
    """Return an encoder FFmpeg actually supports, with graceful fallback.

    NVENC hardware encoders (hevc_nvenc / h264_nvenc) are preferred for speed and
    small files; if unavailable we fall back to libx265 / libx264 so the burn
    still succeeds on machines without an NVIDIA GPU.
    """

    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        listing = result.stdout
    except OSError:
        return requested

    def have(name: str) -> bool:
        return name in listing

    if have(requested):
        return requested
    if "nvenc" in requested:
        if "hevc" in requested and have("libx265"):
            return "libx265"
        if "h264" in requested and have("libx264"):
            return "libx264"
    if have("libx265"):
        return "libx265"
    if have("libx264"):
        return "libx264"
    return requested


def _auto_publish(output_path: Path, cn_title: str) -> None:
    """Upload + submit the burned MP4 to Bilibili after a successful burn.

    Requires a cached Bilibili login; if none, runs the QR login flow (which
    writes ``cache/bilibili_qr.png`` for the agent to show the user) and polls
    until the user scans. Network egress to Bilibili must be allowed — run with
    the sandbox disabled if the sandbox blocks it. Any publish failure is
    reported but never undoes the local burn.
    """

    publish_script = Path(__file__).resolve().with_name("publish_bilibili.py")
    if not publish_script.is_file():
        print("warning: publish_bilibili.py not found; skipping auto-upload", file=sys.stderr)
        return
    job = output_path.parent
    py = sys.executable
    try:
        status = subprocess.run(
            [py, str(publish_script), "status"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        need_login = (
            status.returncode != 0
            or "未登录" in status.stdout
            or "失效" in status.stdout
        )
        if need_login:
            qr_path = Path(__file__).resolve().parent.parent / "cache" / "bilibili_qr.png"
            print(
                f"未登录 B 站，启动二维码登录：请用 B 站 App 扫描 {qr_path}",
                file=sys.stderr,
            )
            login = subprocess.run(
                [py, str(publish_script), "login"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if login.stdout:
                print(login.stdout, file=sys.stderr)
            if login.stderr:
                print(login.stderr, file=sys.stderr)
            if login.returncode != 0:
                print("error: B 站登录失败，未上传。请手动登录后重试。", file=sys.stderr)
                return
        cmd = [py, str(publish_script), "publish", "--job", str(job), "--video", str(output_path)]
        if cn_title:
            cmd += ["--cn-title", cn_title]
        res = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False
        )
        if res.stdout:
            print(res.stdout, file=sys.stderr)
        if res.stderr:
            print(res.stderr, file=sys.stderr)
        if res.returncode != 0:
            print(
                "error: B 站上传/投稿失败（视频已生成在本地）。请检查网络或手动重试。",
                file=sys.stderr,
            )
    except Exception as exc:  # never let publish failure undo a good burn
        print(f"warning: auto-publish skipped due to error: {exc}", file=sys.stderr)


def _last_error_line(stderr: str) -> str:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    return f": {lines[-1]}" if lines else ""


def _clock(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _format_progress(
    percent: int,
    encoded_seconds: float,
    duration: float,
    speed: str,
) -> str:
    percent = max(0, min(100, int(percent)))
    filled = round(percent * PROGRESS_BAR_WIDTH / 100)
    bar = "█" * filled + "░" * (PROGRESS_BAR_WIDTH - filled)
    speed = speed.strip() or "--"
    return (
        f"烧录 [{bar}] {percent:3d}%  "
        f"{_clock(encoded_seconds)} / {_clock(duration)}  {speed}"
    )


def _progress_seconds(values: dict[str, str]) -> float:
    raw = values.get("out_time_us") or values.get("out_time_ms")
    if raw:
        try:
            return max(0.0, int(raw) / 1_000_000)
        except ValueError:
            pass
    clock = values.get("out_time", "")
    try:
        hours, minutes, seconds = clock.split(":", 2)
        return max(0.0, int(hours) * 3600 + int(minutes) * 60 + float(seconds))
    except (TypeError, ValueError):
        return 0.0


def _run_ffmpeg_with_progress(command: Sequence[str], duration: float) -> tuple[int, str]:
    process = subprocess.Popen(
        list(command),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    if process.stdout is None:
        process.kill()
        raise BurnError("FFmpeg progress pipe was not available")

    values: dict[str, str] = {}
    diagnostics: deque[str] = deque(maxlen=12)
    last_bucket = 0
    print(_format_progress(0, 0, duration, "--"), file=sys.stderr, flush=True)
    for raw_line in process.stdout:
        line = raw_line.strip()
        if not line:
            continue
        if "=" not in line:
            diagnostics.append(line)
            continue
        key, value = line.split("=", 1)
        values[key] = value
        if key != "progress":
            continue

        encoded_seconds = _progress_seconds(values)
        raw_percent = 100 * encoded_seconds / duration if duration > 0 else 0
        bucket = min(
            100,
            int(raw_percent // PROGRESS_STEP_PERCENT) * PROGRESS_STEP_PERCENT,
        )
        if value == "end":
            bucket = 100
            encoded_seconds = duration
        if bucket > last_bucket:
            print(
                _format_progress(
                    bucket,
                    encoded_seconds,
                    duration,
                    values.get("speed", "--"),
                ),
                file=sys.stderr,
                flush=True,
            )
            last_bucket = bucket

    return process.wait(), "\n".join(diagnostics)


def _probe(ffprobe: str, path: Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise BurnError(f"ffprobe could not read {path}{_last_error_line(result.stderr)}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BurnError(f"ffprobe returned invalid JSON for {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise BurnError(f"ffprobe returned an unexpected result for {path}")
    return data


def _streams(probe: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    streams = probe.get("streams", [])
    if not isinstance(streams, list):
        return []
    return [
        stream
        for stream in streams
        if isinstance(stream, dict) and stream.get("codec_type") == kind
    ]


def _main_video_stream(probe: dict[str, Any]) -> dict[str, Any]:
    videos = _streams(probe, "video")
    if not videos:
        raise BurnError("input contains no video stream")
    return next(
        (
            stream
            for stream in videos
            if not bool((stream.get("disposition") or {}).get("attached_pic"))
        ),
        videos[0],
    )


def _stream_dimensions(stream: dict[str, Any]) -> tuple[int, int]:
    try:
        width = int(stream["width"])
        height = int(stream["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise BurnError("video stream has no valid dimensions") from exc
    if width <= 0 or height <= 0:
        raise BurnError("video stream has no valid dimensions")
    return width, height


def _duration(probe: dict[str, Any]) -> float:
    candidates: list[Any] = []
    file_format = probe.get("format")
    if isinstance(file_format, dict):
        candidates.append(file_format.get("duration"))
    for stream in probe.get("streams", []):
        if isinstance(stream, dict):
            candidates.append(stream.get("duration"))

    durations: list[float] = []
    for candidate in candidates:
        try:
            duration = float(candidate)
        except (TypeError, ValueError):
            continue
        if math.isfinite(duration) and duration > 0:
            durations.append(duration)
    return max(durations, default=0.0)


def _frame_rate(stream: dict[str, Any]) -> Fraction | None:
    for key in ("avg_frame_rate", "r_frame_rate"):
        value = stream.get(key)
        if not isinstance(value, str):
            continue
        try:
            rate = Fraction(value)
        except (ValueError, ZeroDivisionError):
            continue
        if rate > 0:
            return rate
    return None


def _is_hdr(stream: dict[str, Any]) -> bool:
    if str(stream.get("color_transfer", "")).lower() in HDR_TRANSFERS:
        return True

    if str(stream.get("color_primaries", "")).lower() == "bt2020":
        try:
            bit_depth = int(stream.get("bits_per_raw_sample", 0))
        except (TypeError, ValueError):
            bit_depth = 0
        pixel_format = str(stream.get("pix_fmt", "")).lower()
        if bit_depth >= 10 or re.search(r"(?:10|12|14|16)(?:le|be)?$", pixel_format):
            return True

    side_data = stream.get("side_data_list")
    if isinstance(side_data, list):
        for item in side_data:
            if not isinstance(item, dict):
                continue
            description = " ".join(str(value).lower() for value in item.values())
            if any(marker in description for marker in HDR_SIDE_DATA):
                return True
    return False


def _escape_filter_value(value: str) -> str:
    """Escape a value through FFmpeg's option and filtergraph parser layers."""

    def escape(text: str, special: str) -> str:
        return "".join(f"\\{char}" if char in special else char for char in text)

    option_escaped = escape(value, "\\':")
    return escape(option_escaped, "\\'[],;")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_validation_report(subtitle: Path, report_path: Path) -> dict[str, Any]:
    subtitle = subtitle.expanduser().resolve()
    report_path = report_path.expanduser().resolve()
    if not report_path.is_file():
        raise BurnError(f"validation report does not exist or is not a file: {report_path}")

    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BurnError(f"validation report is not valid UTF-8 JSON: {report_path}: {exc}") from exc
    if not isinstance(report, dict):
        raise BurnError("validation report root must be a JSON object")
    if report.get("structurally_valid") is not True:
        raise BurnError("validation report must declare structurally_valid=true")
    if report.get("validation_scope") != "structural_source_integrity":
        raise BurnError(
            "validation report scope must be structural_source_integrity"
        )

    segment_count = report.get("segment_count")
    translation_count = report.get("translation_count")
    counts = (segment_count, translation_count)
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in counts):
        raise BurnError(
            "validation report segment_count and translation_count must be positive integers"
        )
    if segment_count != translation_count:
        raise BurnError(
            "validation report segment_count and translation_count must be equal"
        )

    outputs = report.get("outputs")
    if not isinstance(outputs, dict):
        raise BurnError("validation report must include an 'outputs' checksum map")
    recorded_hash = outputs.get(subtitle.name)
    if not isinstance(recorded_hash, str) or not re.fullmatch(
        r"[0-9a-fA-F]{64}", recorded_hash
    ):
        raise BurnError(
            f"validation report outputs[{subtitle.name!r}] must be a SHA-256 checksum"
        )
    if _sha256_file(subtitle) != recorded_hash.lower():
        raise BurnError(f"{subtitle.name} SHA-256 does not match the validation report")
    return report


_FONT_FILE_SUFFIXES = frozenset({".ttf", ".otf", ".ttc"})
_FONT_DIRECTORIES = (
    "~/Library/Fonts",
    "/Library/Fonts",
    "/System/Library/Fonts",
    "~/.fonts",
    "~/.local/share/fonts",
    "/usr/share/fonts",
    "/usr/local/share/fonts",
)


def _font_installed(family: str) -> bool | None:
    """Return True/False when detection is trustworthy, None when unavailable.

    libass silently substitutes another font when the requested family is
    missing, which would pass every later gate with the wrong deliverable.
    """

    fc_list = shutil.which("fc-list")
    if fc_list:
        result = subprocess.run(
            [fc_list, ":", "family"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            needle = family.casefold()
            return any(
                needle in entry.strip().casefold()
                for line in result.stdout.splitlines()
                for entry in line.split(",")
            )
    token = re.sub(r"[\s_-]+", "", family).casefold()
    if not token:
        return None
    searched = False
    for directory in _FONT_DIRECTORIES:
        base = Path(directory).expanduser()
        if not base.is_dir():
            continue
        searched = True
        for path in base.rglob("*"):
            if (
                path.suffix.lower() in _FONT_FILE_SUFFIXES
                and token in re.sub(r"[\s_-]+", "", path.stem).casefold()
            ):
                return True
    return False if searched else None


def _require_subtitle_font(report: dict[str, Any], *, allow_missing_font: bool) -> None:
    font = str(report.get("font") or "").strip()
    if not font:
        return
    installed = _font_installed(font)
    if installed is True:
        return
    if installed is None:
        print(
            f"warning: could not verify that font {font!r} is installed; "
            "libass substitutes missing fonts silently",
            file=sys.stderr,
        )
        return
    message = (
        f"font {font!r} required by the validated subtitles was not found; install it "
        "(MiSans: https://hyperos.mi.com/font/zh/download/)"
    )
    if allow_missing_font:
        print(f"warning: {message}; continuing with libass substitution", file=sys.stderr)
        return
    raise BurnError(f"{message} or pass --allow-missing-font to accept substitution")


def _audio_options(audio_streams: Sequence[dict[str, Any]]) -> tuple[list[str], list[str]]:
    if not audio_streams:
        return [], []

    options = ["-c:a", "copy"]
    modes: list[str] = []
    for output_index, stream in enumerate(audio_streams):
        codec = str(stream.get("codec_name", "")).lower()
        if codec in MP4_COPY_AUDIO_CODECS:
            modes.append(f"audio {output_index}: copied {codec}")
            continue
        options.extend(
            [
                f"-c:a:{output_index}",
                "aac",
                f"-b:a:{output_index}",
                "256k",
            ]
        )
        modes.append(f"audio {output_index}: {codec or 'unknown'} -> AAC")
    return options, modes


def _encode_command(
    ffmpeg: str,
    video: Path,
    subtitle: Path,
    output: Path,
    video_stream: dict[str, Any],
    audio_streams: Sequence[dict[str, Any]],
    *,
    force: bool,
    crf: int,
    preset: str,
    encoder: str,
    video_bitrate_cap: int = 0,
    gop_frames: int = 0,
) -> tuple[list[str], list[str]]:
    try:
        stream_index = int(video_stream["index"])
    except (KeyError, TypeError, ValueError) as exc:
        raise BurnError("input video stream has no valid index") from exc

    audio_options, audio_modes = _audio_options(audio_streams)
    subtitle_filter = f"subtitles=filename={_escape_filter_value(str(subtitle))}"

    # NVENC encoders use -cq instead of -crf, and integer presets
    is_nvenc = "nvenc" in encoder.lower()
    if is_nvenc:
        preset_arg = str(int(preset) if preset.isdigit() else 7)  # default p7=best quality
    else:
        preset_arg = preset

    # When a bitrate cap is requested we MUST use bitrate-targeted VBR with a hard
    # -maxrate/-bufsize ceiling. The constant-quality modes (-cq for NVENC, -crf
    # for SVT) ignore -b:v and will blow past the cap to reach the quality target
    # (observed: -rc vbr -cq 25 produced 5.4 Mbps vs a 857 kbps cap).
    if video_bitrate_cap and video_bitrate_cap > 0:
        cap = max(100_000, int(video_bitrate_cap))
        if is_nvenc:
            quality_args = [
                "-rc", "vbr",
                "-b:v", str(cap),
                "-maxrate", str(cap),
                "-bufsize", str(cap * 2),
            ]
        else:
            quality_args = [
                "-b:v", str(cap),
                "-maxrate", str(cap),
                "-bufsize", str(cap * 2),
            ]
        print(
            f"info: capping output video bitrate to {cap // 1000} kbps "
            f"(<= source to avoid bitrate bloat)",
            file=sys.stderr,
        )
    elif is_nvenc:
        quality_args = ["-rc", "vbr", "-cq", str(crf)]
    else:
        quality_args = ["-crf", str(crf)]

    # Seekability (mandatory): force a periodic keyframe interval on EVERY encode.
    # NVENC in particular defaults to an effectively infinite GOP (only one IDR at
    # frame 0), which makes the file unseekable ("cannot drag to scrub"). Software
    # encoders usually pick a sane default (~250) but we set it explicitly so this
    # class of bug can never recur regardless of encoder. gop_frames is ~2s of
    # video. For NVENC we also request -forced-idr so the periodic keyframes are
    # real IDR frames a player can seek to.
    if gop_frames and gop_frames > 0:
        gop_args = ["-g", str(gop_frames), "-keyint_min", str(gop_frames)]
        if is_nvenc:
            gop_args += ["-forced-idr", "1"]
    else:
        # Fallback when fps is unknown: still cap the GOP so the file stays seekable.
        gop_args = ["-g", "120", "-keyint_min", "120"]
        if is_nvenc:
            gop_args += ["-forced-idr", "1"]

    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostats",
        "-stats_period",
        "1",
        "-progress",
        "pipe:1",
        "-y" if force else "-n",
        "-i",
        str(video),
        "-map",
        f"0:{stream_index}",
        "-map",
        "0:a?",
        "-map_metadata",
        "0",
        "-map_chapters",
        "0",
        "-sn",
        "-dn",
        "-vf",
        subtitle_filter,
        "-fps_mode:v:0",
        "passthrough",
        "-c:v",
        encoder,
        *quality_args,
        *gop_args,
        "-preset",
        preset_arg,
        "-pix_fmt",
        "yuv420p",
        *audio_options,
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(output),
    ]
    return command, audio_modes


def _verify_output(
    ffprobe: str,
    output: Path,
    input_video: dict[str, Any],
    input_had_audio: bool,
    *,
    input_duration: float,
) -> None:
    result = _probe(ffprobe, output)
    file_format = result.get("format")
    format_name = file_format.get("format_name", "") if isinstance(file_format, dict) else ""
    if "mp4" not in str(format_name).split(","):
        raise BurnError(f"output verification failed: container is not MP4 ({format_name or 'unknown'})")

    output_videos = _streams(result, "video")
    if not output_videos:
        raise BurnError("output verification failed: no video stream")
    output_video = output_videos[0]
    if output_video.get("codec_name") not in ("h264", "av1", "hevc"):
        raise BurnError(
            "output verification failed: video codec is "
            f"{output_video.get('codec_name', 'unknown')}, not H.264, H.265/HEVC or AV1"
        )
    output_duration = _duration(result)
    if output_duration <= 0:
        raise BurnError("output verification failed: duration is zero or unavailable")
    if input_duration <= 0:
        raise BurnError("output verification failed: input duration is zero or unavailable")
    duration_tolerance = max(0.5, input_duration * 0.01)
    if abs(output_duration - input_duration) > duration_tolerance:
        raise BurnError(
            "output verification failed: duration changed from "
            f"{input_duration:.3f}s to {output_duration:.3f}s "
            f"(allowed difference {duration_tolerance:.3f}s)"
        )

    input_dimensions = _stream_dimensions(input_video)
    output_dimensions = _stream_dimensions(output_video)
    if output_dimensions != input_dimensions:
        raise BurnError(
            "output verification failed: dimensions changed from "
            f"{input_dimensions[0]}x{input_dimensions[1]} to "
            f"{output_dimensions[0]}x{output_dimensions[1]}"
        )

    input_rate = _frame_rate(input_video)
    output_rate = _frame_rate(output_video)
    if input_rate is not None and output_rate is not None:
        relative_drift = abs(float(output_rate - input_rate)) / float(input_rate)
        if relative_drift > 0.005:
            raise BurnError(
                "output verification failed: frame rate changed from "
                f"{float(input_rate):.6g} to {float(output_rate):.6g} fps"
            )

    if input_had_audio and not _streams(result, "audio"):
        raise BurnError("output verification failed: input audio is missing from output")


def burn_subtitles(
    video: Path,
    subtitle: Path,
    output: Path,
    *,
    force: bool = False,
    crf: int = 18,
    preset: str = "slow",
    encoder: str = DEFAULT_ENCODER,
    validation_report: Path | None = None,
    allow_missing_font: bool = False,
    publish: bool = False,
    cn_title: str = "",
) -> list[str]:
    video = video.expanduser().resolve()
    subtitle = subtitle.expanduser().resolve()
    output = output.expanduser().resolve()
    report_path = (
        validation_report.expanduser().resolve()
        if validation_report is not None
        else subtitle.with_name("validation.json")
    )

    if not video.is_file():
        raise BurnError(f"input video does not exist or is not a file: {video}")
    if not subtitle.is_file():
        raise BurnError(f"ASS subtitle does not exist or is not a file: {subtitle}")
    if subtitle.suffix.lower() != ".ass":
        raise BurnError(f"subtitle must be an .ass file: {subtitle}")
    if output in (video, subtitle, report_path):
        raise BurnError("output must be different from all input files")
    if not output.parent.is_dir():
        raise BurnError(f"output directory does not exist: {output.parent}")
    if output.exists() and not force:
        raise BurnError(f"output already exists (use --force to replace it): {output}")
    if output.exists() and not output.is_file():
        raise BurnError(f"output exists and is not a regular file: {output}")
    if not preset.strip():
        raise BurnError("encoder preset cannot be empty")
    if not encoder.strip():
        raise BurnError("encoder cannot be empty")

    report = _validate_validation_report(subtitle, report_path)
    _require_subtitle_font(report, allow_missing_font=allow_missing_font)

    ffmpeg, ffprobe = _required_executables()
    _require_libass_subtitles_filter(ffmpeg)
    encoder = _resolve_encoder(ffmpeg, encoder)

    input_probe = _probe(ffprobe, video)
    input_video = _main_video_stream(input_probe)
    _stream_dimensions(input_video)
    input_duration = _duration(input_probe)
    if input_duration <= 0:
        raise BurnError("input duration is zero or unavailable")
    audio_streams = _streams(input_probe, "audio")

    # --- Output bitrate cap (<= source) ------------------------------------------
    # The final encoded (variable) bitrate must never exceed the original video's
    # bitrate. We cap the VIDEO stream so that video + audio <= source total.
    # Audio is copied losslessly (see _audio_options), so its bitrate stays at the
    # source value and the total stays bounded automatically.
    def _int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    video_bitrate_cap = 0
    src_format = input_probe.get("format")
    source_total = _int(src_format.get("bit_rate")) if isinstance(src_format, dict) else 0
    source_video_br = _int(input_video.get("bit_rate"))
    source_audio_br = _int(audio_streams[0].get("bit_rate")) if audio_streams else 0
    if source_video_br > 0:
        # Direct: keep the video stream at/below the original video bitrate.
        video_bitrate_cap = max(100_000, int(source_video_br * 0.95))
    elif source_total > 0:
        # Fallback: budget the video as (total - audio), leaving a 3% margin.
        audio_budget = source_audio_br if source_audio_br > 0 else 128_000
        video_bitrate_cap = max(100_000, int((source_total - audio_budget) * 0.97))

    # --- Periodic keyframe interval (seekability) -------------------------------
    # Without an explicit GOP length NVENC emits a single IDR at frame 0, making
    # the file effectively unseekable ("cannot drag to scrub"). We enforce a
    # periodic keyframe interval on EVERY encode (~2s of video) so this class of
    # bug can never recur regardless of encoder.
    fps = _frame_rate(input_video)
    gop_frames = 0
    if fps and fps > 0:
        gop_frames = max(24, round(fps * 2))
        print(
            f"info: forcing keyframe interval -g {gop_frames} "
            f"(~2s @ {float(fps):.2f} fps) for seekable output",
            file=sys.stderr,
        )
    else:
        print(
            "info: source fps unknown; forcing keyframe interval -g 120 "
            "for seekable output",
            file=sys.stderr,
        )

    if _is_hdr(input_video):
        print(
            "warning: HDR input detected. The compatibility H.264/yuv420p output is "
            "intended for SDR playback; HDR metadata and appearance may not be preserved.",
            file=sys.stderr,
        )

    command, audio_modes = _encode_command(
        ffmpeg,
        video,
        subtitle,
        output,
        input_video,
        audio_streams,
        force=force,
        crf=crf,
        preset=preset,
        encoder=encoder,
        video_bitrate_cap=video_bitrate_cap,
        gop_frames=gop_frames,
    )
    returncode, diagnostic = _run_ffmpeg_with_progress(command, input_duration)
    if returncode != 0:
        if output.is_file():
            output.unlink()
        detail = _last_error_line(diagnostic)
        raise BurnError(f"FFmpeg subtitle burn failed with exit code {returncode}{detail}")

    try:
        _verify_output(
            ffprobe,
            output,
            input_video,
            bool(audio_streams),
            input_duration=input_duration,
        )
    except BurnError:
        if output.is_file():
            output.unlink()
        raise
    if publish:
        _auto_publish(output, cn_title)
    return audio_modes


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        audio_modes = burn_subtitles(
            args.video,
            args.subtitle,
            args.output,
            force=args.force,
            crf=args.crf,
            preset=args.preset,
            encoder=args.encoder,
            validation_report=args.validation_report,
            allow_missing_font=args.allow_missing_font,
            publish=args.publish,
            cn_title=args.cn_title or "",
        )
    except (BurnError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(
        "validated ASS was burned into verified MP4: "
        f"{args.output.expanduser().resolve()}"
    )
    for mode in audio_modes:
        print(mode)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
