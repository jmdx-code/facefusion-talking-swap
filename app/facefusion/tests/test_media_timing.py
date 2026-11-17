import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from facefusion import manual_preflight, media_normalizer


class VideoTimingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.video = self.root / 'source.mp4'
        self.video.write_bytes(b'test-video')

    def tearDown(self):
        self.temp.cleanup()

    def inspect(self, timestamps, rate='30/1', average='299/10'):
        metadata = {'streams': [{'codec_type': 'video', 'codec_name': 'h264', 'pix_fmt': 'yuv420p', 'width': 1080, 'height': 1920, 'avg_frame_rate': average, 'r_frame_rate': rate}], 'format': {'duration': '10'}}
        replies = [
            SimpleNamespace(returncode=0, stdout=json.dumps(metadata), stderr=''),
            SimpleNamespace(returncode=0, stdout=json.dumps({'packets': [{'pts_time': str(t)} for t in timestamps]}), stderr='')
        ]
        with patch.object(media_normalizer, '_resolve_tool_binary', return_value='ffprobe'), patch.object(media_normalizer.subprocess, 'run', side_effect=replies):
            return media_normalizer.inspect_video(self.video)

    def test_variable_packets_detected_despite_nearly_equal_metadata_rates(self):
        info = self.inspect([0, 1/30, 2/30, 3/30, 7/30, 8/30])
        self.assertFalse(info.is_standard)
        self.assertTrue(any('时间戳' in reason for reason in info.reasons))
        self.assertAlmostEqual(info.nominal_fps, 30)

    def test_fractional_cfr_with_reordered_b_frames_is_not_vfr(self):
        fps = 30000/1001
        info = self.inspect([i/fps for i in [0,3,1,2,6,4,5]], rate='30000/1001', average='30000/1001')
        self.assertTrue(info.is_standard)
        self.assertAlmostEqual(info.nominal_fps, fps)

    def test_millisecond_timestamp_rounding_is_tolerated(self):
        info = self.inspect([0,.033,.067,.100,.133,.167], average='30/1')
        self.assertTrue(info.is_standard)

    def test_failed_timing_probe_does_not_silently_accept_video(self):
        with patch.object(media_normalizer, '_resolve_tool_binary', return_value='ffprobe'), patch.object(media_normalizer.subprocess, 'run', return_value=SimpleNamespace(returncode=1,stderr='bad timestamps')):
            with self.assertRaises(ValueError):
                media_normalizer._inspect_frame_timing(self.video)

    def test_standard_manual_video_does_not_transcode_or_change_arguments(self):
        args = {'target_path': str(self.video), 'output_path': str(self.root/'out.mp4')}
        info = SimpleNamespace(reasons=[],avg_fps=29.97)
        with patch.object(manual_preflight, 'is_video', return_value=True), patch.object(manual_preflight.media_normalizer, 'inspect_video', return_value=info), patch.object(manual_preflight.media_normalizer, 'inspect_and_normalize_video') as convert:
            actual,note=manual_preflight.prepare_manual_video(args)
        self.assertIs(actual,args)
        self.assertEqual(note,'')
        convert.assert_not_called()

    def test_manual_normalization_preserves_selected_times_and_ui_arguments(self):
        args = {'target_path':str(self.video),'output_path':str(self.root/'out.mp4'),'trim_frame_start':56,'trim_frame_end':280,'reference_frame_number':112,'face_enhancer_blend':30,'expression_restorer_factor':100,'output_video_encoder':'h264_nvenc'}
        before=copy.deepcopy(args)
        normalized=self.root/'target_normalized.mp4'
        old=SimpleNamespace(reasons=['可变帧率VFR(实际帧时间戳不均匀)'],avg_fps=28)
        new=SimpleNamespace(avg_fps=30)
        with patch.object(manual_preflight, 'is_video', return_value=True), patch.object(manual_preflight, 'get_userdata_dir', return_value=self.root), patch.object(manual_preflight.media_normalizer, 'inspect_video', side_effect=[old,new]), patch.object(manual_preflight.media_normalizer, 'inspect_and_normalize_video', return_value=(normalized,True,'normalized')):
            actual,note=manual_preflight.prepare_manual_video(args)
        self.assertEqual(args,before)
        self.assertEqual(actual['target_path'],str(normalized))
        self.assertEqual([actual[k] for k in ['trim_frame_start','trim_frame_end','reference_frame_number']],[60,300,120])
        for key in ['face_enhancer_blend','expression_restorer_factor','output_path','output_video_encoder']:
            self.assertEqual(actual[key],args[key])
        self.assertEqual(note,'normalized')

    def test_cancelled_normalization_does_not_mutate_input(self):
        args={'target_path':str(self.video)}
        info=SimpleNamespace(reasons=['可变帧率VFR'],avg_fps=28)
        with patch.object(manual_preflight, 'is_video', return_value=True), patch.object(manual_preflight, 'get_userdata_dir', return_value=self.root), patch.object(manual_preflight.media_normalizer, 'inspect_video', return_value=info), patch.object(manual_preflight.media_normalizer, 'inspect_and_normalize_video', side_effect=InterruptedError):
            with self.assertRaises(InterruptedError):manual_preflight.prepare_manual_video(args)
        self.assertEqual(args,{'target_path':str(self.video)})

    def test_manual_default_fps_follows_normalization_but_custom_fps_is_preserved(self):
        for requested, expected in [(28.6,30),(24,24)]:
            old=SimpleNamespace(reasons=['VFR'],avg_fps=28.6)
            new=SimpleNamespace(avg_fps=30)
            def normalize(source, directory, **kwargs):
                output=directory/'normalized.mp4'; output.write_bytes(b'normalized')
                return output,True,'normalized'
            original={'target_path':str(self.video),'output_video_fps':requested}
            with patch.object(manual_preflight,'is_video',return_value=True),patch.object(manual_preflight,'get_userdata_dir',return_value=self.root),patch.object(manual_preflight.media_normalizer,'inspect_video',side_effect=[old,new]),patch.object(manual_preflight.media_normalizer,'inspect_and_normalize_video',side_effect=normalize):
                prepared,_=manual_preflight.prepare_manual_video(original)
                self.assertEqual(prepared['output_video_fps'],expected)
                manual_preflight.cleanup_prepared_video(original,prepared)
                self.assertEqual(list((self.root/'temp/manual_preflight').iterdir()),[])


if __name__=='__main__':unittest.main()
