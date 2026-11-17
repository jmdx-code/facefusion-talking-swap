from collections import deque
from contextlib import ExitStack
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np
from facefusion import logger, presets, sheet_worker, video_manager


class EncoderFallbackTests(unittest.TestCase):
    def test_write_failure_emits_marker_accepted_by_worker(self):
        process=Mock()
        process.stdin.write.side_effect=BrokenPipeError('encoder stopped')
        process.poll.return_value=1
        with patch.object(logger,'error') as error:
            with self.assertRaises(BrokenPipeError):
                video_manager.write_video_frame({'process':process},np.zeros((16,16,3),np.uint8))
        self._marker_is_recognized(error.call_args.args[0])

    def test_close_flush_failure_emits_marker_accepted_by_worker(self):
        process=Mock()
        process.stdin.close.side_effect=BrokenPipeError('buffer flush failed')
        with patch.object(logger,'error') as error:
            with self.assertRaises(BrokenPipeError):
                video_manager.close_video_writer({'process':process})
        self._marker_is_recognized(error.call_args.args[0])

    def _marker_is_recognized(self, message):
        task=sheet_worker.LocalTask('test',2,'https://example.com/s.png','https://example.com/t.mp4','','手动领取')
        state={}
        sheet_worker.SheetWorkerManager._parse_headless_line(task,message,deque(),state)
        self.assertTrue(state['saw_merging'])

    def test_worker_retries_software_encoder_on_stream_failure_marker(self):
        with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
            root=Path(temp)
            manager=sheet_worker.SheetWorkerManager(config_path=root/'config.json',token_path=root/'token.json')
            manager.log=Mock(); manager.bump_ui_version=Mock()
            task=sheet_worker.LocalTask('test',2,'https://example.com/s.png','https://example.com/t.mp4','','手动领取')
            output=root/'output.mp4'; attempts=[]
            class Child:
                def __init__(self,cmd,**kwargs):
                    encoder=cmd[cmd.index('--output-video-encoder')+1]
                    attempts.append(encoder)
                    self.code=1 if encoder=='h264_nvenc' else 0
                    self.stdout=io.BytesIO(b'[FACEFUSION.VIDEO_MANAGER] stream_encoder_failed: pipe closed\n' if self.code else b'[FACEFUSION.CORE] swapped_frames_total=2\n')
                    if not self.code:output.write_bytes(b'encoded')
                def __enter__(self):return self
                def __exit__(self,*args):self.stdout.close()
                def wait(self,timeout=None):return self.code
            stack.enter_context(patch.object(sheet_worker.subprocess,'Popen',side_effect=Child))
            stack.enter_context(patch.object(sheet_worker,'terminate_process_tree'))
            stack.enter_context(patch.object(sheet_worker,'get_available_execution_providers',return_value=['cuda']))
            manager._execute_headless_swap(task,root/'source.png',root/'target.mp4',output,root,root/'jobs',presets.BUILTINS['default']['values'])
            self.assertEqual(attempts,['h264_nvenc','libx264'])
            self.assertEqual(output.read_bytes(),b'encoded')
