from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

import imageio_ffmpeg
from google import genai

VIDEO_MODEL = os.getenv("GEMINI_VIDEO_MODEL", "gemini-omni-flash-preview")
POLL_SECONDS = 5
NETWORK_ATTEMPTS = 5
VIDEO_ATTEMPTS = 3


def ffmpeg() -> str:
    return str(imageio_ffmpeg.get_ffmpeg_exe())


def run_ffmpeg(args: list[str], label: str) -> None:
    result = subprocess.run(
        [ffmpeg(), "-hide_banner", "-loglevel", "error", *args],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise RuntimeError(f"{label} failed: {detail}")


def duration(path: Path) -> float:
    result = subprocess.run(
        [ffmpeg(), "-hide_banner", "-i", str(path)],
        text=True,
        capture_output=True,
        check=False,
    )
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
    if not match:
        raise RuntimeError(f"Could not read video duration: {path.name}")
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def video_size(path: Path) -> tuple[int, int]:
    result = subprocess.run(
        [ffmpeg(), "-hide_banner", "-i", str(path)],
        text=True,
        capture_output=True,
        check=False,
    )
    match = re.search(r"Video:.*?\b(\d{2,5})x(\d{2,5})\b", result.stderr)
    if not match:
        raise RuntimeError(f"Could not read video dimensions: {path.name}")
    return int(match.group(1)), int(match.group(2))


def has_audio(path: Path) -> bool:
    result = subprocess.run(
        [ffmpeg(), "-hide_banner", "-i", str(path)],
        text=True,
        capture_output=True,
        check=False,
    )
    return re.search(r"Stream #\S+: Audio:", result.stderr) is not None


def aspect_ratio_for(path: Path) -> str:
    width, height = video_size(path)
    return "9:16" if height > width else "16:9"


def aspect_matches(path: Path, expected: str, tolerance: float = 0.03) -> bool:
    width, height = video_size(path)
    actual = width / height
    target = 9 / 16 if expected == "9:16" else 16 / 9
    return abs(actual - target) / target <= tolerance


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def state_name(value: Any) -> str:
    name = getattr(value, "name", None)
    return str(name or value or "UNKNOWN").rsplit(".", 1)[-1].upper()


def transient(exc: Exception) -> bool:
    message = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in message for marker in (
        "timeout", "disconnected", "connection reset", "429", "500", "502",
        "503", "504", "jsondecodeerror", "expecting value", "ssl",
        "unexpected_eof", "eof occurred", "connecterror", "connect error",
        "connection refused", "remoteprotocolerror", "readerror",
    ))


async def call(label: str, fn: Callable[..., Any], **kwargs: Any) -> Any:
    for attempt in range(1, NETWORK_ATTEMPTS + 1):
        try:
            return await asyncio.to_thread(fn, **kwargs)
        except Exception as exc:
            if attempt == NETWORK_ATTEMPTS or not transient(exc):
                raise
            await asyncio.sleep(min(8 * attempt, 30))
    raise RuntimeError(f"{label} failed")


async def upload_ready(client: Any, path: Path) -> Any:
    item = await call(f"uploading {path.name}", client.files.upload, file=str(path))
    deadline = time.monotonic() + 15 * 60
    while state_name(getattr(item, "state", None)) == "PROCESSING":
        if time.monotonic() > deadline:
            raise TimeoutError(f"Upload processing timed out: {path.name}")
        await asyncio.sleep(POLL_SECONDS)
        item = await call("checking upload", client.files.get, name=str(item.name))
    if state_name(getattr(item, "state", None)) == "FAILED" or not getattr(item, "uri", None):
        raise RuntimeError(f"Google could not process {path.name}")
    return item


