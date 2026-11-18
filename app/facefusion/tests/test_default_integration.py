import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import numpy as np
from facefusion import presets, state_manager, hash_helper, download, model_bootstrap, sheet_worker
from facefusion.workflows import staged_video

spec=importlib.util.spec_from_file_location('test_auto_updater', Path(__file__).resolve().parents[2]/'scripts/auto_updater.py')
updater=importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)


class DefaultIntegrationTest(unittest.TestCase):
    def test_double_click_launcher_uses_original_recipe(self):
        import re
        from facefusion import config
        from facefusion.args import apply_args
        from facefusion.program import create_program
        path = Path(__file__).resolve().parents[2]/'scripts/run-facefusion-portable.ps1'
        text = path.read_text(encoding='utf-8-sig')
        block = re.search(r'^    \$FaceFusionArgs = @\(\n(.*?)^    \)', text, re.M | re.S)
        self.assertIsNotNone(block)
        argv = re.findall(r"'([^']*)'", block.group(1))
        config.get_static_config_parser.cache_clear()
        args = vars(create_program().parse_args(argv))
        apply_args(args, state_manager.init_item)
        current = presets.snapshot()
        current['face_mask_padding'] = list(current['face_mask_padding'])
        self.assertEqual(current, presets.DEFAULTS)
        self.assertEqual(args['temp_frame_format'], 'png')
        self.assertEqual(args['workflow_strategy'], 'disk')
        config.get_static_config_parser.cache_clear()

    def test_shipped_and_empty_config_defaults_match_original_recipe(self):
        from facefusion import config
        from facefusion.args import apply_args
        from facefusion.program import create_program
        with tempfile.TemporaryDirectory() as folder:
            empty = Path(folder)/'empty.ini'
            empty.write_text('', encoding='utf-8')
            shipped = Path(__file__).resolve().parents[1]/'facefusion.ini'
            for path in [shipped, empty]:
                with self.subTest(config=str(path)):
                    config.get_static_config_parser.cache_clear()
                    args = vars(create_program().parse_args(['headless-run', '--config-path', str(path)]))
                    apply_args(args, state_manager.init_item)
                    current = presets.snapshot()
                    current['face_mask_padding'] = list(current['face_mask_padding'])
                    self.assertEqual(current, presets.DEFAULTS)
                    self.assertTrue(staged_video.supported())
                    self.assertTrue(args['upscale_1080p'])
        config.get_static_config_parser.cache_clear()

    def test_default_and_legacy_sheet_labels_use_unchanged_old_recipe(self):
        for label in ['', '默认', '没问题选这个', 'default']:
            resolved, values, warning=sheet_worker.resolve_preset_values_by_label(label)
            self.assertFalse(warning)
            self.assertEqual(values, presets.DEFAULTS)
            self.assertEqual(values['face_swapper_model'], 'inswapper_128_fp16')
            self.assertEqual(values['face_swapper_pixel_boost'], '128x128')
            self.assertEqual(values['expression_restorer_areas'], ['upper-face', 'lower-face'])
            self.assertEqual(values['expression_restorer_factor'], 80)
            self.assertEqual(values['face_enhancer_blend'], 65)
            self.assertEqual(values['face_mask_types'], ['box'])
            self.assertEqual(values['temple_correction'], 'off')
            cmd=sheet_worker.build_headless_command('python',Path('facefusion.py'),Path('source.png'),Path('input.mp4'),Path('out.mp4'),Path('temp'),Path('jobs'),values)
            self.assertEqual(cmd[cmd.index('--temple-correction')+1], 'off')
            self.assertEqual(cmd[cmd.index('--face-swapper-model')+1], 'inswapper_128_fp16')
        for label in ['clear', '旧模型']:
            _, values, warning=sheet_worker.resolve_preset_values_by_label(label)
            self.assertEqual(values['face_swapper_model'],'inswapper_128_fp16')
            self.assertEqual(values['expression_restorer_factor'],80)
            self.assertEqual(values['expression_restorer_areas'],['upper-face','lower-face'])
            self.assertEqual(values['temple_correction'],'off')

    def test_legacy_custom_is_migrated_without_changing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'presets.json'
            values=copy.deepcopy(presets.DEFAULTS)
            del values['temple_correction']
            original=json.dumps({'version':1,'presets':{'我的旧预设':values}},ensure_ascii=False)
            path.write_text(original,encoding='utf-8')
            with patch.object(presets,'PRESET_PATH',path):
                self.assertEqual(presets.read_custom()['我的旧预设']['temple_correction'],'off')
            self.assertEqual(path.read_text(encoding='utf-8'),original)

    def test_missing_model_with_existing_hash_is_recoverable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'missing.onnx'
            path.with_suffix('.hash').write_text('00000000')
            self.assertFalse(hash_helper.validate_hash(str(path)))
            self.assertEqual(download.validate_source_paths([str(path)]),([],[str(path)]))
            path.write_bytes(b'original weights')
            path.with_suffix('.hash').write_text(hash_helper.create_hash(path.read_bytes())+'\n')
            self.assertTrue(hash_helper.validate_hash(str(path)))
            path.with_suffix('.hash').write_text('<html>error</html>')
            self.assertFalse(download.is_valid_hash_file(str(path.with_suffix('.hash'))))
        self.assertIn(('models-3.0.0','inswapper_128_fp16.onnx'), model_bootstrap.PRESET_MODELS)
        self.assertNotIn(('models-3.3.0','hyperswap_1a_256.onnx'), model_bootstrap.PRESET_MODELS)

    def test_staged_progress_and_encoder_fallback_marker(self):
        from collections import deque
        task=sheet_worker.LocalTask('test',2,'https://example.com/s.png','https://example.com/v.mp4','','手动领取')
        state={}; recent=deque()
        for phase, pct in [('face metadata',45),('face swap',53.5),('expression restore',64.5),('enhance temple encode',78)]:
            sheet_worker.SheetWorkerManager._parse_headless_line(task,phase+': 50%|#####     | 5/10 [00:01<00:01, 5.00it/s]',recent,state)
            self.assertEqual(task.progress_pct,pct)
        self.assertTrue(state['saw_processing'])
        sheet_worker.SheetWorkerManager._parse_headless_line(task,'[FACEFUSION.STAGED_VIDEO] staged_encoder_failed: broken pipe',recent,state)
        self.assertTrue(state['saw_merging'])

    def test_manual_compatibility_preflight_handles_non_vfr_codecs(self):
        from facefusion import manual_preflight
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            normalized=Path(tmp)/'normalized.mp4'
            inspection=SimpleNamespace(reasons=['视频编码(hevc)'], avg_fps=30)
            with patch.object(manual_preflight,'is_video',return_value=True), patch.object(manual_preflight,'get_userdata_dir',return_value=Path(tmp)), patch.object(manual_preflight.media_normalizer,'inspect_video',return_value=inspection), patch.object(manual_preflight.media_normalizer,'inspect_and_normalize_video',return_value=(normalized,True,'已转码')) as normalize:
                result,note=manual_preflight.prepare_manual_video({'target_path':'original.mkv','output_path':'out.mkv','trim_frame_end':30})
            self.assertEqual(result['target_path'],str(normalized))
            self.assertEqual(result['output_path'],'out.mp4')
            self.assertEqual(result['trim_frame_end'],30)
            normalize.assert_called_once()

    def test_fast_path_rejects_identity_filters_and_non_neutral_weight(self):
        valid=dict(presets.BUILTINS['default']['values'],workflow_strategy='disk', face_selector_mode='one',face_tracker_score=0,temp_pixel_format='bgr24')
        with patch.object(state_manager,'get_item',side_effect=lambda key:valid.get(key)):
            self.assertTrue(staged_video.supported())
            for key, value in [('face_selector_mode','reference'),('face_selector_gender','female'),('face_swapper_weight',.7),('face_tracker_score',.5),('temp_pixel_format','bgra')]:
                previous=valid.get(key)
                valid[key]=value
                self.assertFalse(staged_video.supported(),key)
                valid[key]=previous

    def test_vram_tiers_use_free_memory_and_bounded_submission(self):
        self.assertEqual(staged_video.choose_workers(8000)['swap'],8)
        self.assertEqual(staged_video.choose_workers(4500)['restore'],2)
        self.assertEqual(staged_video.choose_workers(2000)['enhance'],1)
        with patch.object(staged_video,'check_running'):
            self.assertEqual(list(staged_video.bounded_map(lambda n:n*n,range(50),4,'test')), [n*n for n in range(50)])

    def test_high_plus_requires_more_free_vram_than_an_eight_gib_card(self):
        for free in [5000,6200,6800,8192,8499]:
            with self.subTest(free_mib=free):
                self.assertEqual(staged_video.choose_workers(free),dict(metadata=8,swap=8,restore=4,enhance=4,temple=8,tier='高'))
        for free in [8500,9500,12288,24000]:
            with self.subTest(free_mib=free):
                self.assertEqual(staged_video.choose_workers(free),dict(metadata=8,swap=10,restore=5,enhance=5,temple=10,tier='高+'))
        self.assertEqual(staged_video.choose_workers(4999)['tier'],'中')
        self.assertEqual(staged_video.choose_workers(3600)['tier'],'中')
        self.assertEqual(staged_video.choose_workers(3599)['tier'],'低')
        self.assertEqual(staged_video.choose_workers(0)['tier'],'低')


