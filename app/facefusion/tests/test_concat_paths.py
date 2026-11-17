import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from facefusion import ffmpeg, media_normalizer, process_manager, state_manager


class UnicodeConcatTest(unittest.TestCase):
    def test_real_ffmpeg_can_finalize_chinese_output_paths(self):
        tool = Path(media_normalizer._resolve_tool_binary('ffmpeg'))
        probe = media_normalizer._resolve_tool_binary('ffprobe')
        before = process_manager.get_process_state()
        old_level = state_manager.get_item('log_level')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / '中文导出目录'
            root.mkdir()
            parts = []
            for name, color in [('嘴巴片段','red'),('牙齿片段','blue')]:
                part = root / (name+'.mp4')
                subprocess.run([str(tool),'-y','-v','error','-f','lavfi','-i',f'color=c={color}:s=96x96:r=25:d=0.4','-c:v','libx264','-pix_fmt','yuv420p',str(part)],check=True)
                parts.append(str(part))
            output = root / '改善结果.mp4'
            try:
                state_manager.init_item('log_level','error')
                process_manager.start()
                with patch.dict(os.environ,{'PATH':str(tool.parent)+os.pathsep+os.environ.get('PATH','')}):
                    self.assertTrue(ffmpeg.concat_video(str(output),parts))
                info = json.loads(subprocess.check_output([probe,'-v','error','-show_entries','format=duration','-of','json',str(output)]))
                self.assertAlmostEqual(float(info['format']['duration']),.8,places=1)
            finally:
                process_manager.set_process_state(before)
                state_manager.init_item('log_level',old_level)


if __name__=='__main__':unittest.main()