async def output_bytes(client: Any, interaction: Any) -> bytes:
    output = getattr(interaction, "output_video", None)
    if output is None:
        raise RuntimeError(f"No video returned (status={getattr(interaction, 'status', 'unknown')})")
    if getattr(output, "data", None):
        return base64.b64decode(output.data)
    uri = str(getattr(output, "uri", ""))
    match = re.search(r"/files/([^/:?]+)", uri)
    if not match:
        raise RuntimeError("Video response contained no usable output URI")
    name = f"files/{match.group(1)}"
    deadline = time.monotonic() + 30 * 60
    while True:
        info = await call("checking output", client.files.get, name=name)
        state = state_name(getattr(info, "state", None))
        if state == "ACTIVE":
            break
        if state == "FAILED":
            raise RuntimeError("Gemini video generation failed")
        if time.monotonic() > deadline:
            raise TimeoutError("Generated video processing timed out")
        await asyncio.sleep(POLL_SECONDS)
    return await call("downloading output", client.files.download, file=uri)


def silence_points(source: Path) -> list[float]:
    result = subprocess.run(
        [
            ffmpeg(), "-hide_banner", "-i", str(source),
            "-af", "silencedetect=noise=-35dB:d=0.18",
            "-f", "null", "-",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    starts = [float(value) for value in re.findall(r"silence_start:\s*([0-9.]+)", result.stderr)]
    ends = [float(value) for value in re.findall(r"silence_end:\s*([0-9.]+)", result.stderr)]
    return [
        (start + end) / 2
        for start, end in zip(starts, ends)
        if end > start
    ]


def scene_points(source: Path, threshold: float = 0.25) -> list[float]:
    result = subprocess.run(
        [
            ffmpeg(), "-hide_banner", "-i", str(source),
            "-vf", f"select='gt(scene,{threshold})',showinfo",
            "-an", "-f", "null", "-",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    points = [
        float(value)
        for value in re.findall(r"pts_time:([0-9.]+)", result.stderr)
    ]
    deduplicated: list[float] = []
    for point in points:
        if not deduplicated or point - deduplicated[-1] >= 0.25:
            deduplicated.append(point)
    return deduplicated


def choose_boundaries(
    total: float,
    maximum: float,
    pauses: list[float],
    scenes: list[float] | None = None,
) -> list[float]:
    boundaries = [0.0]
    position = 0.0
    minimum = 3.0
    natural_points = sorted({*pauses, *(scenes or [])})

    while total - position > maximum:
        latest = min(position + maximum, total - minimum)
        earliest = position + minimum
        candidates = [point for point in natural_points if earliest <= point <= latest]
        target = min(position + maximum - 0.25, latest)
        cut = min(candidates, key=lambda point: abs(point - target)) if candidates else target
        boundaries.append(cut)
        position = cut

    boundaries.append(total)
    return boundaries


def split_source(source: Path, out_dir: Path, seconds: float) -> list[tuple[Path, float, float]]:
    total = duration(source)
    if total < 3:
        raise ValueError("Input video must be at least 3 seconds")
    boundaries = choose_boundaries(
        total,
        seconds,
        silence_points(source),
        scene_points(source),
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    parts: list[tuple[Path, float, float]] = []
    for index, (start, end) in enumerate(zip(boundaries, boundaries[1:]), 1):
        part = out_dir / f"source_{index:03d}.mp4"
        length = end - start
        run_ffmpeg([
            "-ss", f"{start:.6f}", "-i", str(source), "-t", f"{length:.6f}",
            "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264",
            "-crf", "18", "-preset", "medium", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", "-y", str(part),
        ], f"split segment {index}")
        parts.append((part, start, end))
    return parts


def normalize_dimensions(path: Path, width: int, height: int) -> None:
    current_width, current_height = video_size(path)
    if (current_width, current_height) == (width, height):
        return
    normalized = path.with_name(f"{path.stem}.normalized.mp4")
    run_ffmpeg([
        "-i", str(path), "-vf", f"scale={width}:{height}:flags=lanczos,setsar=1",
        "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264",
        "-crf", "18", "-preset", "medium", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", "-y", str(normalized),
    ], f"normalize dimensions for {path.name}")
    normalized.replace(path)


def image_item(path: Path) -> dict[str, str]:
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    return {
        "type": "image",
        "data": base64.b64encode(path.read_bytes()).decode("ascii"),
        "mime_type": mime,
    }


def prepare_visual_segment(path: Path, width: int, height: int, seconds: float,
                           start_seconds: float = 0.0) -> None:
    """Discard model audio and fit each edited shot to its original time window."""
    actual = duration(path)
    if abs(actual - seconds) > 0.5:
        raise RuntimeError("Edited segment duration differs from source by more than 0.5 seconds")
    normalized = path.with_name(f"{path.stem}.visual.mp4")
    # Round absolute boundaries so fractional-frame errors do not accumulate per shot.
    frame_count = max(1, round((start_seconds + seconds) * 30) - round(start_seconds * 30))
    run_ffmpeg([
        "-i", str(path), "-map", "0:v:0", "-an",
        "-vf", f"scale={width}:{height}:flags=lanczos,setsar=1,fps=30,tpad=stop_mode=clone:stop_duration=0.5",
        "-frames:v", str(frame_count), "-c:v", "libx264", "-crf", "18", "-preset", "medium",
        "-movflags", "+faststart", "-y", str(normalized),
    ], "normalize visual segment")
    normalized.replace(path)


def merge_with_original_audio(concat: Path, source: Path, output: Path) -> None:
    """Use only the original source's audio; a silent input stays silent."""
    run_ffmpeg([
        "-f", "concat", "-safe", "0", "-i", str(concat), "-i", str(source),
        "-map", "0:v:0", "-map", "1:a:0?", "-c:v", "libx264",
        "-crf", "18", "-preset", "medium", "-c:a", "aac", "-b:a", "192k",
        "-t", f"{duration(source):.6f}", "-movflags", "+faststart", "-y", str(output),
    ], "merge visuals with original audio")
    if has_audio(source) and not has_audio(output):
        raise RuntimeError("Original audio was not included in output")


async def run_pipeline(
    source: Path,
    references: list[Path],
    job_dir: Path,
    segment_seconds: float = 8.0,
    prompt: str | None = None,
    progress: Callable[[int, int, str], None] | None = None,
    generate_audio: bool = False,
) -> Path:
    if not 3 <= segment_seconds <= 9:
        raise ValueError("segment_seconds must be between 3 and 9")
    if not 1 <= len(references) <= 6:
        raise ValueError("Provide 1 to 6 reference images")
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured")

    parts = split_source(source, job_dir / "source_parts", segment_seconds)
    source_width, source_height = video_size(source)
    output_aspect_ratio = aspect_ratio_for(source)
    edited_dir = job_dir / "edited_parts"
    edited_dir.mkdir(parents=True, exist_ok=True)
    tags = ", ".join(f"<IMAGE_REF_{i}>" for i in range(len(references)))
    edit_prompt = prompt or (
        f"Replace only the consenting adult person in the source video with the same "
        f"consenting adult shown in {tags}. Preserve the exact performance, body motion, "
        "gestures, facial timing, camera, cuts, framing, duration, lighting, background, "
        "objects, text, composition, and existing audio. Do not add or remove anything else. "
        "This authorized synthetic edit will be disclosed as AI-generated."
    )

    client = genai.Client(api_key=api_key)
    edited: list[Path] = []
    try:
        for index, (part, segment_start, segment_end) in enumerate(parts, 1):
            target = edited_dir / f"edited_{index:03d}.mp4"
            edited.append(target)
            if (
                target.exists()
                and target.stat().st_size
                and aspect_matches(target, output_aspect_ratio)
                and (not generate_audio or has_audio(target))
            ):
                if generate_audio:
                    normalize_dimensions(target, source_width, source_height)
                else:
                    prepare_visual_segment(target, source_width, source_height, segment_end - segment_start, segment_start)
                if progress:
                    progress(index, len(parts), "cached")
                continue
            if progress:
                progress(index, len(parts), "processing")
            uploaded = await upload_ready(client, part)
            segment_duration = segment_end - segment_start
            segment_prompt = (
                edit_prompt
                .replace("{{SEGMENT_INDEX}}", str(index))
                .replace("{{SEGMENT_COUNT}}", str(len(parts)))
                .replace("{{SEGMENT_DURATION}}", f"{segment_duration:.3f}")
                + "\n\nThis segment was cut at a natural audio pause or visual shot boundary "
                "whenever one was available. "
                + ("Generate only the speech audible inside this exact segment; do not repeat boundary words. "
                   if generate_audio else "Edit visuals only. Do not generate speech. Original audio is restored separately. ")
                +
                f"Keep the exact source canvas and output aspect ratio {output_aspect_ratio}. "
                "Do not rotate, crop, stretch, reframe, or switch between portrait and landscape."
            )

            for video_attempt in range(1, VIDEO_ATTEMPTS + 1):
                interaction = await call(
                    "editing video",
                    client.interactions.create,
                    model=VIDEO_MODEL,
                    input=[
                        {"type": "video", "uri": str(uploaded.uri), "mime_type": "video/mp4"},
                        *[image_item(path) for path in references],
                        {"type": "text", "text": segment_prompt},
                    ],
                    generation_config={"video_config": {"task": "edit"}},
                    response_format={
                        "type": "video",
                        "delivery": "uri",
                    },
                    timeout=20 * 60,
                )
                target.write_bytes(await output_bytes(client, interaction))
                correct_aspect = aspect_matches(target, output_aspect_ratio)
                audio_present = has_audio(target)
                if correct_aspect and (not generate_audio or audio_present):
                    if generate_audio:
                        normalize_dimensions(target, source_width, source_height)
                    else:
                        prepare_visual_segment(target, source_width, source_height, segment_duration, segment_start)
                    break
                if progress:
                    problems = []
                    if not correct_aspect:
                        problems.append("wrong aspect ratio")
                    if generate_audio and not audio_present:
                        problems.append("missing audio track")
                    progress(
                        index,
                        len(parts),
                        f"{', '.join(problems)}; retry "
                        f"{video_attempt}/{VIDEO_ATTEMPTS}",
                    )
            else:
                width, height = video_size(target)
                audio_status = "present" if has_audio(target) else "missing"
                raise RuntimeError(
                    f"Segment {index} validation failed after {VIDEO_ATTEMPTS} attempts: "
                    f"video={width}x{height}, expected_aspect={output_aspect_ratio}, "
                    f"audio={audio_status}"
                )
            if progress:
                progress(index, len(parts), "completed")
    finally:
        close = getattr(client, "close", None)
        if close:
            close()

    concat = job_dir / "concat.txt"
    concat.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in edited), encoding="utf-8")
    output = job_dir / "result.mp4"
    if generate_audio:
        run_ffmpeg([
            "-f", "concat", "-safe", "0", "-i", str(concat), "-c:v", "libx264",
            "-crf", "18", "-preset", "medium", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", "-y", str(output),
        ], "merge edited segments")
        if not has_audio(output):
            raise RuntimeError("Merged output contains no audio track")
    else:
        merge_with_original_audio(concat, source, output)
    manifest = {
        "model": VIDEO_MODEL,
        "input_sha256": file_sha256(source),
        "reference_sha256": [file_sha256(path) for path in references],
        "input_duration": duration(source),
        "output_duration": duration(output),
        "segments": len(parts),
        "segment_seconds": segment_seconds,
        "segment_boundaries": [
            {"start": start, "end": end, "duration": end - start}
            for _, start, end in parts
        ],
        "output_aspect_ratio": output_aspect_ratio,
        "audio_mode": "generated" if generate_audio else "original",
    }
    (job_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return output
