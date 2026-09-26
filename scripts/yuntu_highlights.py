"""Select and extract auditable highlight frames from normalized Yuntu details."""

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import requests


ALLOWED_HIGHLIGHT_TRENDS = (
    "点赞指数",
    "流失指数",
    "点击指数",
    "互动指数",
    "评论指数",
)
STANDARD_HIGHLIGHT_TREND = "点击指数"
STANDARD_HIGHLIGHT_PEAK_LIMIT = 5

VIDEO_DOWNLOAD_TIMEOUT_SECONDS = 30
FRAME_EXTRACT_TIMEOUT_SECONDS = 120
VIDEO_DOWNLOAD_MAX_BYTES = 200 * 1024 * 1024


class HighlightFrameError(Exception):
    """Raised when an individual video or frame cannot be processed."""


def _redact_error_text(value):
    """Keep useful diagnostics while removing URL credentials and query secrets."""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    text = str(value or "").strip()
    text = re.sub(
        r"(?i)(https?://)([^/\s:@]+):([^@\s/]+)@",
        r"\1[REDACTED]@",
        text,
    )
    text = re.sub(
        r"(?i)(https?://[^\s?]+)\?[^\s]*",
        r"\1?[REDACTED_QUERY]",
        text,
    )
    text = re.sub(
        r"(?i)\b(authorization|bearer|token|password|secret|api[_-]?key)\s*[:=]\s*[^\s,;]+",
        r"\1=[REDACTED]",
        text,
    )
    return text[:240]


def _diagnostic_error_detail(error):
    """Return a bounded, credential-safe error detail for persisted diagnostics."""
    detail = _redact_error_text(error)
    return detail or error.__class__.__name__


def _ffmpeg_stderr_suffix(stderr):
    lines = _redact_error_text(stderr).splitlines()
    return f": {lines[-1]}" if lines and lines[-1] else ""


def normalize_highlight_trends(trend_names):
    """Validate configured highlight metrics and preserve their first occurrence."""
    normalized = []
    for trend_name in trend_names:
        if trend_name not in ALLOWED_HIGHLIGHT_TRENDS:
            raise ValueError(f"未知的高光趋势指标: {trend_name}")
        if trend_name not in normalized:
            normalized.append(trend_name)
    return normalized


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _normalized_points(series):
    by_second = {}
    for point in series:
        if not isinstance(point, dict):
            continue
        second = point.get("second")
        value = point.get("value")
        if not isinstance(second, int) or isinstance(second, bool) or not _is_number(value):
            continue
        previous = by_second.get(second)
        if previous is None or value > previous:
            by_second[second] = value
    return sorted(by_second.items())


def _collapse_plateaus(points):
    """Keep the earliest second of every continuous equal-value plateau."""
    collapsed = []
    for second, value in points:
        if (
            collapsed
            and second == collapsed[-1][0] + 1
            and value == collapsed[-1][1]
        ):
            continue
        collapsed.append((second, value))
    return collapsed


def _local_peaks(series, *, limit=3):
    points = _collapse_plateaus(_normalized_points(series))
    if len(points) < 2:
        return []

    peaks = []
    for index, (second, value) in enumerate(points):
        if index == 0:
            is_peak = value > points[1][1]
        elif index == len(points) - 1:
            is_peak = value > points[index - 1][1]
        else:
            is_peak = value > points[index - 1][1] and value > points[index + 1][1]
        if is_peak:
            peaks.append((second, value))
    return sorted(peaks, key=lambda point: (-point[1], point[0]))[:limit]


def _top_values(series, *, limit):
    """Return the highest-value distinct seconds, preferring earlier ties."""
    return sorted(_normalized_points(series), key=lambda point: (-point[1], point[0]))[:limit]


def _within_high_point(detail, trend_name, second):
    for high_point in detail.get("high_points", []):
        if not isinstance(high_point, dict) or high_point.get("trend_name") != trend_name:
            continue
        start_sec = high_point.get("start_sec")
        end_sec = high_point.get("end_sec")
        if (
            isinstance(start_sec, int)
            and not isinstance(start_sec, bool)
            and isinstance(end_sec, int)
            and not isinstance(end_sec, bool)
            and start_sec <= second <= end_sec
        ):
            return True
    return False


