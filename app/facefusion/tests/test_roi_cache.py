import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from unittest.mock import patch

import numpy as np
from facefusion import process_manager, state_manager
from facefusion.roi_cache import CacheStats, CacheSpaceError, PipelineCancelled, ProcessScope, SegmentReader, SegmentWriter, read_exact
from facefusion.workflows import image_to_video
from facefusion.workflows.roi_video import OriginalFrames, overlay_region

ROOT = Path(__file__).resolve().parents[3]
FFMPEG = next((ROOT / 'tools/ffmpeg').rglob('ffmpeg.exe'), None)
if FFMPEG:
    os.environ['PATH'] = str(FFMPEG.parent) + os.pathsep + os.environ.get('PATH', '')


class RoiCacheTests(unittest.TestCase):
    def setUp(self):
        process_manager.start()

    def tearDown(self):
        process_manager.end()

    @unittest.skipUnless(FFMPEG or shutil.which('ffmpeg'), 'FFmpeg unavailable')
    def test_lossless_variable_native_regions_cross_segments_and_delete_open_reader(self):
        rng = np.random.default_rng(19)
        boxes = [np.array([0, 0, 19+n, 21+n]) for n in range(7)]
        images = [rng.integers(0, 256, (b[3], b[2], 3), dtype=np.uint8) for b in boxes]
        with tempfile.TemporaryDirectory() as temp, ProcessScope() as scope:
            stats = CacheStats()
            first = SegmentWriter(Path(temp)/'first', boxes, .1, scope, stats)
            for n, image in enumerate(images):
                first.write(n, image)
            first.close()
            self.assertEqual(len(list(first.directory.glob('*.mkv'))), 3)
            reader = SegmentReader(first)
            second = SegmentWriter(Path(temp)/'second', boxes, .1, scope, stats, first.delete_segment)
            for n, expected in enumerate(images):
                actual = reader.read(n)
                np.testing.assert_array_equal(actual, expected)
                second.write(n, actual)
            reader.close()
            second.close()
            self.assertFalse(list(first.directory.glob('*')))
            reader = SegmentReader(second)
            for n, expected in enumerate(images):
                np.testing.assert_array_equal(reader.read(n), expected)
                if (n+1) % 3 == 0 or n == 6:
                    self.assertIsNone(reader.process)
                    second.delete_segment(n//3)
            reader.close()
            self.assertEqual(stats.current_bytes, 0)
            self.assertGreater(stats.peak_bytes, 0)
            self.assertFalse(scope.processes)

    @unittest.skipUnless(FFMPEG or shutil.which('ffmpeg'), 'FFmpeg unavailable')
    def test_missing_face_single_pixel_and_fractional_fps(self):
        with tempfile.TemporaryDirectory() as temp, ProcessScope() as scope:
            writer = SegmentWriter(temp, [np.array([0,0,1,1])]*2, 29.97, scope, CacheStats())
            pixel = np.array([[[1, 128, 255]]], np.uint8)
            writer.write(0, pixel); writer.write(1, pixel); writer.close()
            reader = SegmentReader(writer)
            for n in range(2):
                np.testing.assert_array_equal(reader.read(n), pixel)
            reader.close(); writer.delete_segment(0)

    def test_cancel_unblocks_owned_pipe_and_reaps_child(self):
        with ProcessScope() as scope:
            child = scope.start([sys.executable, '-c', 'import time; time.sleep(20)'], stdout=subprocess.PIPE)
            timer = threading.Timer(.2, process_manager.stop)
            timer.start()
            started = time.monotonic()
            try:
                with self.assertRaises(PipelineCancelled):
                    read_exact(child, 1)
            finally:
                timer.cancel()
        self.assertIsNotNone(child.poll())
        self.assertLess(time.monotonic()-started, 4)

    @unittest.skipUnless(FFMPEG or shutil.which('ffmpeg'), 'FFmpeg unavailable')
    def test_trimmed_stream_resets_timestamps_and_replays_identical_frames(self):
        with tempfile.TemporaryDirectory() as temp, ProcessScope() as scope:
            target=Path(temp)/'input.mkv'
            subprocess.run([shutil.which('ffmpeg'),'-y','-v','error','-f','lavfi','-i','testsrc2=size=24x26:rate=4','-frames:v','8','-c:v','ffv1',str(target)],check=True)
            first=list(OriginalFrames(str(target),(24,26),2,2,6,scope))
            self.assertEqual(len(first),2)
            second=list(OriginalFrames(str(target),(24,26),2,2,6,scope,2))
            for (a,x),(b,y) in zip(first,second):
                self.assertEqual(a,b);np.testing.assert_array_equal(x,y)
            self.assertFalse(scope.processes)

    def test_overlay_preserves_every_pixel_outside_native_box(self):
        original = np.arange(12*13*3, dtype=np.uint8).reshape(12,13,3)
        region = np.full((5,4,3), 249, np.uint8)
        output = overlay_region(original, region, [2,3,6,8], object())
        expected = original.copy(); expected[3:8,2:6] = region
        np.testing.assert_array_equal(output, expected)
        np.testing.assert_array_equal(overlay_region(original, region, [2,3,6,8], None), original)

    def test_default_route_skips_extraction_and_obsolete_full_frame_budget(self):
        self._route(False)

    def test_cache_space_failure_retries_stream_with_one_worker(self):
        self._route(True)

    def _route(self, low_space):
        state_manager.init_item('workflow_strategy','disk')
        state_manager.init_item('execution_thread_count',8)
        with ExitStack() as stack:
            for name in ['analyse_video','clear','setup','restore_audio','finalize_video']:
                stack.enter_context(patch.object(image_to_video,name,return_value=0))
            stack.enter_context(patch.object(image_to_video,'supported',return_value=True))
            budget=stack.enter_context(patch.object(image_to_video,'has_disk_budget',return_value=False))
            extract=stack.enter_context(patch.object(image_to_video,'extract_frames'))
            render=stack.enter_context(patch.object(image_to_video,'process_staged_frames',side_effect=CacheSpaceError('full') if low_space else None,return_value=0))
            def stream():
                self.assertEqual(state_manager.get_item('execution_thread_count'),1)
                return 0
            fallback=stack.enter_context(patch.object(image_to_video,'process_memory_frames',side_effect=stream))
            stack.enter_context(patch('facefusion.video_manager.clear_video_pool'))
            self.assertEqual(image_to_video.process(0),0)
            render.assert_called_once(); extract.assert_not_called(); budget.assert_not_called()
            self.assertEqual(fallback.call_count,int(low_space))
            self.assertEqual(state_manager.get_item('execution_thread_count'),8)
