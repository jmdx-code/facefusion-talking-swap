"""On-demand media inspection and FFmpeg standardization for automated face swapping."""
from __future__ import annotations

import collections
import json
import os
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from PIL import Image, ImageOps

NORMALIZE_IDLE_TIMEOUT_SEC = 300.0
MAX_SOURCE_IMAGE_PIXELS = 64_000_000
SAFE_PILLOW_IMAGE_FORMATS = ['JPEG', 'PNG', 'WEBP', 'BMP']
FFMPEG_LOCAL_PROTOCOL_WHITELIST = 'file,pipe,crypto,data'

Image.MAX_IMAGE_PIXELS = MAX_SOURCE_IMAGE_PIXELS


def terminate_process_tree(proc: Any) -> None:
    """Kill a subprocess and all its descendant processes (e.g. child ffmpeg.exe on Windows)."""
    if proc is None:
        return
    poll_fn = getattr(proc, 'poll', None)
    try:
        if callable(poll_fn) and poll_fn() is not None:
            return
    except Exception:
        pass

    pid = getattr(proc, 'pid', None)
    if os.name == 'nt' and isinstance(pid, int) and pid > 0:
        try:
            subprocess.run(
                ['taskkill', '/PID', str(pid), '/T', '/F'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=8,
                check=False
            )
        except Exception:
            pass

    kill_fn = getattr(proc, 'kill', None)
    if callable(kill_fn):
        try:
            kill_fn()
        except Exception:
            pass

    wait_fn = getattr(proc, 'wait', None)
    if callable(wait_fn):
        try:
            wait_fn(timeout=5)
        except Exception:
            pass


@dataclass
class VideoInspection:
    is_standard: bool
    reasons: List[str]
    width: int
    height: int
    duration_sec: float
    avg_fps: float
    r_fps: float
    is_hdr: bool
    has_audio: bool
    rotation: int


def _resolve_tool_binary(binary_name: str) -> str:
    found = shutil.which(binary_name)
    if found:
        return found

    # Fallback search for portable or workspace tools/ffmpeg layout
    here = Path(__file__).resolve()
    for ancestor in [here.parent.parent.parent, here.parent.parent.parent.parent]:
        tools_ffmpeg = ancestor / 'tools' / 'ffmpeg'
        if tools_ffmpeg.is_dir():
            for match in tools_ffmpeg.rglob(f'{binary_name}.exe' if os.name == 'nt' else binary_name):
                if match.is_file():
                    return str(match)
    raise ValueError(f'未找到 {binary_name} 工具，请确认通过启动脚本运行或已配置 FFmpeg。')


def _parse_fraction(rate_str: str) -> float:
    text = (rate_str or '').strip()
    if not text or text == '0/0':
        return 0.0
    if '/' in text:
        num_s, den_s = text.split('/', 1)
        try:
            num = float(num_s)
            den = float(den_s)
            return num / den if den != 0 else 0.0
        except ValueError:
            return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def _decode_image_via_ffmpeg(image_path: Path, work_dir: Path) -> Path:
    """Fallback decoder using FFmpeg for formats Pillow lacks native plugins for (e.g. iPhone .HEIC/.HEIF)."""
    ffmpeg_bin = _resolve_tool_binary('ffmpeg')
    work_dir.mkdir(parents=True, exist_ok=True)
    decoded_jpg = work_dir / 'source_ffmpeg_decoded.jpg'
    proc = subprocess.run(
        [
            ffmpeg_bin, '-y', '-hide_banner', '-loglevel', 'error',
            '-protocol_whitelist', FFMPEG_LOCAL_PROTOCOL_WHITELIST,
            '-i', str(image_path),
            '-frames:v', '1',
            '-pix_fmt', 'yuvj420p',
            '-q:v', '2',
            str(decoded_jpg)
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding='utf-8',
        errors='replace',
        timeout=45
    )
    if proc.returncode != 0 or not decoded_jpg.is_file() or decoded_jpg.stat().st_size == 0:
        detail = proc.stderr.strip()[:200]
        raise ValueError(f'无法解码源人脸图片（含 FFmpeg 兜底转换失败），请确认图片链接有效且未损坏：{detail}')
    return decoded_jpg


def prepare_source_image(image_path: Path, work_dir: Path, max_edge: int = 3000) -> Tuple[Path, str]:
    """Validate source face image, apply EXIF orientation if present, and convert exotic/HEIC formats to JPG."""
    path = Path(image_path)
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError('源人脸图片文件不存在或为空。')

    used_ffmpeg_fallback = False
    open_target = path
    try:
        with Image.open(open_target, formats=SAFE_PILLOW_IMAGE_FORMATS) as probe:
            probe.load()
    except Image.DecompressionBombError as exc:
        raise ValueError('源人脸图片像素尺寸异常过大（疑似解压炸弹），已拒绝解析。') from exc
    except Exception:
        open_target = _decode_image_via_ffmpeg(path, work_dir)
        used_ffmpeg_fallback = True

    try:
        with Image.open(open_target, formats=SAFE_PILLOW_IMAGE_FORMATS) as img:
            img.load()
            exif = img.getexif()
            orientation = int(exif.get(274, 1)) if exif else 1
            width, height = img.size
            if width < 32 or height < 32:
                raise ValueError(f'源人脸图片分辨率过小（{width}x{height}），请提供清晰人脸照片。')

            ext = open_target.suffix.lower()
            needs_convert = (
                used_ffmpeg_fallback
                or orientation != 1
                or ext not in {'.jpg', '.jpeg', '.png'}
                or img.mode not in {'RGB'}
                or max(width, height) > max_edge
            )
            if not needs_convert:
                return open_target, '源图格式标准'

            fixed = ImageOps.exif_transpose(img)
            if fixed.mode in {'RGBA', 'LA'} or (fixed.mode == 'P' and 'transparency' in fixed.info):
                rgba = fixed.convert('RGBA')
                canvas = Image.new('RGB', rgba.size, (255, 255, 255))
                canvas.paste(rgba, mask=rgba.split()[3])
                fixed = canvas
            elif fixed.mode != 'RGB':
                fixed = fixed.convert('RGB')

            if max(fixed.size) > max_edge:
                fixed.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)

            work_dir.mkdir(parents=True, exist_ok=True)
            out_path = work_dir / 'source_normalized.jpg'
            fixed.save(out_path, format='JPEG', quality=95, subsampling=0)
            note = '已通过 FFmpeg 解码并标准化源图（如 HEIC/特殊编码）' if used_ffmpeg_fallback else '已标准化源图朝向/色彩格式'
            return out_path, note
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f'无法解码源人脸图片，请确认图片链接有效且未损坏：{exc}') from exc


