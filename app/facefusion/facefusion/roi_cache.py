"""Lossless RGB face-region cache in 30-second FFV1 segments.

Only complete segments become readable. Readers close each segment at its last
frame, so Windows can delete it as soon as the next stage commits its replacement.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading

import numpy as np


class CacheSpaceError(OSError):
    pass


class PipelineCancelled(Exception):
    pass


def check_cancelled():
    from facefusion import process_manager
    if process_manager.is_stopping() or process_manager.is_pending():
        raise PipelineCancelled()


class ProcessScope:
    """Terminate owned subprocesses on cancellation, including blocked pipe IO."""
    def __init__(self):
        self.processes = {}
        self.lock = threading.RLock()
        self.closed = threading.Event()

    def __enter__(self):
        self.watcher = threading.Thread(target=self._watch, daemon=True)
        self.watcher.start()
        return self

    def _watch(self):
        from facefusion import process_manager
        while not self.closed.wait(.1):
            if process_manager.is_stopping():
                self.terminate_all()

    def start(self, args, **kwargs):
        check_cancelled()
        errors = tempfile.TemporaryFile()
        try:
            process = subprocess.Popen(args, stderr=errors, **kwargs)
        except BaseException:
            errors.close()
            raise
        with self.lock:
            self.processes[process] = errors
        return process

    def attach(self, process):
        with self.lock:
            self.processes[process] = None

    def error_tail(self, process):
        with self.lock:
            stream = self.processes.get(process)
            if stream is None:
                return ''
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - 4096))
            return stream.read().decode('utf-8', errors='replace').strip()

    def finish(self, process):
        code = process.wait()
        tail = self.error_tail(process)
        self.forget(process)
        check_cancelled()
        if code:
            if 'no space left' in tail.lower():
                raise CacheSpaceError(tail)
            raise OSError('FFmpeg进程失败：' + (tail or str(code)))

    def forget(self, process):
        with self.lock:
            errors = self.processes.pop(process, None)
        for stream in (process.stdin, process.stdout, process.stderr, errors):
            if stream:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass

    def terminate_all(self):
        with self.lock:
            processes = list(self.processes)
        for process in processes:
            try:
                if process.poll() is None:
                    process.terminate()
            except OSError:
                pass

    def __exit__(self, *args):
        self.closed.set()
        self.terminate_all()
        with self.lock:
            processes = list(self.processes)
        for process in processes:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            self.forget(process)
        self.watcher.join(timeout=1)


def read_exact(process, size):
    buffer = bytearray(size)
    offset = 0
    while offset < size:
        check_cancelled()
        chunk = process.stdout.read(size - offset)
        if not chunk:
            check_cancelled()
            if offset == 0:
                return None
            raise OSError('视频帧数据不完整')
        buffer[offset:offset + len(chunk)] = chunk
        offset += len(chunk)
    return buffer


class CacheStats:
    def __init__(self):
        self.current_bytes = 0
        self.peak_bytes = 0
        self.written_bytes = 0

    def commit(self, path):
        size = path.stat().st_size
        self.current_bytes += size
        self.written_bytes += size
        self.peak_bytes = max(self.peak_bytes, self.current_bytes)

    def delete(self, path):
        size = path.stat().st_size
        path.unlink()
        self.current_bytes -= size


class SegmentWriter:
    def __init__(self, directory, boxes, fps, scope, stats, on_commit=None):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.boxes = boxes
        self.fps = fps
        self.frames_per_segment = max(1, round(fps * 30))
        self.scope = scope
        self.stats = stats
        self.on_commit = on_commit
        self.process = None
        self.segment = None
        self.next_frame = 0
        self.ffmpeg = shutil.which('ffmpeg')
        if not self.ffmpeg:
            raise OSError('未找到FFmpeg')

    def path(self, segment):
        return self.directory / ('%06d.mkv' % segment)

    def dimensions(self, segment):
        start = segment * self.frames_per_segment
        group = self.boxes[start:start + self.frames_per_segment]
        return (max(16, max(int(b[2] - b[0]) for b in group)),
                max(16, max(int(b[3] - b[1]) for b in group)))

    def ensure_space(self):
        if shutil.disk_usage(self.directory).free < 64 * 1024**2:
            raise CacheSpaceError('FFV1缓存磁盘余量不足64MiB')

    def finish_segment(self):
        if self.process is None:
            return
        process = self.process
        process.stdin.close()
        self.scope.finish(process)
        self.process = None
        path = self.path(self.segment)
        partial = path.with_suffix('.partial.mkv')
        partial.replace(path)
        self.stats.commit(path)
        if self.on_commit:
            self.on_commit(self.segment)

    def write(self, frame_number, image):
        check_cancelled()
        if frame_number != self.next_frame:
            raise ValueError('FFV1缓存帧顺序不一致')
        if frame_number % 30 == 0:
            self.ensure_space()
        segment = frame_number // self.frames_per_segment
        if segment != self.segment:
            self.finish_segment()
            self.segment = segment
            self.width, self.height = self.dimensions(segment)
            path = self.path(segment).with_suffix('.partial.mkv')
            self.process = self.scope.start([
                self.ffmpeg, '-y', '-v', 'error', '-filter_threads', '1',
                '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-s', f'{self.width}x{self.height}',
                '-r', str(self.fps), '-i', 'pipe:0', '-an', '-c:v', 'ffv1',
                '-level', '3', '-coder', '2', '-context', '1', '-g', '1',
                '-slices', '4', '-slicecrc', '1', '-pix_fmt', 'bgr0', '-threads', '4',
                str(path)
            ], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
        box = self.boxes[frame_number]
        expected = (int(box[3] - box[1]), int(box[2] - box[0]), 3)
        if image.shape != expected or image.dtype != np.uint8:
            raise ValueError('FFV1人脸区域尺寸或像素格式不一致')
        padded = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        padded[:image.shape[0], :image.shape[1]] = image
        try:
            self.process.stdin.write(padded.tobytes())
        except OSError:
            check_cancelled()
            self.ensure_space()
            if self.process.poll() is not None:
                self.scope.finish(self.process)
            raise
        self.next_frame += 1

    def close(self):
        self.finish_segment()
        if self.next_frame != len(self.boxes):
            raise OSError('FFV1缓存帧数不完整')

    def delete_segment(self, segment):
        path = self.path(segment)
        # All deletion is confined to the cache directory created for this job.
        if path.resolve().parent != self.directory:
            raise ValueError('缓存目录越界')
        self.stats.delete(path)


class SegmentReader:
    def __init__(self, writer):
        self.writer = writer
        self.scope = writer.scope
        self.segment = None
        self.process = None
        self.next_frame = 0

    def read(self, frame_number):
        check_cancelled()
        if frame_number != self.next_frame:
            raise ValueError('FFV1读取帧顺序不一致')
        segment = frame_number // self.writer.frames_per_segment
        if segment != self.segment:
            self.finish_segment()
            self.segment = segment
            self.width, self.height = self.writer.dimensions(segment)
            self.process = self.scope.start([
                self.writer.ffmpeg, '-v', 'error', '-threads', '4',
                '-i', str(self.writer.path(segment)), '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-fps_mode', 'passthrough', 'pipe:1'
            ], stdout=subprocess.PIPE)
        data = read_exact(self.process, self.width * self.height * 3)
        if data is None:
            raise OSError('FFV1缓存提前结束')
        box = self.writer.boxes[frame_number]
        width, height = int(box[2] - box[0]), int(box[3] - box[1])
        image = np.frombuffer(data, np.uint8).reshape(self.height, self.width, 3)[:height, :width].copy()
        self.next_frame += 1
        if self.next_frame % self.writer.frames_per_segment == 0 or self.next_frame == len(self.writer.boxes):
            self.finish_segment()
        return image

    def finish_segment(self):
        if self.process is not None:
            extra = self.process.stdout.read(1)
            if extra:
                raise OSError('FFV1缓存包含额外帧')
            self.scope.finish(self.process)
            self.process = None

    def close(self):
        self.finish_segment()
        if self.next_frame != len(self.writer.boxes):
            raise OSError('FFV1缓存读取未完成')