def _series_for_trend(detail, trend_name):
    points = []
    for trend in detail.get("trend_series", []):
        if isinstance(trend, dict) and trend.get("trend_name") == trend_name:
            series = trend.get("series")
            if isinstance(series, list):
                points.extend(series)
    return points


def select_highlight_candidates(detail, trend_names):
    """Return a cover frame plus up to three ranked peaks for each selected metric."""
    trend_names = normalize_highlight_trends(trend_names)
    candidates = {0: {"second": 0, "is_cover": True, "reasons": [{"type": "cover"}]}}

    for trend_name in trend_names:
        for rank, (second, value) in enumerate(
            _local_peaks(_series_for_trend(detail, trend_name)), start=1
        ):
            candidate = candidates.setdefault(
                second, {"second": second, "is_cover": False, "reasons": []}
            )
            candidate["reasons"].append(
                {
                    "type": "local_peak",
                    "trend_name": trend_name,
                    "value": value,
                    "rank": rank,
                    "within_high_point": _within_high_point(detail, trend_name, second),
                }
            )

    return [candidates[second] for second in sorted(candidates)]


def select_highlight_candidates_by_spec(detail, highlight_spec):
    """Select one merged candidate list from independently configured metrics."""
    candidates = {}
    if highlight_spec.get("includeZeroSecond", highlight_spec.get("include_cover", True)):
        candidates[0] = {
            "second": 0,
            "is_cover": True,
            "frame_type": "cover",
            "reasons": [{"type": "cover"}],
        }
    for trend_name, policy in highlight_spec.get("metrics", {}).items():
        if policy.get("enabled", True) is False:
            continue
        series = _series_for_trend(detail, trend_name)
        if policy["strategy"] in {"global_top_values", "top_values"}:
            selected = _top_values(series, limit=policy.get("maxFrames", policy.get("limit")))
            selected_type = "top_values"
        else:
            selected = _local_peaks(series, limit=policy.get("maxFrames", policy.get("limit")))
            selected_type = "local_peak"
        for rank, (second, value) in enumerate(selected, start=1):
            candidate = candidates.setdefault(
                second,
                {
                    "second": second,
                    "is_cover": second == 0,
                    "frame_type": "cover" if second == 0 else "custom_highlight",
                    "reasons": [],
                },
            )
            candidate["reasons"].append(
                {
                    "type": selected_type,
                    "trend_name": trend_name,
                    "value": value,
                    "rank": rank,
                    "within_high_point": _within_high_point(detail, trend_name, second),
                }
            )
    return [candidates[second] for second in sorted(candidates)]


def select_standard_highlight_candidates(detail):
    """Select the report's fixed cover plus click-index Top5 peak frames."""
    cover = {
        "second": 0,
        "is_cover": True,
        "frame_type": "cover",
        "peak_rank": None,
        "click_value": None,
        "reasons": [{"type": "cover"}],
    }
    candidates = [cover]
    for rank, (second, value) in enumerate(
        _local_peaks(
            _series_for_trend(detail, STANDARD_HIGHLIGHT_TREND),
            limit=STANDARD_HIGHLIGHT_PEAK_LIMIT,
        ),
        start=1,
    ):
        reason = {
            "type": "local_peak",
            "trend_name": STANDARD_HIGHLIGHT_TREND,
            "value": value,
            "rank": rank,
            "within_high_point": _within_high_point(
                detail, STANDARD_HIGHLIGHT_TREND, second
            ),
        }
        if second == 0:
            # The cover owns 0s so a boundary peak never produces two JPEGs.
            cover["peak_rank"] = rank
            cover["click_value"] = value
            cover["reasons"].append(reason)
            continue
        candidates.append(
            {
                "second": second,
                "is_cover": False,
                "frame_type": "click_peak",
                "peak_rank": rank,
                "click_value": value,
                "reasons": [reason],
            }
        )
    return candidates