class TempleIsolationTest(unittest.TestCase):
    def test_repair_matches_roi_and_leaves_mouth_and_outside_unchanged(self):
        from facefusion.temple_correction import temple_mask, original_skin, local_skin
        points=np.full((68,2),(320,230),dtype=np.float32)
        points[0]=(160,220);points[16]=(480,220);points[8]=(320,400);points[27]=(320,200)
        points[17:22]=[(195,180),(215,170),(235,165),(255,170),(270,180)]
        points[22:27]=[(370,180),(385,170),(405,165),(425,170),(445,180)]
        points[36:42]=[(220,225),(235,215),(255,215),(270,225),(255,235),(235,235)]
        points[42:48]=[(370,225),(385,215),(405,215),(420,225),(405,235),(385,235)]
        skin=np.ones((480,640),dtype=np.float32)
        original=np.full((480,640,3),100,dtype=np.uint8)
        generated=np.full((480,640,3),180,dtype=np.uint8)
        generated[100:240,160:210]=70
        mask=temple_mask(generated.shape,points,skin,points)
        direct=original_skin(original,generated,points,mask,skin)
        roi=local_skin(original,generated,points,mask,skin)
        self.assertLessEqual(np.abs(direct.astype(int)-roi.astype(int)).max(),1)
        self.assertTrue(np.array_equal(roi[mask==0],generated[mask==0]))
        self.assertTrue(np.array_equal(roi[300:370,230:410],generated[300:370,230:410]))
        self.assertGreater(np.count_nonzero(roi!=generated),100)