def inspect_video(video_path: Path, max_long_edge: int = 1920) -> VideoInspection:
    """Inspect video streams via ffprobe and determine whether re-encoding is needed."""
    path = Path(video_path)
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError('目标视频文件不存在或为空。')

    ffprobe_bin = _resolve_tool_binary('ffprobe')
    proc = subprocess.run(
        [
            ffprobe_bin, '-v', 'error',
            '-protocol_whitelist', FFMPEG_LOCAL_PROTOCOL_WHITELIST,
            '-show_format', '-show_streams', '-of', 'json', str(path)
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding='utf-8',
        errors='replace',
        timeout=45
    )
    if proc.returncode != 0:
        raise ValueError(f'无法解析目标视频文件（可能已损坏或不是有效视频）：{proc.stderr.strip()[:200]}')

    try:
        info: Dict[str, Any] = json.loads(proc.stdout)
    except ValueError as exc:
        raise ValueError('ffprobe 返回结果解析失败。') from exc

    streams = info.get('streams') or []
    video_streams = [
        s for s in streams
        if s.get('codec_type') == 'video' and not (s.get('disposition') or {}).get('attached_pic')
    ]
    if not video_streams:
        raise ValueError('目标文件中未检测到有效的视频画面流。')

    vstream = video_streams[0]
    audio_streams = [s for s in streams if s.get('codec_type') == 'audio']

    codec = str(vstream.get('codec_name') or '').lower()
    pix_fmt = str(vstream.get('pix_fmt') or '').lower()
    width = int(vstream.get('width') or 0)
    height = int(vstream.get('height') or 0)
    if width <= 0 or height <= 0:
        raise ValueError('目标视频宽高无效。')

    color_transfer = str(vstream.get('color_transfer') or '').lower()
    color_primaries = str(vstream.get('color_primaries') or '').lower()
    is_hdr = color_transfer in {'arib-std-b67', 'smpte2084'} or color_primaries == 'bt2020'

    rotation = 0
    tags = vstream.get('tags') or {}
    if 'rotate' in tags:
        try:
            rotation = int(float(tags['rotate'])) % 360
        except (ValueError, TypeError):
            rotation = 0
    for side in vstream.get('side_data_list') or []:
        if 'rotation' in side:
            try:
                rotation = int(float(side['rotation'])) % 360
            except (ValueError, TypeError):
                pass

    r_fps = _parse_fraction(str(vstream.get('r_frame_rate') or ''))
    avg_fps = _parse_fraction(str(vstream.get('avg_frame_rate') or '')) or r_fps
    duration_sec = float((info.get('format') or {}).get('duration') or vstream.get('duration') or 0.0)

    reasons: List[str] = []
    ext = path.suffix.lower()
    if ext not in {'.mp4', '.mov'}:
        reasons.append(f'容器格式({ext or "未知"})')
    if codec != 'h264':
        reasons.append(f'视频编码({codec})')
    if pix_fmt != 'yuv420p':
        reasons.append(f'像素位深({pix_fmt})')
    if is_hdr:
        reasons.append('HDR色彩空间')
    if rotation != 0:
        reasons.append(f'带旋转元数据({rotation}°)')
    if max(width, height) > max_long_edge:
        reasons.append(f'超高清分辨率({width}x{height})')
    if width % 2 != 0 or height % 2 != 0:
        reasons.append(f'奇数宽高({width}x{height})')
    if avg_fps > 60.5 or avg_fps < 12.0:
        reasons.append(f'非常规帧率({avg_fps:.1f}fps)')
    elif r_fps > 0 and avg_fps > 0 and abs(r_fps - avg_fps) > 1.2:
        reasons.append(f'可变帧率VFR({avg_fps:.1f}/{r_fps:.1f}fps)')

    if audio_streams:
        acodec = str(audio_streams[0].get('codec_name') or '').lower()
        if acodec not in {'aac', 'mp3'}:
            reasons.append(f'音频编码({acodec})')

    return VideoInspection(
        is_standard=(len(reasons) == 0),
        reasons=reasons,
        width=width,
        height=height,
        duration_sec=duration_sec,
        avg_fps=avg_fps,
        r_fps=r_fps,
        is_hdr=is_hdr,
        has_audio=bool(audio_streams),
        rotation=rotation
    )


def inspect_and_normalize_video(
    video_path: Path,
    work_dir: Path,
    max_long_edge: int = 1920,
    progress_cb: Optional[Callable[[float], None]] = None,
    should_abort: Optional[Callable[[], bool]] = None,
    idle_timeout_sec: float = NORMALIZE_IDLE_TIMEOUT_SEC
) -> Tuple[Path, bool, str]:
    """
    Only re-encode if video violates standard rules; otherwise return original path untouched.
    Returns (usable_video_path, was_reencoded, summary_note).
    """
    inspection = inspect_video(video_path, max_long_edge=max_long_edge)
    if inspection.is_standard:
        if Path(video_path).suffix.lower() == '.mp4':
            if progress_cb:
                progress_cb(100.0)
            return Path(video_path), False, '原片编码规整(免重编码)'

        # Standard H.264/AAC inside .mov must be fast-remuxed to .mp4 so FaceFusion's
        # `same_file_extension(target_path, output_path)` check succeeds without video re-encoding.
        ffmpeg_bin = _resolve_tool_binary('ffmpeg')
        work_dir.mkdir(parents=True, exist_ok=True)
        remux_path = work_dir / 'target_remuxed.mp4'
        remux_cmd = [
            ffmpeg_bin, '-y', '-hide_banner', '-loglevel', 'error',
            '-protocol_whitelist', FFMPEG_LOCAL_PROTOCOL_WHITELIST,
            '-i', str(video_path),
            '-map', '0:v:0'
        ]
        if inspection.has_audio:
            remux_cmd.extend(['-map', '0:a:0'])
        else:
            remux_cmd.append('-an')
        remux_cmd.extend(['-c', 'copy', '-movflags', '+faststart', str(remux_path)])
        remux_proc = subprocess.run(
            remux_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=120
        )
        if remux_proc.returncode == 0 and remux_path.is_file() and remux_path.stat().st_size > 0:
            if progress_cb:
                progress_cb(100.0)
            return remux_path, False, '原片编码规整(MOV无损封装MP4)'
        inspection.reasons.append('MOV容器兼容转换')

    reason_text = '、'.join(inspection.reasons)
    ffmpeg_bin = _resolve_tool_binary('ffmpeg')
    work_dir.mkdir(parents=True, exist_ok=True)
    output_path = work_dir / 'target_normalized.mp4'

    target_fps = inspection.avg_fps if 15.0 <= inspection.avg_fps <= 60.0 else 30.0
    if inspection.avg_fps > 60.0:
        target_fps = 30.0
    fps_value = round(target_fps) if abs(target_fps - round(target_fps)) < 0.15 else round(target_fps, 2)

    scale_expr = (
        f"scale='if(gt(iw,ih),min(iw,{max_long_edge}),-2)':"
        f"'if(gt(iw,ih),-2,min(ih,{max_long_edge}))'"
    )
    hdr_vf = (
        'zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,'
        'tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,'
        f'{scale_expr},format=yuv420p'
    )
    sdr_vf = f'{scale_expr},format=yuv420p'

    # Try NVENC first (with HDR tonemap if needed), then software libx264 fallback.
    attempts: List[Tuple[str, List[str]]] = []
    vf_primary = hdr_vf if inspection.is_hdr else sdr_vf
    attempts.append(('h264_nvenc', ['-vf', vf_primary, '-c:v', 'h264_nvenc', '-preset', 'p5', '-cq', '18']))
    attempts.append(('libx264', ['-vf', vf_primary, '-c:v', 'libx264', '-preset', 'fast', '-crf', '18']))
    if inspection.is_hdr:
        # Extra fallback if an external FFmpeg lacks zscale/tonemap filter
        attempts.append(('libx264_basic', ['-vf', sdr_vf, '-c:v', 'libx264', '-preset', 'fast', '-crf', '18']))

    last_err = ''
    for _, video_encode_args in attempts:
        if output_path.exists():
            try:
                output_path.unlink()
            except OSError:
                pass

        cmd = [
            ffmpeg_bin,
            '-y',
            '-hide_banner',
            '-loglevel', 'error',
            '-progress', 'pipe:1',
            '-nostats',
            '-protocol_whitelist', FFMPEG_LOCAL_PROTOCOL_WHITELIST,
            '-i', str(video_path),
            *video_encode_args,
            '-r', str(fps_value),
            '-fps_mode', 'cfr',
            '-movflags', '+faststart'
        ]
        if inspection.has_audio:
            cmd.extend(['-c:a', 'aac', '-b:a', '192k', '-ar', '48000', '-ac', '2'])
        else:
            cmd.append('-an')
        cmd.append(str(output_path))

        with subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',
            errors='replace'
        ) as proc:
            stderr_tail: Deque[str] = collections.deque(maxlen=35)
            stdout_queue: queue.Queue[Optional[str]] = queue.Queue()

            def _drain_stderr() -> None:
                if proc.stderr is None:
                    return
                try:
                    for err_line in proc.stderr:
                        cleaned_err = err_line.strip()
                        if cleaned_err:
                            stderr_tail.append(cleaned_err)
                except Exception:
                    pass

            def _drain_stdout() -> None:
                try:
                    if proc.stdout is not None:
                        for out_line in proc.stdout:
                            stdout_queue.put(out_line)
                except Exception:
                    pass
                finally:
                    stdout_queue.put(None)

            err_thread = threading.Thread(target=_drain_stderr, daemon=True)
            out_thread = threading.Thread(target=_drain_stdout, daemon=True)
            err_thread.start()
            out_thread.start()

            last_activity = time.monotonic()
            try:
                while True:
                    if should_abort and should_abort():
                        terminate_process_tree(proc)
                        raise InterruptedError('任务已由用户中止。')
                    try:
                        line = stdout_queue.get(timeout=0.5)
                    except queue.Empty:
                        if (time.monotonic() - last_activity) > idle_timeout_sec:
                            terminate_process_tree(proc)
                            raise ValueError(
                                f'FFmpeg 重编码目标视频超时无响应（连续 {int(idle_timeout_sec)} 秒无进度输出），可能原视频文件流严重损坏。'
                            )
                        continue

                    if line is None:
                        break
                    last_activity = time.monotonic()
                    if line.startswith('out_time_us=') and inspection.duration_sec > 0 and progress_cb:
                        try:
                            us = int(line.split('=', 1)[1].strip())
                            pct = min(99.0, max(0.0, (us / 1_000_000.0) / inspection.duration_sec * 100.0))
                            progress_cb(pct)
                        except ValueError:
                            pass

                err_thread.join(timeout=3.0)
                ret = proc.wait(timeout=30)
                if ret == 0 and output_path.is_file() and output_path.stat().st_size > 0:
                    if progress_cb:
                        progress_cb(100.0)
                    return output_path, True, f'已自动标准化({reason_text})'
                last_err = '\n'.join(stderr_tail).strip()
            finally:
                terminate_process_tree(proc)

    raise ValueError(f'FFmpeg 重编码目标视频失败：{last_err[:240]}')