def download_video(
    url,
    destination,
    *,
    session=None,
    timeout=VIDEO_DOWNLOAD_TIMEOUT_SECONDS,
    max_bytes=VIDEO_DOWNLOAD_MAX_BYTES,
):
    """Download one HTTP(S) video while enforcing the configured byte limit."""
    parsed = urlsplit(url) if isinstance(url, str) else None
    if not parsed or parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HighlightFrameError("video URL must use HTTP(S)")

    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f".{target.name}.part")
    response = None
    try:
        response = (session or requests).get(url, stream=True, timeout=timeout)
        response.raise_for_status()
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                if int(content_length) > max_bytes:
                    raise HighlightFrameError("video response exceeds maximum size")
            except ValueError:
                pass

        bytes_written = 0
        with partial.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                bytes_written += len(chunk)
                if bytes_written > max_bytes:
                    raise HighlightFrameError("video response exceeds maximum size")
                handle.write(chunk)
        if bytes_written == 0:
            raise HighlightFrameError("video response is empty")
        partial.replace(target)
        return target
    except HighlightFrameError:
        raise
    except Exception as error:
        raise HighlightFrameError(
            f"video download failed: {_diagnostic_error_detail(error)}"
        ) from error
    finally:
        partial.unlink(missing_ok=True)
        if response is not None:
            response.close()


def persist_video_for_detail(detail, output_dir, *, downloader=download_video):
    """Persist one detail video's local report asset without extracting frames."""
    material_id = str(detail.get("material_id") or "")
    video_url = detail.get("video_url")
    if not material_id or not isinstance(video_url, str) or not video_url.strip():
        raise HighlightFrameError("missing_video_url")
    target = Path(output_dir) / "videos" / material_id / "source-video.mp4"
    if target.is_file() and target.stat().st_size > 0:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    downloader(video_url, target)
    if not target.is_file() or target.stat().st_size == 0:
        raise HighlightFrameError("video download produced no local artifact")
    return target


def _resolve_ffmpeg(ffmpeg_bin=None, *, environ=None, imageio_resolver=None, which=None):
    environment = os.environ if environ is None else environ
    path_resolver = shutil.which if which is None else which

    for candidate in (ffmpeg_bin, environment.get("IMAGEIO_FFMPEG_EXE")):
        if candidate:
            return str(candidate)

    imageio_error = None
    if imageio_resolver is None:
        try:
            from imageio_ffmpeg import get_ffmpeg_exe
        except ImportError as error:
            imageio_error = error
        else:
            imageio_resolver = get_ffmpeg_exe

    if imageio_resolver is not None:
        try:
            candidate = imageio_resolver()
        except Exception as error:
            imageio_error = error
        else:
            if candidate:
                return str(candidate)

    candidate = path_resolver("ffmpeg")
    if candidate:
        return str(candidate)

    message = "ffmpeg executable was not found; install imageio-ffmpeg or set IMAGEIO_FFMPEG_EXE"
    if imageio_error is not None:
        raise HighlightFrameError(f"{message}: {imageio_error}") from imageio_error
    raise HighlightFrameError(message)


def extract_frame(video_path, second, destination, *, ffmpeg_bin=None):
    """Use FFmpeg to produce one non-empty JPEG, publishing it atomically."""
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f".{target.stem}.part{target.suffix}")
    command = [
        _resolve_ffmpeg(ffmpeg_bin),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(video_path),
        "-ss",
        str(second),
        "-map",
        "0:v:0",
        "-frames:v",
        "1",
        "-an",
        "-sn",
        "-dn",
        "-q:v",
        "2",
        "-f",
        "image2",
        str(partial),
    ]
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=FRAME_EXTRACT_TIMEOUT_SECONDS,
            check=False,
        )
        if completed.returncode != 0 or not partial.is_file() or partial.stat().st_size == 0:
            raise HighlightFrameError(
                f"ffmpeg frame extraction failed{_ffmpeg_stderr_suffix(completed.stderr)}"
            )
        partial.replace(target)
        return target
    except HighlightFrameError:
        raise
    except subprocess.TimeoutExpired as error:
        suffix = _ffmpeg_stderr_suffix(error.stderr)
        raise HighlightFrameError(
            f"ffmpeg frame extraction timed out after {FRAME_EXTRACT_TIMEOUT_SECONDS} seconds{suffix}"
        ) from error
    except (OSError, subprocess.SubprocessError) as error:
        raise HighlightFrameError(
            f"ffmpeg frame extraction failed: {_diagnostic_error_detail(error)}"
        ) from error
    finally:
        partial.unlink(missing_ok=True)