class UpdateTransactionTest(unittest.TestCase):
    def package(self, root, entries):
        path=root/'update.zip'
        with zipfile.ZipFile(path,'w') as archive:
            for name,data in entries.items():
                archive.writestr(name,data)
        return path

    def test_failed_install_rolls_back_all_files_and_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); (root/'app').mkdir()
            for name in ['a.py','b.py','VERSION']:
                (root/'app'/name).write_text('old')
            personal=root/'userdata'/'private.json';personal.parent.mkdir();personal.write_text('secret')
            path=self.package(root,{'app/a.py':'new','app/b.py':'new','app/VERSION':'1.2.0'})
            replace=updater.os.replace
            def locked(source,dest):
                if Path(dest).name=='b.py':
                    raise PermissionError('locked file')
                replace(source,dest)
            with patch.object(updater.os,'replace',side_effect=locked), self.assertRaises(PermissionError):
                updater.validate_and_extract_update_zip(path,root)
            for name in ['a.py','b.py','VERSION']:
                self.assertEqual((root/'app'/name).read_text(),'old')
            self.assertEqual(personal.read_text(),'secret')

    def test_protected_paths_and_bad_digest_change_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for name in ['userdata/token.json','../outside.py','/app/absolute.py','app/x.py:stream','app/../x.py','runtime/python.exe']:
                path=self.package(root,{'app/a.py':'new',name:'bad'})
                with self.assertRaises(ValueError):
                    updater.validate_and_extract_update_zip(path,root)
                self.assertFalse((root/'app/a.py').exists())
            path=self.package(root,{'app/a.py':'new'})
            with self.assertRaises(ValueError):
                updater.validate_and_extract_update_zip(path,root,expected_sha256='0'*64)
            self.assertFalse((root/'app/a.py').exists())

    def test_upgrade_then_offline_start_preserves_models_and_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'app').mkdir();(root/'app/VERSION').write_text('1.1.0')
            model=root/'userdata/models/existing.onnx';model.parent.mkdir(parents=True);model.write_bytes(b'cached')
            credentials=root/'userdata/token.json';credentials.write_text('private')
            payload=self.package(root,{'app/a.py':'new','app/VERSION':'1.2.0'}).read_bytes()
            release={'tag_name':'v1.2.0','assets':[{'name':updater.UPDATE_ASSET_NAME,'browser_download_url':'https://github.com/example/update.zip','digest':'sha256:'+hashlib.sha256(payload).hexdigest()}]}
            with patch.object(updater,'fetch_latest_release_metadata',return_value=release),patch.object(updater,'_http_get_bytes',return_value=payload):
                result=updater.check_and_apply_update(target_root=root)
            self.assertEqual(result.status,'updated',result.message)
            self.assertEqual(updater.read_local_version(root),'1.2.0')
            self.assertEqual(model.read_bytes(),b'cached');self.assertEqual(credentials.read_text(),'private')
            with patch.object(updater,'fetch_latest_release_metadata',side_effect=OSError('offline')):
                result=updater.check_and_apply_update(target_root=root)
            self.assertEqual(result.status,'error')
            self.assertEqual(updater.read_local_version(root),'1.2.0')


if __name__=='__main__':
    unittest.main()