def _validate_jpeg(path, *, ffmpeg_bin=None):
    """Reject empty or structurally malformed JPEG output without spawning FFmpeg."""
    image = Path(path)
    if not image.is_file() or image.stat().st_size == 0:
        raise HighlightFrameError("JPEG output is empty")
    with image.open("rb") as handle:
        header = handle.read(2)
        handle.seek(-2, os.SEEK_END)
        trailer = handle.read(2)
    if header != b"\xff\xd8" or trailer != b"\xff\xd9":
        raise HighlightFrameError("JPEG markers are invalid")


def _batch_filter_graph(seconds):
    labels = "".join(f"[s{index}]" for index in range(len(seconds)))
    branches = [f"[0:v]split={len(seconds)}{labels}"]
    branches.extend(
        f"[s{index}]trim=start={second:g},setpts=PTS-STARTPTS[out{index}]"
        for index, second in enumerate(seconds)
    )
    return ";".join(branches)


def extract_frames_batch(video_path, requests, *, ffmpeg_bin=None):
    """Extract a material's unique target seconds in one FFmpeg process."""
    staged = []
    seen_seconds = set()
    for request in requests:
        second = request["second"]
        target = Path(request["destination"])
        if second in seen_seconds:
            continue
        seen_seconds.add(second)
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            prefix="yuntu-frame-", suffix=".jpg", dir=target.parent, delete=False
        ) as handle:
            staged.append((second, target, Path(handle.name)))
    if not staged:
        raise HighlightFrameError("no frame requests")

    command = [
        _resolve_ffmpeg(ffmpeg_bin),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-threads",
        "1",
        "-filter_threads",
        "1",
        "-i",
        str(video_path),
        "-filter_complex",
        _batch_filter_graph([second for second, _, _ in staged]),
    ]
    for index, (_, _, staging) in enumerate(staged):
        command.extend(
            [
                "-map",
                f"[out{index}]",
                "-frames:v",
                "1",
                "-an",
                "-sn",
                "-dn",
                "-q:v",
                "2",
                "-f",
                "image2",
                str(staging),
            ]
        )

    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=FRAME_EXTRACT_TIMEOUT_SECONDS,
            check=False,
        )
        if completed.returncode != 0:
            raise HighlightFrameError(
                "ffmpeg batch frame extraction failed"
                f"{_ffmpeg_stderr_suffix(completed.stderr)}"
            )
        for _, _, staging in staged:
            _validate_jpeg(staging, ffmpeg_bin=ffmpeg_bin)
        for _, target, staging in staged:
            staging.replace(target)
        return [target for _, target, _ in staged]
    except HighlightFrameError:
        raise
    except subprocess.TimeoutExpired as error:
        suffix = _ffmpeg_stderr_suffix(error.stderr)
        raise HighlightFrameError(
            "ffmpeg batch frame extraction timed out after "
            f"{FRAME_EXTRACT_TIMEOUT_SECONDS} seconds{suffix}"
        ) from error
    except (OSError, subprocess.SubprocessError) as error:
        raise HighlightFrameError(
            "ffmpeg batch frame extraction failed: "
            f"{_diagnostic_error_detail(error)}"
        ) from error
    finally:
        for _, _, staging in staged:
            staging.unlink(missing_ok=True)


def _write_json_atomically(payload, destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(f".{destination.name}.part")
    try:
        partial.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        partial.replace(destination)
    finally:
        partial.unlink(missing_ok=True)


def _frame_record(
    candidate,
    *,
    material_id,
    available,
    relative_path=None,
    failure_reason=None,
    failure_detail=None,
    frame_source=None,
):
    frame = {**candidate, "available": available}
    frame_type = frame.get("frame_type") or (
        "cover" if frame.get("is_cover") else "click_peak"
    )
    frame["frame_type"] = frame_type
    frame["frame_ref"] = f"highlight:{material_id}:{frame_type}:{frame['second']}"
    if relative_path is not None:
        frame["relative_path"] = relative_path
    if failure_reason is not None:
        frame["failure_reason"] = failure_reason
    if failure_detail is not None:
        frame["failure_detail"] = _redact_error_text(failure_detail)
    if frame_source is not None:
        frame["frame_source"] = frame_source
    return frame


def _highlight_filename(index, candidate):
    return f"{index:02d}-{candidate['second']:03d}s.jpg"


def _reused_frame_record(candidate, *, material_id, root, index):
    target = root / "highlights" / material_id / _highlight_filename(index, candidate)
    if not target.is_file() or target.stat().st_size == 0:
        return None
    return _frame_record(
        candidate,
        material_id=material_id,
        available=True,
        relative_path=str(target.relative_to(root)),
        frame_source="reused_local_extracted_frame",
    )


def _failed_frames_with_local_reuse(
    candidates,
    *,
    material_id,
    root,
    failure_reason,
    failure_detail=None,
    candidate_indexes=None,
):
    frames = []
    indexes = candidate_indexes or range(1, len(candidates) + 1)
    for index, candidate in zip(indexes, candidates):
        frames.append(
            _reused_frame_record(
                candidate,
                material_id=material_id,
                root=root,
                index=index,
            )
            or _frame_record(
                candidate,
                material_id=material_id,
                available=False,
                failure_reason=failure_reason,
                failure_detail=failure_detail,
            )
        )
    return frames


def _extract_frames_individually(
    video_path,
    candidates,
    root,
    material_id,
    material_root,
    frame_extractor,
    ffmpeg_bin,
    candidate_indexes=None,
):
    frames = []
    indexes = candidate_indexes or range(1, len(candidates) + 1)
    for index, candidate in zip(indexes, candidates):
        filename = _highlight_filename(index, candidate)
        target = material_root / filename
        relative_path = str(target.relative_to(root))
        try:
            frame_extractor(video_path, candidate["second"], target, ffmpeg_bin=ffmpeg_bin)
            frames.append(
                _frame_record(
                    candidate,
                    material_id=material_id,
                    available=True,
                    relative_path=relative_path,
                )
            )
        except Exception as error:
            frames.append(
                _reused_frame_record(
                    candidate,
                    material_id=material_id,
                    root=root,
                    index=index,
                )
                or _frame_record(
                    candidate,
                    material_id=material_id,
                    available=False,
                    failure_reason="extract_failed",
                    failure_detail=_diagnostic_error_detail(error),
                )
            )
    return frames


def _material_frames(
    detail,
    candidates,
    root,
    downloader,
    frame_extractor,
    ffmpeg_bin,
    batch_extractor=None,
    candidate_indexes=None,
):
    material_id = str(detail.get("material_id") or "")
    indexes = list(candidate_indexes or range(1, len(candidates) + 1))
    if len(indexes) != len(candidates):
        raise HighlightFrameError("candidate indexes must match configured frame candidates")
    video_url = detail.get("video_url")
    if not isinstance(video_url, str) or not video_url.strip():
        return _failed_frames_with_local_reuse(
            candidates,
            material_id=material_id,
            root=root,
            failure_reason="missing_video_url",
            candidate_indexes=indexes,
        )

    material_root = root / "highlights" / material_id
    try:
        video_path = persist_video_for_detail(detail, root, downloader=downloader)
        material_root.mkdir(parents=True, exist_ok=True)
        requests = [
            {
                "second": candidate["second"],
                "destination": material_root / _highlight_filename(index, candidate),
            }
            for index, candidate in zip(indexes, candidates)
        ]
        try:
            (batch_extractor or extract_frames_batch)(
                video_path, requests, ffmpeg_bin=ffmpeg_bin
            )
        except Exception:
            return _extract_frames_individually(
                video_path,
                candidates,
                root,
                material_id,
                material_root,
                frame_extractor,
                ffmpeg_bin,
                indexes,
            )
        return [
            _frame_record(
                candidate,
                material_id=material_id,
                available=True,
                relative_path=str(request["destination"].relative_to(root)),
            )
            for candidate, request in zip(candidates, requests)
        ]
    except Exception as error:
        return _failed_frames_with_local_reuse(
            candidates,
            material_id=material_id,
            root=root,
            failure_reason="download_failed",
            failure_detail=_diagnostic_error_detail(error),
            candidate_indexes=indexes,
        )


def load_highlights_manifest(output_dir):
    """Load standard highlight frames keyed by material ID when locally valid."""
    path = Path(output_dir) / "highlights-manifest.json"
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    materials = payload.get("materials") if isinstance(payload, dict) else None
    if not isinstance(materials, list):
        return {}
    return {
        str(item.get("material_id")): item.get("frames", [])
        for item in materials
        if isinstance(item, dict)
        and isinstance(item.get("material_id"), (str, int))
        and isinstance(item.get("frames"), list)
    }


def write_highlights_manifest(output_dir, frames_by_material, highlight_spec_fingerprint=None):
    """Atomically publish complete frame records from the scheduler thread."""
    materials = [
        {"material_id": material_id, "frames": frames}
        for material_id, frames in sorted(frames_by_material.items())
    ]
    payload = {
            "schemaVersion": 2,
            "standard": "cover_plus_click_top5",
            "materials": materials,
    }
    if highlight_spec_fingerprint is not None:
        payload["highlightSpecFingerprint"] = highlight_spec_fingerprint
    _write_json_atomically(payload, Path(output_dir) / "highlights-manifest.json")


def _reusable_standard_frames(detail, root):
    material_id = str(detail.get("material_id") or "")
    candidates = select_standard_highlight_candidates(detail)
    frames = load_highlights_manifest(root).get(material_id)
    if not isinstance(frames, list) or len(frames) != len(candidates):
        return None

    by_key = {}
    for frame in frames:
        if not isinstance(frame, dict):
            return None
        key = (frame.get("frame_type"), frame.get("second"))
        if key in by_key:
            return None
        by_key[key] = frame

    reusable = []
    root_path = Path(root).resolve()
    for candidate in candidates:
        frame = by_key.get((candidate["frame_type"], candidate["second"]))
        relative_path = frame.get("relative_path") if isinstance(frame, dict) else None
        if not frame or not frame.get("available") or not isinstance(relative_path, str):
            return None
        path = (root_path / relative_path).resolve()
        try:
            path.relative_to(root_path)
        except ValueError:
            return None
        if not path.is_file() or not path.stat().st_size:
            return None
        reusable.append(
            _frame_record(
                candidate,
                material_id=material_id,
                available=True,
                relative_path=relative_path,
                frame_source="reused_local_extracted_frame",
            )
        )
    return reusable


def details_missing_standard_highlights(details, output_dir):
    """Return only details without a complete reusable local standard frame set."""
    root = Path(output_dir)
    return [
        detail
        for detail in details
        if isinstance(detail, dict) and _reusable_standard_frames(detail, root) is None
    ]


def generate_standard_highlights_for_detail(
    detail,
    output_dir,
    *,
    downloader=download_video,
    frame_extractor=extract_frame,
    batch_extractor=None,
    ffmpeg_bin=None,
):
    """Generate or reuse the report's standard highlighter set for one material."""
    root = Path(output_dir)
    reusable = _reusable_standard_frames(detail, root)
    if reusable is not None:
        return reusable
    return _material_frames(
        detail,
        select_standard_highlight_candidates(detail),
        root,
        downloader,
        frame_extractor,
        ffmpeg_bin,
        batch_extractor,
    )


def generate_highlights_for_detail(
    detail,
    output_dir,
    highlight_spec,
    *,
    downloader=download_video,
    frame_extractor=extract_frame,
    batch_extractor=None,
    ffmpeg_bin=None,
):
    """Generate or reuse frames selected by a resolved HighlightSpec."""
    root = Path(output_dir)
    return _material_frames(
        detail,
        select_highlight_candidates_by_spec(detail, highlight_spec),
        root,
        downloader,
        frame_extractor,
        ffmpeg_bin,
        batch_extractor,
    )


def _candidate_key(candidate):
    return candidate["frame_type"], candidate["second"]


def _usable_configured_frame(root, candidate, frame):
    if (
        not isinstance(frame, dict)
        or not frame.get("available")
        or frame.get("frame_type") != candidate["frame_type"]
        or frame.get("second") != candidate["second"]
    ):
        return False
    relative_path = frame.get("relative_path")
    if not isinstance(relative_path, str) or not relative_path:
        return False
    root_path = Path(root).resolve()
    path = (root_path / relative_path).resolve()
    try:
        path.relative_to(root_path)
    except ValueError:
        return False
    return path.is_file() and path.stat().st_size > 0


def _configured_highlight_frame_state(detail, root, highlight_spec):
    candidates = select_highlight_candidates_by_spec(detail, highlight_spec)
    material_id = str(detail.get("material_id") or "")
    frames = load_highlights_manifest(root).get(material_id, [])
    frames_by_key = {}
    for frame in frames:
        if not isinstance(frame, dict):
            continue
        key = (frame.get("frame_type"), frame.get("second"))
        if key in frames_by_key:
            frames_by_key[key] = None
        else:
            frames_by_key[key] = frame
    available = {}
    for candidate in candidates:
        key = _candidate_key(candidate)
        frame = frames_by_key.get(key)
        if _usable_configured_frame(root, candidate, frame):
            available[key] = frame
    return candidates, available


def generate_missing_highlights_for_detail(
    detail,
    output_dir,
    highlight_spec,
    *,
    downloader=download_video,
    frame_extractor=extract_frame,
    batch_extractor=None,
    ffmpeg_bin=None,
):
    """Regenerate only unavailable configured frames and retain usable local ones."""
    root = Path(output_dir)
    candidates, available = _configured_highlight_frame_state(
        detail, root, highlight_spec
    )
    missing = [
        (index, candidate)
        for index, candidate in enumerate(candidates, start=1)
        if _candidate_key(candidate) not in available
    ]
    if not missing:
        return [available[_candidate_key(candidate)] for candidate in candidates]
    generated = _material_frames(
        detail,
        [candidate for _, candidate in missing],
        root,
        downloader,
        frame_extractor,
        ffmpeg_bin,
        batch_extractor,
        candidate_indexes=[index for index, _ in missing],
    )
    frames = {**available, **{_candidate_key(frame): frame for frame in generated}}
    return [frames[_candidate_key(candidate)] for candidate in candidates]


def details_missing_highlights_by_spec(details, output_dir, highlight_spec):
    """Return details whose configured frame files are not all locally available."""
    root = Path(output_dir)
    missing = []
    for detail in details:
        expected, available = _configured_highlight_frame_state(
            detail, root, highlight_spec
        )
        if len(available) != len(expected):
            missing.append(detail)
    return missing


def generate_highlights(
    details,
    trend_names,
    output_dir,
    *,
    downloader=download_video,
    frame_extractor=extract_frame,
    batch_extractor=None,
    ffmpeg_bin=None,
):
    """Create non-blocking highlight JPEGs and return their manifest frame records."""
    selected_trends = normalize_highlight_trends(trend_names)
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    results = {}
    materials = []
    for detail in details:
        material_id = str(detail.get("material_id") or "")
        frames = _material_frames(
            detail,
            select_highlight_candidates(detail, selected_trends),
            root,
            downloader,
            frame_extractor,
            ffmpeg_bin,
            batch_extractor,
        )
        results[material_id] = frames
        materials.append(
            {
                "material_id": material_id,
                "video_url": detail.get("video_url") if isinstance(detail.get("video_url"), str) else "",
                "frames": frames,
            }
        )
    _write_json_atomically(
        {"schemaVersion": 1, "materials": materials}, root / "highlights-manifest.json"
    )
    return results


def generate_standard_highlights(
    details,
    output_dir,
    *,
    downloader=download_video,
    frame_extractor=extract_frame,
    batch_extractor=None,
    ffmpeg_bin=None,
):
    """Create the standard cover plus click-index Top5 frames for the report."""
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    results = {}
    for detail in details:
        material_id = str(detail.get("material_id") or "")
        frames = generate_standard_highlights_for_detail(
            detail,
            root,
            downloader=downloader,
            frame_extractor=frame_extractor,
            batch_extractor=batch_extractor,
            ffmpeg_bin=ffmpeg_bin,
        )
        results[material_id] = frames
    write_highlights_manifest(root, results)
    return results
