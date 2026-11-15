import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import unquote

from facefusion import media_normalizer, sheet_worker
from facefusion.google_sheets_client import (
    SHEET_STATUS_COMPLETED,
    SHEET_STATUS_FAILED,
    SHEET_STATUS_IN_PROGRESS,
    SHEET_STATUS_PENDING,
    GoogleDriveClient,
    GoogleOAuthManager,
    GoogleSheetsCoordinator,
    col_index_to_letter,
    parse_drive_id,
    parse_spreadsheet_ref,
    sanitize_filename_stem,
)


class FakeResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self) -> Any:
        return self._payload


class InMemorySheetsHttp:
    """In-memory Google Sheets v4 simulator supporting arbitrary column/row shifts and race hooks."""

    def __init__(self, rows: List[List[str]], sheet_title: str = '口播需求表') -> None:
        self.sheet_title = sheet_title
        self.rows = [list(r) for r in rows]
        self.write_calls: int = 0
        self.last_value_input_option: str = ''
        self.last_read_url: str = ''
        self.read_ranges: List[str] = []
        self.after_batch_update_hook = None

    @staticmethod
    def _letter_to_col(letters: str) -> int:
        idx = 0
        for ch in letters:
            idx = idx * 26 + (ord(ch.upper()) - ord('A') + 1)
        return idx - 1

    def _set_cell(self, a1_range: str, value: str) -> None:
        cell_part = a1_range.split('!')[-1]
        col_letters = ''.join(ch for ch in cell_part if ch.isalpha())
        row_digits = ''.join(ch for ch in cell_part if ch.isdigit())
        col_idx = self._letter_to_col(col_letters)
        row_idx = int(row_digits) - 1
        while len(self.rows) <= row_idx:
            self.rows.append([])
        while len(self.rows[row_idx]) <= col_idx:
            self.rows[row_idx].append('')
        self.rows[row_idx][col_idx] = value

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = 45.0,
        max_retries: int = 3
    ) -> FakeResponse:
        if method == 'GET' and '/values/' in url:
            decoded_url = unquote(url)
            self.last_read_url = decoded_url
            range_part = decoded_url.split('/values/', 1)[1].split('?', 1)[0]
            self.read_ranges.append(range_part)
            if '!' in range_part:
                sub = range_part.rsplit('!', 1)[1]
                if sub == '1:1':
                    return FakeResponse(200, {'values': [list(self.rows[0])] if self.rows else []})
                if ':' in sub:
                    left, right = sub.split(':', 1)
                    if left.isdigit() and right.isdigit() and left == right:
                        r_idx = int(left) - 1
                        row_data = [list(self.rows[r_idx])] if 0 <= r_idx < len(self.rows) else []
                        return FakeResponse(200, {'values': row_data})
                    if left.isalpha() and left == right:
                        c_idx = self._letter_to_col(left)
                        sliced = [[r[c_idx]] if c_idx < len(r) else [] for r in self.rows]
                        return FakeResponse(200, {'values': sliced})
            return FakeResponse(200, {'values': [list(r) for r in self.rows]})
        if method == 'GET':
            return FakeResponse(200, {
                'properties': {'title': '测试换脸总表'},
                'sheets': [{'properties': {'sheetId': 0, 'title': self.sheet_title}}]
            })
        if method == 'POST' and url.endswith(':batchUpdate'):
            self.write_calls += 1
            self.last_value_input_option = str((json_body or {}).get('valueInputOption', ''))
            for item in (json_body or {}).get('data', []):
                rng = unquote(item['range'])
                val = str(item['values'][0][0])
                self._set_cell(rng, val)
            if self.after_batch_update_hook:
                self.after_batch_update_hook()
            return FakeResponse(200, {'totalUpdatedCells': len((json_body or {}).get('data', []))})
        raise AssertionError(f'Unexpected call: {method} {url}')


class SheetCoordinatorAndWorkerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self._orig_recent_dir = sheet_worker.LOCAL_RECENT_OUTPUT_DIR
        self._orig_temp_root = sheet_worker.SHEET_TEMP_ROOT
        sheet_worker.LOCAL_RECENT_OUTPUT_DIR = self.tmp_path / 'recent_outputs'
        sheet_worker.SHEET_TEMP_ROOT = self.tmp_path / 'sheet_temp'
        self.token_path = self.tmp_path / 'token.json'
        self.token_path.write_text(json.dumps({
            'client_id': 'cid',
            'client_secret': 'csec',
            'refresh_token': 'rtok',
            'access_token': 'atok',
            'expires_at': 9999999999.0
        }), encoding='utf-8')
        self.oauth = GoogleOAuthManager(token_path=self.token_path)

    def tearDown(self) -> None:
        sheet_worker.LOCAL_RECENT_OUTPUT_DIR = self._orig_recent_dir
        sheet_worker.SHEET_TEMP_ROOT = self._orig_temp_root
        self.tmp.cleanup()

    def test_url_and_column_helpers(self) -> None:
        sid, gid = parse_spreadsheet_ref('https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz/edit#gid=12345')
        self.assertEqual(sid, '1AbCdEfGhIjKlMnOpQrStUvWxYz')
        self.assertEqual(gid, 12345)
        self.assertEqual(parse_drive_id('https://drive.google.com/file/d/1XyZ_0123456789abcdef/view?usp=sharing'), '1XyZ_0123456789abcdef')
        self.assertEqual(col_index_to_letter(0), 'A')
        self.assertEqual(col_index_to_letter(25), 'Z')
        self.assertEqual(col_index_to_letter(26), 'AA')

    def test_dynamic_headers_row_insertion_and_column_reorder_resilience(self) -> None:
        # Shuffled columns + extra business columns + missing system columns initially
        initial_rows = [
            ['编导', '目标视频', '无关备注', '源人脸图片', '场景预设'],
            ['Alex', 'https://example.com/v1.mp4', '加急', 'https://example.com/f1.jpg', '解决嘴巴张不开'],
            ['Jordan', 'https://example.com/v2.mp4', '', 'https://example.com/f2.jpg', '']
        ]
        sim = InMemorySheetsHttp(initial_rows)
        coord = GoogleSheetsCoordinator(self.oauth, '1AbCdEfGhIjKlMnOpQrStUvWxYz', http_helper=sim)

        _, _, tasks = coord.fetch_tasks_snapshot()
        self.assertEqual(len(tasks), 2)
        self.assertTrue(all(t.is_claimable for t in tasks))

        # Claim row 2 (auto-creates missing system headers on row 1 and generates task_id)
        claimed, conflicts = coord.claim_specific_tasks(
            [(2, 'https://example.com/f1.jpg', 'https://example.com/v1.mp4')],
            worker_name='节点A',
            verify_delay_sec=0.0
        )
        self.assertEqual(conflicts, [])
        self.assertEqual(len(claimed), 1)
        task_id = claimed[0].task_id
        self.assertTrue(task_id.startswith('FF-'))
        self.assertEqual(claimed[0].status, SHEET_STATUS_IN_PROGRESS)

        # Now simulate an operator:
        # 1) Inserting 3 new rows above our task AND sorting/reordering rows
        # 2) Inserting a new column at column A (shifting EVERY column index right by 1!)
        sim.rows = [['新增序号列'] + r for r in sim.rows]
        header = sim.rows[0]
        data_rows = sim.rows[1:]
        sim.rows = [
            header,
            ['1', '新编导', 'https://example.com/new.mp4', '', 'https://example.com/new.jpg', '', '', '', '', '', ''],
            data_rows[1],  # v2 moved above v1
            ['3', '空行', '', '', '', '', '', '', '', '', ''],
            data_rows[0],  # our claimed task is now on row 5 with shifted columns!
        ]

        updated_row_num = coord.complete_task(task_id, 'https://drive.google.com/file/d/finished123456789/view', '耗时 3分10秒')
        self.assertEqual(updated_row_num, 5)

        _, col_map, after_rows = coord.fetch_tasks_snapshot()
        by_id = {r.task_id: r for r in after_rows if r.task_id}
        self.assertEqual(by_id[task_id].status, SHEET_STATUS_COMPLETED)
        self.assertEqual(by_id[task_id].output_url, 'https://drive.google.com/file/d/finished123456789/view')
        # Unrelated row was untouched
        self.assertEqual(sim.rows[2][col_map['source_image']], 'https://example.com/f2.jpg')

    def test_conflict_detection_both_precheck_and_concurrent_race(self) -> None:
        rows = [
            ['源人脸图片', '目标视频', '任务编号', '任务状态', '领取节点', '成品链接', '处理备注'],
            ['https://ex.com/a.jpg', 'https://ex.com/a.mp4', 'FF-OLD-1111', SHEET_STATUS_IN_PROGRESS, 'Peer-A', '', ''],
            ['https://ex.com/b.jpg', 'https://ex.com/b.mp4', '', '', '', '', ''],
            ['https://ex.com/c.jpg', 'https://ex.com/c.mp4', '', '', '', '', '']
        ]
        sim = InMemorySheetsHttp(rows)
        coord = GoogleSheetsCoordinator(self.oauth, '1AbCdEfGhIjKlMnOpQrStUvWxYz', http_helper=sim)

        # Simulate colleague 'Peer-B' overwriting row 3 right after our write before verification!
        def _concurrent_colleague_overwrite() -> None:
            sim.rows[2][2] = 'FF-COLLEAGUE-9999'
            sim.rows[2][3] = SHEET_STATUS_IN_PROGRESS
            sim.rows[2][4] = 'Peer-B'

        sim.after_batch_update_hook = _concurrent_colleague_overwrite

        claimed, conflicts = coord.claim_specific_tasks(
            [
                (2, 'https://ex.com/a.jpg', 'https://ex.com/a.mp4'),  # Already claimed before write
                (3, 'https://ex.com/b.jpg', 'https://ex.com/b.mp4'),  # Lost millisecond race
                (4, 'https://ex.com/c.jpg', 'https://ex.com/c.mp4')   # Clean win
            ],
            worker_name='Worker-Local',
            verify_delay_sec=0.0
        )

        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0].source_url, 'https://ex.com/c.jpg')
        self.assertEqual(len(conflicts), 2)
        self.assertIn('Peer-A', conflicts[0])
        self.assertIn('Peer-B', conflicts[1])

    def test_auto_claim_saturation_limit_vs_manual_unlimited(self) -> None:
        rows = [
            ['源人脸图片', '目标视频', '任务编号', '任务状态', '领取节点', '成品链接', '处理备注'],
            ['https://ex.com/1.jpg', 'https://ex.com/1.mp4', '', '', '', '', ''],
            ['https://ex.com/2.jpg', 'https://ex.com/2.mp4', '', '', '', '', ''],
            ['https://ex.com/3.jpg', 'https://ex.com/3.mp4', '', '', '', '', ''],
            ['https://ex.com/4.jpg', 'https://ex.com/4.mp4', '', '', '', '', ''],
            ['https://ex.com/5.jpg', 'https://ex.com/5.mp4', '', '', '', '', '']
        ]
        sim = InMemorySheetsHttp(rows)
        coord = GoogleSheetsCoordinator(self.oauth, '1AbCdEfGhIjKlMnOpQrStUvWxYz', http_helper=sim)

        mgr = sheet_worker.SheetWorkerManager(
            config_path=self.tmp_path / 'worker.json',
            token_path=self.token_path
        )
        mgr.config.worker_name = '自动节点'
        mgr.config.spreadsheet_url = '1AbCdEfGhIjKlMnOpQrStUvWxYz'
        # Prevent background execution loop from popping tasks during queue assertions
        mgr.ensure_background_threads = lambda: None  # type: ignore[method-assign]
        mgr._make_coordinator = lambda: GoogleSheetsCoordinator(self.oauth, '1AbCdEfGhIjKlMnOpQrStUvWxYz', http_helper=sim)  # type: ignore[method-assign]

        # Patch verify delay to 0 for instant unit test
        orig_claim_specific = GoogleSheetsCoordinator.claim_specific_tasks
        GoogleSheetsCoordinator.claim_specific_tasks = lambda self_c, t, worker_name, verify_delay_sec=0.0, allow_retry_failed=False: orig_claim_specific(self_c, t, worker_name, verify_delay_sec=0.0, allow_retry_failed=allow_retry_failed)  # type: ignore[assignment]
        try:
            # 1. Idle (0 active) -> auto-claim must take AT MOST 2 tasks (1 running + 1 backlog)
            mgr.trigger_auto_claim_once()
            self.assertEqual(mgr.active_task_count(), 2)

            # 2. Saturated (2 active) -> auto-claim must skip without claiming a 3rd task
            writes_before = sim.write_calls
            msg = mgr.trigger_auto_claim_once()
            self.assertIn('饱和跳过', msg)
            self.assertEqual(mgr.active_task_count(), 2)
            self.assertEqual(sim.write_calls, writes_before)  # Zero wasted API writes!

            # 3. Manual selection has NO saturation limit: claim remaining 3 tasks manually
            mgr.refresh_sheet_tasks()
            remaining_tokens = [
                json.dumps({'row': r.row_number, 'src': r.source_url, 'tgt': r.target_url})
                for r in mgr.cached_sheet_rows if r.is_claimable
            ]
            self.assertEqual(len(remaining_tokens), 3)
            added_cnt, conflicts = mgr.claim_manual_selection(remaining_tokens)
            self.assertEqual(added_cnt, 3)
            self.assertEqual(conflicts, [])
            self.assertEqual(mgr.active_task_count(), 5)

            # 4. Release queued tasks back to sheet -> clears task_id & restores claimability
            mgr.release_all_queued_tasks()
            self.assertEqual(mgr.active_task_count(), 0)
            _, _, released_snapshot = coord.fetch_tasks_snapshot()
            self.assertTrue(all(r.is_claimable for r in released_snapshot))
            self.assertTrue(all(r.status == SHEET_STATUS_PENDING for r in released_snapshot))
        finally:
            GoogleSheetsCoordinator.claim_specific_tasks = orig_claim_specific  # type: ignore[assignment]

    def test_conditional_video_normalizer_skips_standard_and_transcodes_nonstandard(self) -> None:
        ffmpeg_bin = media_normalizer._resolve_tool_binary('ffmpeg')

        std_mp4 = self.tmp_path / 'standard.mp4'
        subprocess.run([
            ffmpeg_bin, '-y', '-hide_banner', '-loglevel', 'error',
            '-f', 'lavfi', '-i', 'testsrc=size=320x240:rate=30:duration=1',
            '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=1',
            '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac',
            str(std_mp4)
        ], check=True)

        out_path, was_reencoded, note = media_normalizer.inspect_and_normalize_video(std_mp4, self.tmp_path / 'w1')
        self.assertFalse(was_reencoded)
        self.assertEqual(out_path, std_mp4)
        self.assertIn('免重编码', note)

        # Non-standard video: mpeg4 codec + odd resolution (321x241 -> mpeg4 needs even, so 320x240 mpeg4 in .avi)
        nonstd_avi = self.tmp_path / 'nonstandard.avi'
        subprocess.run([
            ffmpeg_bin, '-y', '-hide_banner', '-loglevel', 'error',
            '-f', 'lavfi', '-i', 'testsrc=size=320x240:rate=25:duration=1',
            '-c:v', 'mpeg4', '-pix_fmt', 'yuv420p',
            str(nonstd_avi)
        ], check=True)

        out_path2, was_reencoded2, note2 = media_normalizer.inspect_and_normalize_video(nonstd_avi, self.tmp_path / 'w2')
        self.assertTrue(was_reencoded2)
        self.assertTrue(out_path2.is_file())
        self.assertIn('已自动标准化', note2)
        recheck = media_normalizer.inspect_video(out_path2)
        self.assertTrue(recheck.is_standard)

    def test_gradio_sheet_worker_tab_renders_and_binds(self) -> None:
        import gradio
        from facefusion.uis.components import sheet_worker as sheet_worker_ui

        with gradio.Blocks() as demo:
            sheet_worker_ui.render()
            sheet_worker_ui.listen()
        self.assertIsNotNone(demo)

        # Verify unconfigured action automatically opens CONFIG_ACCORDION to guide the user
        old_name = sheet_worker_ui.WORKER_MANAGER.config.worker_name
        sheet_worker_ui.WORKER_MANAGER.config.worker_name = ''
        try:
            ref_out = sheet_worker_ui.on_refresh_sheet(False)
            self.assertEqual(ref_out[0].constructor_args.get('open'), True)
            auto_out = sheet_worker_ui.on_toggle_auto_claim(True)
            self.assertEqual(auto_out[0].get('value'), False)
            self.assertEqual(auto_out[1].constructor_args.get('open'), True)
        finally:
            sheet_worker_ui.WORKER_MANAGER.config.worker_name = old_name

    def test_spreadsheet_switch_stale_sheet_title_and_owned_task_recovery(self) -> None:
        rows = [
            ['源人脸图片', '目标视频', '任务编号', '任务状态', '领取节点', '成品链接', '处理备注'],
            ['https://ex.com/m1.jpg', 'https://ex.com/m1.mp4', 'FF-1114-010101-MINE', SHEET_STATUS_IN_PROGRESS, 'Worker-Alex', '', '已领取'],
            ['https://ex.com/p1.jpg', 'https://ex.com/p1.mp4', 'FF-1114-010102-PEER', SHEET_STATUS_IN_PROGRESS, 'Worker-Peer', '', '已领取'],
            ['https://ex.com/f1.jpg', 'https://ex.com/f1.mp4', 'FF-1114-010103-FAIL', SHEET_STATUS_FAILED, 'Worker-Alex', '', '上次源图403']
        ]
        sim = InMemorySheetsHttp(rows, sheet_title='11月新需求页签')

        mgr = sheet_worker.SheetWorkerManager(
            config_path=self.tmp_path / 'worker_switch.json',
            token_path=self.token_path
        )
        mgr.ensure_background_threads = lambda: None  # type: ignore[method-assign]
        mgr.config.worker_name = 'Worker-Alex'
        mgr.config.spreadsheet_url = 'https://docs.google.com/spreadsheets/d/1OldSpreadsheetId123456789/edit'
        mgr.config.sheet_title = '10月旧页签'

        # Switch to a brand new spreadsheet URL while UI dropdown still passes stale '10月旧页签'
        mgr.update_config(
            worker_name='Worker-Alex',
            spreadsheet_url='https://docs.google.com/spreadsheets/d/1NewSpreadsheetId987654321/edit',
            sheet_title='10月旧页签',
            drive_folder_url=''
        )
        self.assertEqual(mgr.config.sheet_title, '')

        # Even if Coordinator is constructed directly with a stale sheet_title, resolve_sheet_title falls back safely
        coord_stale = GoogleSheetsCoordinator(self.oauth, '1NewSpreadsheetId987654321', sheet_title='不存在的旧页签', http_helper=sim)
        self.assertEqual(coord_stale.resolve_sheet_title(), '11月新需求页签')

        mgr._make_coordinator = lambda: GoogleSheetsCoordinator(self.oauth, '1NewSpreadsheetId987654321', sheet_title=mgr.config.sheet_title or None, http_helper=sim)  # type: ignore[method-assign]
        _, titles, selected = mgr.test_and_load_sheets()
        self.assertEqual(titles, ['11月新需求页签'])
        self.assertEqual(selected, '11月新需求页签')

        # test_and_load_sheets must have automatically recovered ONLY 'Worker-Alex's unfinished task (not 'Worker-Peer's)
        self.assertEqual(mgr.active_task_count(), 1)
        self.assertEqual(mgr.queued_tasks[0].task_id, 'FF-1114-010101-MINE')
        self.assertEqual(mgr.queued_tasks[0].claim_mode, '断点恢复')

        # Auto-claim must NOT claim the '制作失败' row; manual claim WITH allow_retry_failed=True CAN re-claim it!
        mgr.queued_tasks.clear()
        mgr.trigger_auto_claim_once()
        self.assertEqual(mgr.active_task_count(), 0)

        orig_claim = GoogleSheetsCoordinator.claim_specific_tasks
        GoogleSheetsCoordinator.claim_specific_tasks = lambda self_c, t, worker_name, verify_delay_sec=0.0, allow_retry_failed=False: orig_claim(self_c, t, worker_name, verify_delay_sec=0.0, allow_retry_failed=allow_retry_failed)  # type: ignore[assignment]
        try:
            retry_token = json.dumps({'row': 4, 'src': 'https://ex.com/f1.jpg', 'tgt': 'https://ex.com/f1.mp4'})
            cnt, conflicts = mgr.claim_manual_selection([retry_token], allow_retry_failed=True)
            self.assertEqual(cnt, 1)
            self.assertEqual(conflicts, [])
            self.assertNotEqual(mgr.queued_tasks[0].task_id, 'FF-1114-010103-FAIL')
        finally:
            GoogleSheetsCoordinator.claim_specific_tasks = orig_claim  # type: ignore[assignment]

    def test_oauth_web_client_guard_drive_helpers_and_ffmpeg_image_fallback(self) -> None:
        # 1. Reject 'web' OAuth client JSON with clear Desktop App guidance
        web_json = self.tmp_path / 'web_secret.json'
        web_json.write_text(json.dumps({'web': {'client_id': 'x', 'client_secret': 'y'}}), encoding='utf-8')
        with self.assertRaises(ValueError) as ctx:
            GoogleOAuthManager.load_client_config(web_json)
        self.assertIn('桌面应用', str(ctx.exception))

        # 2. Reject Google Drive folder link mistaken for media file link
        drive = GoogleDriveClient(self.oauth)
        with self.assertRaises(ValueError) as folder_ctx:
            drive.download_media('https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUv', self.tmp_path, 'src', '.jpg')
        self.assertIn('文件夹', str(folder_ctx.exception))

        # 3. Extract virus-scan confirmation URL from >100MB Google Drive HTML
        sample_html = (
            '<form id="download-form" action="https://drive.usercontent.google.com/download" method="get">'
            '<input type="hidden" name="id" value="1FileId123456789">'
            '<input type="hidden" name="export" value="download">'
            '<input type="hidden" name="confirm" value="t">'
            '<input type="hidden" name="uuid" value="abc-uuid-999">'
            '</form>'
        )
        confirm_url = GoogleDriveClient._extract_drive_confirm_url(sample_html)
        self.assertIsNotNone(confirm_url)
        assert confirm_url is not None
        self.assertIn('uuid=abc-uuid-999', confirm_url)
        self.assertEqual(sanitize_filename_stem('03 口播素材(最终版).mov'), '03_口播素材(最终版)')

        # 4. FFmpeg image fallback in prepare_source_image (e.g. raw video frame / container Pillow can't open)
        ffmpeg_bin = media_normalizer._resolve_tool_binary('ffmpeg')
        hevc_frame = self.tmp_path / 'iphone_like_hevc_still.mp4'
        subprocess.run([
            ffmpeg_bin, '-y', '-hide_banner', '-loglevel', 'error',
            '-f', 'lavfi', '-i', 'color=c=blue:s=256x256:d=0.1',
            '-frames:v', '1', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
            str(hevc_frame)
        ], check=True)
        converted_jpg, img_note = media_normalizer.prepare_source_image(hevc_frame, self.tmp_path / 'img_work')
        self.assertTrue(converted_jpg.is_file())
        self.assertIn('FFmpeg', img_note)

    def test_abort_returns_to_sheet_and_upload_failure_preserves_backup_for_retry(self) -> None:
        rows = [
            ['源人脸图片', '目标视频', '任务编号', '任务状态', '领取节点', '成品链接', '处理备注'],
            ['https://ex.com/s1.jpg', 'https://ex.com/v1.mp4', 'FF-1114-020201-ABRT', SHEET_STATUS_IN_PROGRESS, '测试节点', '', '已领取'],
            ['https://ex.com/s2.jpg', 'https://ex.com/v2.mp4', 'FF-1114-020202-BKUP', SHEET_STATUS_IN_PROGRESS, '测试节点', '', '已领取']
        ]
        sim = InMemorySheetsHttp(rows)
        mgr = sheet_worker.SheetWorkerManager(
            config_path=self.tmp_path / 'worker_backup.json',
            token_path=self.token_path
        )
        mgr.ensure_background_threads = lambda: None  # type: ignore[method-assign]
        mgr.config.worker_name = '测试节点'
        mgr.config.spreadsheet_url = '1TestSheetId123456789'
        mgr._make_coordinator = lambda: GoogleSheetsCoordinator(self.oauth, '1TestSheetId123456789', http_helper=sim)  # type: ignore[method-assign]

        # Part A: Abort current task -> returns row 2 to 待处理 (claimable) instead of 制作失败
        abort_task = sheet_worker.LocalTask(
            task_id='FF-1114-020201-ABRT',
            initial_row_number=2,
            source_url='https://ex.com/s1.jpg',
            target_url='https://ex.com/v1.mp4',
            preset_name='没问题选这个',
            claim_mode='手动领取',
            abort_requested=True,
            abort_return_to_sheet=True
        )
        orig_dl = GoogleDriveClient.download_media_with_name
        orig_upload = GoogleDriveClient.upload_and_share_detailed
        orig_backup_dir = sheet_worker.FAILED_OUTPUT_BACKUP_DIR
        sheet_worker.FAILED_OUTPUT_BACKUP_DIR = self.tmp_path / 'saved_backups'
        try:
            def _fake_dl(self_d: Any, url: str, dest_dir: Path, stem: str, ext: str, progress_cb: Any = None) -> Any:
                dest_dir.mkdir(parents=True, exist_ok=True)
                p = dest_dir / f'{stem}{ext}'
                p.write_bytes(b'dummy')
                return p, '口播测试片.mp4'

            GoogleDriveClient.download_media_with_name = _fake_dl  # type: ignore[assignment]
            mgr._run_single_task(abort_task)
            self.assertEqual(abort_task.local_status, sheet_worker.LOCAL_STATUS_RETURNED)
            _, _, snap_after_abort = mgr._make_coordinator().fetch_tasks_snapshot()
            row2 = [r for r in snap_after_abort if r.row_number == 2][0]
            self.assertTrue(row2.is_claimable)
            self.assertEqual(row2.status, SHEET_STATUS_PENDING)
            self.assertEqual(row2.task_id, '')

            # Part B: Swap succeeds, Drive upload throws network error -> MP4 preserved in saved_backups & one-click retry completes it!
            from PIL import Image
            src_img = self.tmp_path / 'face.jpg'
            Image.new('RGB', (128, 128), (200, 150, 120)).save(src_img)
            ffmpeg_bin = media_normalizer._resolve_tool_binary('ffmpeg')
            std_vid = self.tmp_path / 'vid.mp4'
            subprocess.run([
                ffmpeg_bin, '-y', '-hide_banner', '-loglevel', 'error',
                '-f', 'lavfi', '-i', 'testsrc=size=320x240:rate=30:duration=1',
                '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(std_vid)
            ], check=True)

            def _fake_dl_real(self_d: Any, url: str, dest_dir: Path, stem: str, ext: str, progress_cb: Any = None) -> Any:
                dest_dir.mkdir(parents=True, exist_ok=True)
                if ext == '.jpg':
                    return src_img, '模特A.jpg'
                return std_vid, '口播第8期.mp4'

            def _fake_swap(task: Any, source_image: Path, target_video: Path, output_video: Path, **kw: Any) -> None:
                output_video.write_bytes(b'fake-rendered-mp4-video-bytes')

            upload_should_fail = {'flag': True}

            def _fake_upload(self_d: Any, file_path: Path, upload_name: str, folder_url_or_id: str = '', progress_cb: Any = None) -> Any:
                if upload_should_fail['flag']:
                    raise ValueError('模拟代理闪断上传失败')
                return f'https://drive.google.com/file/d/recovered_{upload_name}/view', ''

            GoogleDriveClient.download_media_with_name = _fake_dl_real  # type: ignore[assignment]
            GoogleDriveClient.upload_and_share_detailed = _fake_upload  # type: ignore[assignment]
            mgr._execute_headless_swap = _fake_swap  # type: ignore[method-assign]

            bk_task = sheet_worker.LocalTask(
                task_id='FF-1114-020202-BKUP',
                initial_row_number=3,
                source_url='https://ex.com/s2.jpg',
                target_url='https://ex.com/v2.mp4',
                preset_name='没问题选这个',
                claim_mode='手动领取'
            )
            mgr._run_single_task(bk_task)
            mgr.history_tasks.appendleft(bk_task)

            self.assertEqual(bk_task.local_status, sheet_worker.LOCAL_STATUS_FAILED)
            saved_files = mgr.saved_backup_files()
            self.assertEqual(len(saved_files), 1)
            self.assertEqual(saved_files[0].name, '口播第8期_FF-1114-020202-BKUP.mp4')

            # Now network recovers -> one-click retry_saved_uploads finishes upload & updates Google Sheet!
            upload_should_fail['flag'] = False
            retry_msg = mgr.retry_saved_uploads()
            self.assertIn('已成功补传并回写 1 条', retry_msg)
            self.assertEqual(mgr.saved_backup_files(), [])
            self.assertEqual(bk_task.local_status, sheet_worker.LOCAL_STATUS_COMPLETED)
            _, _, snap_final = mgr._make_coordinator().fetch_tasks_snapshot()
            row3 = [r for r in snap_final if r.row_number == 3][0]
            self.assertEqual(row3.status, SHEET_STATUS_COMPLETED)
            self.assertIn('口播第8期_FF-1114-020202-BKUP.mp4', row3.output_url)
        finally:
            GoogleDriveClient.download_media_with_name = orig_dl  # type: ignore[assignment]
            GoogleDriveClient.upload_and_share_detailed = orig_upload  # type: ignore[assignment]
            sheet_worker.FAILED_OUTPUT_BACKUP_DIR = orig_backup_dir

    def test_instant_and_job_runner_start_never_hangs_on_empty_click(self) -> None:
        import threading
        import time
        from facefusion import process_manager, state_manager
        from facefusion.jobs import job_manager
        from facefusion.uis.components import instant_runner, job_runner

        jobs_dir = str(self.tmp_path / 'ui_jobs')
        old_jobs_path = state_manager.get_item('jobs_path')
        old_create_and_run = instant_runner.create_and_run_job
        state_manager.init_item('jobs_path', jobs_dir)
        job_manager.init_jobs(jobs_dir)
        instant_runner.create_and_run_job = lambda _step_args: False  # type: ignore[assignment]
        process_manager.end()
        try:
            # 1. Simulate concurrent click of start() + run() when no source/target/job is configured
            results: Dict[str, Any] = {}

            def _call_instant_start() -> None:
                results['instant_start'] = instant_runner.start()

            def _call_instant_run() -> None:
                time.sleep(0.05)
                results['instant_run'] = instant_runner.run()

            t1 = threading.Thread(target=_call_instant_start)
            t2 = threading.Thread(target=_call_instant_run)
            t0 = time.monotonic()
            t1.start()
            t2.start()
            t1.join(timeout=3.0)
            t2.join(timeout=3.0)
            self.assertFalse(t1.is_alive(), 'instant_runner.start() hung on empty click!')
            self.assertIn('instant_run', results)
            self.assertLess(time.monotonic() - t0, 2.5)
            # Early abort must NOT hide the Start button
            start_btn, stop_btn = results['instant_start']
            self.assertIsNot(start_btn.constructor_args.get('visible'), False)
            self.assertIsNot(stop_btn.constructor_args.get('visible'), True)

            # 2. Same check for job_runner.start() + job_runner.run('job-run', 'none')
            def _call_job_start() -> None:
                results['job_start'] = job_runner.start()

            def _call_job_run() -> None:
                time.sleep(0.05)
                results['job_run'] = job_runner.run('job-run', 'none')

            j1 = threading.Thread(target=_call_job_start)
            j2 = threading.Thread(target=_call_job_run)
            j1.start()
            j2.start()
            j1.join(timeout=3.0)
            j2.join(timeout=3.0)
            self.assertFalse(j1.is_alive(), 'job_runner.start() hung on empty click!')
            self.assertIn('job_run', results)

            # 3. When process_manager enters processing, start() hides Start and shows Stop
            process_manager.start()
            active_start_btn, active_stop_btn = instant_runner.start()
            self.assertEqual(active_start_btn.constructor_args.get('visible'), False)
            self.assertEqual(active_stop_btn.constructor_args.get('visible'), True)
        finally:
            process_manager.end()
            instant_runner.create_and_run_job = old_create_and_run  # type: ignore[assignment]
            state_manager.init_item('jobs_path', old_jobs_path)

    def test_standard_mov_lossless_remux_to_mp4(self) -> None:
        ffmpeg_bin = media_normalizer._resolve_tool_binary('ffmpeg')
        std_mov = self.tmp_path / 'iphone_clip.mov'
        subprocess.run([
            ffmpeg_bin, '-y', '-hide_banner', '-loglevel', 'error',
            '-f', 'lavfi', '-i', 'testsrc=size=640x360:rate=30:duration=1',
            '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100:duration=1',
            '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-shortest',
            str(std_mov)
        ], check=True)

        out_vid, was_reencoded, note = media_normalizer.inspect_and_normalize_video(
            std_mov,
            self.tmp_path / 'mov_work'
        )
        self.assertFalse(was_reencoded)
        self.assertIn('MOV无损封装MP4', note)
        self.assertEqual(out_vid.suffix.lower(), '.mp4')
        self.assertTrue(out_vid.is_file() and out_vid.stat().st_size > 0)
        remux_info = media_normalizer.inspect_video(out_vid)
        self.assertTrue(remux_info.is_standard)

    def test_local_recent_outputs_retention_10_and_restart_keeps_auto_claim_off(self) -> None:
        sample_mp4 = self.tmp_path / 'src_sample.mp4'
        sample_mp4.write_bytes(b'valid-mp4-payload')
        out_dir = self.tmp_path / 'recent_outputs'

        for i in range(1, 14):
            sheet_worker.save_and_prune_recent_output(
                sample_mp4,
                f'成品_{i:02d}_FF-1114-0000{i:02d}-ABCD.mp4',
                output_dir=out_dir,
                max_keep=10
            )

        mgr = sheet_worker.SheetWorkerManager(
            config_path=self.tmp_path / 'worker_cfg.json',
            token_path=self.token_path
        )
        kept = mgr.recent_local_output_files()
        self.assertEqual(len(kept), 10)
        kept_names = [p.name for p in kept]
        self.assertEqual(kept_names[0], '成品_13_FF-1114-000013-ABCD.mp4')
        self.assertEqual(kept_names[-1], '成品_04_FF-1114-000004-ABCD.mp4')
        for dropped_idx in (1, 2, 3):
            self.assertNotIn(f'成品_{dropped_idx:02d}_FF-1114-0000{dropped_idx:02d}-ABCD.mp4', kept_names)

        # Verify auto-claim does NOT persist or auto-restore across restarts
        mgr.update_config('节点A', '1AbCdEfGhIjKlMnOpQrStUvWxYz', '口播需求表', '')
        mgr.set_auto_claim(True)
        self.assertTrue(mgr.auto_claim_enabled)
        mgr.config.save(mgr.config_path)

        restarted_mgr = sheet_worker.SheetWorkerManager(
            config_path=mgr.config_path,
            token_path=self.token_path
        )
        self.assertFalse(restarted_mgr.auto_claim_enabled)

    def test_selective_queue_release_raw_formula_safety_and_logout(self) -> None:
        rows = [
            ['源人脸图片', '目标视频'],
            ['https://ex.com/s1.jpg', 'https://ex.com/v1.mp4'],
            ['https://ex.com/s2.jpg', 'https://ex.com/v2.mp4'],
            ['https://ex.com/s3.jpg', 'https://ex.com/v3.mp4'],
        ]
        sim = InMemorySheetsHttp(rows)
        mgr = sheet_worker.SheetWorkerManager(
            config_path=self.tmp_path / 'sel_cfg.json',
            token_path=self.token_path
        )
        mgr.config.worker_name = '节点A'
        mgr.config.spreadsheet_url = '1AbCdEfGhIjKlMnOpQrStUvWxYz'
        mgr._make_coordinator = lambda: GoogleSheetsCoordinator(self.oauth, '1AbCdEfGhIjKlMnOpQrStUvWxYz', http_helper=sim)  # type: ignore[method-assign]
        mgr.ensure_background_threads = lambda: None  # type: ignore[method-assign]

        mgr.refresh_sheet_tasks()
        # Verify whole-sheet read without !A1:ZZ truncation
        self.assertNotIn('A1:ZZ', sim.last_read_url)

        tokens = [
            json.dumps({'row': r.row_number, 'src': r.source_url, 'tgt': r.target_url})
            for r in mgr.cached_sheet_rows
        ]
        added_cnt, conflicts = mgr.claim_manual_selection(tokens)
        self.assertEqual(added_cnt, 3)
        self.assertEqual(conflicts, [])
        queued_ids = [t.task_id for t in mgr.queued_tasks]
        self.assertEqual(len(queued_ids), 3)

        # Selectively release only the middle queued task
        rel_msg = mgr.release_queued_tasks([queued_ids[1]])
        self.assertIn('1 条选中的排队任务退回表格', rel_msg)
        self.assertEqual([t.task_id for t in mgr.queued_tasks], [queued_ids[0], queued_ids[2]])

        _, _, snap = mgr._make_coordinator().fetch_tasks_snapshot()
        by_row = {r.row_number: r for r in snap}
        self.assertEqual(by_row[2].status, SHEET_STATUS_IN_PROGRESS)
        self.assertTrue(by_row[3].is_claimable)
        self.assertEqual(by_row[3].task_id, '')
        self.assertEqual(by_row[4].status, SHEET_STATUS_IN_PROGRESS)

        # Verify RAW write mode preserves leading '=' / '-' notes literally without formula error
        formula_like_note = '=SUM(A1:A5) -特殊开头报错文本'
        mgr._make_coordinator().fail_task(queued_ids[0], formula_like_note)
        self.assertEqual(sim.last_value_input_option, 'RAW')
        _, _, snap2 = mgr._make_coordinator().fetch_tasks_snapshot()
        row2 = [r for r in snap2 if r.row_number == 2][0]
        self.assertEqual(row2.note, formula_like_note)

        # Verify account unbind clears token and disables auto_claim
        mgr.auto_claim_enabled = True
        logout_msg = mgr.logout_google_account()
        self.assertIn('已解绑', logout_msg)
        self.assertFalse(self.token_path.exists())
        self.assertFalse(mgr.auto_claim_enabled)
        self.assertIn('未授权', mgr.get_auth_status_text())

    def test_headless_command_state_isolation_and_swap_error_guards(self) -> None:
        import collections
        import io
        from unittest.mock import patch
        from facefusion import presets, state_manager

        old_mode = state_manager.get_item('face_selector_mode')
        old_providers = state_manager.get_item('execution_providers')
        old_fmt = state_manager.get_item('temp_frame_format')
        try:
            # Simulate Manual UI Tab polluted to CPU + reference mode + PNG temp frames
            state_manager.init_item('face_selector_mode', 'reference')
            state_manager.init_item('execution_providers', ['cpu'])
            state_manager.init_item('temp_frame_format', 'png')

            cfg_ini = self.tmp_path / 'isolated.ini'
            cmd = sheet_worker.build_headless_command(
                python_executable='python.exe',
                facefusion_script=Path('facefusion.py'),
                source_image=Path('src.jpg'),
                target_video=Path('tgt.mp4'),
                output_video=Path('out.mp4'),
                temp_dir=Path('tmp'),
                jobs_dir=Path('jobs'),
                preset_values=presets.BUILTINS['clear']['values'],
                config_path=cfg_ini
            )
            self.assertIn('--config-path', cmd)
            mode_idx = cmd.index('--face-selector-mode')
            self.assertEqual(cmd[mode_idx + 1], 'one')
            fmt_idx = cmd.index('--temp-frame-format')
            self.assertEqual(cmd[fmt_idx + 1], 'jpeg')
            order_idx = cmd.index('--face-selector-order')
            self.assertEqual(cmd[order_idx + 1], 'large-small')
        finally:
            state_manager.init_item('face_selector_mode', old_mode)
            state_manager.init_item('execution_providers', old_providers)
            state_manager.init_item('temp_frame_format', old_fmt)

        # Verify stage parsing monotonicity and swapped_frames_total detection
        dummy_task = sheet_worker.LocalTask(
            task_id='FF-1114-030303-TEST',
            initial_row_number=2,
            source_url='https://ex.com/s.jpg',
            target_url='https://ex.com/v.mp4',
            preset_name='没问题选这个',
            claim_mode='手动领取'
        )
        recent = collections.deque(maxlen=20)
        st: Dict[str, Any] = {'saw_analysing': False, 'saw_extracting': False, 'saw_processing': False, 'saw_merging': False, 'swapped_frames': None}
        sheet_worker.SheetWorkerManager._parse_headless_line(dummy_task, 'analysing: 100%|====| 30/30 [00:01<00:00, 30frame/s]', recent, st)
        p_analyse = dummy_task.progress_pct
        sheet_worker.SheetWorkerManager._parse_headless_line(dummy_task, 'extracting: 100%|====| 30/30 [00:01<00:00, 30frame/s]', recent, st)
        p_extract = dummy_task.progress_pct
        sheet_worker.SheetWorkerManager._parse_headless_line(dummy_task, 'processing: 50%|====| 15/30 [00:02<00:02, 7.5frame/s]', recent, st)
        p_proc = dummy_task.progress_pct
        sheet_worker.SheetWorkerManager._parse_headless_line(dummy_task, '[FACEFUSION.FACE_SWAPPER] swapped_frames_total=0', recent, st)
        sheet_worker.SheetWorkerManager._parse_headless_line(dummy_task, 'merging: 100%|====| 30/30 [00:01<00:00, 30frame/s]', recent, st)
        p_merge = dummy_task.progress_pct
        self.assertLess(p_analyse, p_extract)
        self.assertLess(p_extract, p_proc)
        self.assertLess(p_proc, p_merge)
        self.assertEqual(st['swapped_frames'], 0)

        mgr = sheet_worker.SheetWorkerManager(
            config_path=self.tmp_path / 'swap_guard_cfg.json',
            token_path=self.token_path
        )
        temp_d = self.tmp_path / 'ff_t'
        jobs_d = self.tmp_path / 'ff_j'
        temp_d.mkdir(parents=True, exist_ok=True)
        jobs_d.mkdir(parents=True, exist_ok=True)
        out_mp4 = self.tmp_path / 'guard_out.mp4'

        # Case A: 0 swapped frames must be rejected even if exit code is 0
        class FakePopenZeroSwap:
            def __init__(self, *_a: Any, **_kw: Any) -> None:
                out_mp4.write_bytes(b'unswapped-video')
                self.stdout = io.BytesIO(b'[FACEFUSION] swapped_frames_total=0\n')

            def __enter__(self) -> 'FakePopenZeroSwap':
                return self

            def __exit__(self, *_exc: Any) -> None:
                return None

            def wait(self, timeout: Optional[float] = None) -> int:
                return 0

        with patch('subprocess.Popen', side_effect=FakePopenZeroSwap):
            with self.assertRaisesRegex(ValueError, '0 帧生效'):
                mgr._execute_headless_swap(
                    dummy_task, Path('s.jpg'), Path('t.mp4'), out_mp4,
                    temp_d, jobs_d, presets.BUILTINS['clear']['values']
                )
        self.assertFalse(out_mp4.exists())

        # Case B: no source face detected fails immediately without wasting a second libx264 run
        popen_calls = {'count': 0}

        class FakePopenNoSourceFace:
            def __init__(self, *_a: Any, **_kw: Any) -> None:
                popen_calls['count'] += 1
                self.stdout = io.BytesIO(b'[FACEFUSION.FACE_SWAPPER] No source face detected!\n')

            def __enter__(self) -> 'FakePopenNoSourceFace':
                return self

            def __exit__(self, *_exc: Any) -> None:
                return None

            def wait(self, timeout: Optional[float] = None) -> int:
                return 2

        with patch('subprocess.Popen', side_effect=FakePopenNoSourceFace):
            with self.assertRaisesRegex(ValueError, '源人脸图片中未检测到清晰正脸'):
                mgr._execute_headless_swap(
                    dummy_task, Path('s.jpg'), Path('t.mp4'), out_mp4,
                    temp_d, jobs_d, presets.BUILTINS['clear']['values']
                )
        self.assertEqual(popen_calls['count'], 1)

    def test_no_computer_name_probing_mandatory_worker_name_and_userdata_isolation(self) -> None:
        from facefusion import google_sheets_client, presets, userdata_paths

        # 1. WorkerConfig defaults to '' and sheet_worker never imports platform
        self.assertFalse(hasattr(sheet_worker, 'platform'))
        fresh_cfg = sheet_worker.WorkerConfig()
        self.assertEqual(fresh_cfg.worker_name, '')

        mgr = sheet_worker.SheetWorkerManager(
            config_path=self.tmp_path / 'empty_worker.json',
            token_path=self.token_path
        )
        mgr.ensure_background_threads = lambda: None  # type: ignore[method-assign]
        mgr.config.spreadsheet_url = '1AbCdEfGhIjKlMnOpQrStUvWxYz'
        self.assertEqual(mgr.config.worker_name, '')

        # 2. Cannot claim manually, enable auto-claim, or claim via coordinator with empty worker_name
        with self.assertRaisesRegex(ValueError, '本机节点名称'):
            mgr.claim_manual_selection([json.dumps({'row': 2, 'src': 'https://ex.com/a.jpg', 'tgt': 'https://ex.com/a.mp4'})])

        with self.assertRaisesRegex(ValueError, '本机节点名称'):
            mgr.set_auto_claim(True)
        self.assertFalse(mgr.auto_claim_enabled)

        sim = InMemorySheetsHttp([['源人脸图片', '目标视频'], ['https://ex.com/a.jpg', 'https://ex.com/a.mp4']])
        coord = GoogleSheetsCoordinator(self.oauth, '1AbCdEfGhIjKlMnOpQrStUvWxYz', http_helper=sim)
        with self.assertRaisesRegex(ValueError, '本机节点名称'):
            coord.claim_specific_tasks([(2, 'https://ex.com/a.jpg', 'https://ex.com/a.mp4')], worker_name='   ', verify_delay_sec=0.0)

        # 3. Verify all persistent/session paths resolve strictly inside `<PackageRoot>/userdata`
        ud = userdata_paths.get_userdata_dir()
        self.assertEqual(ud.name.lower(), 'userdata')
        self.assertEqual(presets.PRESET_PATH.parent.resolve(), ud.resolve())
        self.assertEqual(google_sheets_client.DEFAULT_TOKEN_PATH.parent.resolve(), ud.resolve())
        self.assertEqual(sheet_worker.WORKER_CONFIG_PATH.parent.resolve(), ud.resolve())
        self.assertEqual(sheet_worker.CLIENT_SECRET_COPY_PATH.parent.resolve(), ud.resolve())
        self.assertEqual(self._orig_recent_dir.parent.resolve(), ud.resolve())
        self.assertEqual(sheet_worker.FAILED_OUTPUT_BACKUP_DIR.parent.resolve(), ud.resolve())
        self.assertEqual(self._orig_temp_root.parent.resolve(), ud.resolve())

    def test_20min_auto_claim_interval_lightweight_writeback_and_ui_skip_optimizations(self) -> None:
        from facefusion.google_sheets_client import SheetTaskRow
        from facefusion.uis.components import sheet_worker as sheet_worker_ui

        # 1. Verify auto-claim interval is 20 minutes (1200 seconds) across backend & UI
        self.assertEqual(sheet_worker.AUTO_CLAIM_INTERVAL_MINUTES, 20)
        self.assertEqual(sheet_worker.AUTO_CLAIM_INTERVAL_SEC, 20 * 60)

        mgr = sheet_worker.SheetWorkerManager(
            config_path=self.tmp_path / 'interval_cfg.json',
            token_path=self.token_path
        )
        mgr.ensure_background_threads = lambda: None  # type: ignore[method-assign]
        mgr.update_config('工位01', '1AbCdEfGhIjKlMnOpQrStUvWxYz', '口播需求表', '')
        auto_msg = mgr.set_auto_claim(True)
        self.assertIn('20 分钟', auto_msg)

        # 2. Verify complete_task / fail_task / release_queued_task only read Row 1 + task_id col (never whole sheet)
        rows = [
            ['源人脸图片', '目标视频', '任务编号', '任务状态', '领取节点', '成品链接', '处理备注'],
            ['https://ex.com/1.jpg', 'https://ex.com/1.mp4', 'FF-1115-080001-AAAA', SHEET_STATUS_IN_PROGRESS, '工位01', '', ''],
            ['https://ex.com/2.jpg', 'https://ex.com/2.mp4', 'FF-1115-080002-BBBB', SHEET_STATUS_IN_PROGRESS, '工位01', '', ''],
        ]
        sim = InMemorySheetsHttp(rows)
        coord = GoogleSheetsCoordinator(self.oauth, '1AbCdEfGhIjKlMnOpQrStUvWxYz', http_helper=sim)
        sim.read_ranges.clear()

        updated_r = coord.complete_task('FF-1115-080002-BBBB', 'https://drive.google.com/file/d/done2/view', 'ok')
        self.assertEqual(updated_r, 3)
        self.assertTrue(all('!' in rng for rng in sim.read_ranges), f'Expected sub-range reads only, got: {sim.read_ranges}')
        self.assertTrue(any(rng.endswith('!1:1') for rng in sim.read_ranges))
        self.assertTrue(any(rng.endswith('!C:C') for rng in sim.read_ranges))

        # 3. Verify UI >500 rows cap preserves active/claimable rows while bounding DOM size
        huge_rows = [
            SheetTaskRow(
                row_number=i,
                source_url=f'https://ex.com/{i}.jpg',
                target_url=f'https://ex.com/{i}.mp4',
                preset_name='',
                task_id=f'FF-0000-{i:06d}-DONE',
                status=SHEET_STATUS_COMPLETED,
                worker='工位01',
                output_url=f'https://drive.google.com/file/d/out{i}/view',
                note='done'
            )
            for i in range(2, 702)
        ]
        huge_rows.insert(0, SheetTaskRow(
            row_number=2,
            source_url='https://ex.com/pending.jpg',
            target_url='https://ex.com/pending.mp4',
            preset_name='',
            task_id='',
            status='',
            worker='',
            output_url='',
            note=''
        ))
        capped_table = sheet_worker_ui._build_all_rows_table(huge_rows)
        self.assertEqual(len(capped_table), sheet_worker_ui.MAX_UI_ALL_ROWS)
        self.assertEqual(capped_table[0][0], 2)
        self.assertEqual(capped_table[0][2], '待领取')

        # 4. Verify on_timer_tick returns pure gradio.skip() when idle and versions match
        with sheet_worker_ui.WORKER_MANAGER._lock:
            rev = sheet_worker_ui.WORKER_MANAGER.snapshot_revision
            ui_ver = sheet_worker_ui.WORKER_MANAGER.ui_state_version
            was_auto = sheet_worker_ui.WORKER_MANAGER.auto_claim_enabled
            sheet_worker_ui.WORKER_MANAGER.auto_claim_enabled = False
        auth_sig = sheet_worker_ui._auth_sync_signature()
        try:
            tick_out = sheet_worker_ui.on_timer_tick(rev, '', ui_ver, False, auth_sig, False)
            # First 10 visual outputs must all be gradio.skip() dicts ({'__type__': 'update'})
            for widget_val in tick_out[:10]:
                self.assertIsInstance(widget_val, dict)
        finally:
            with sheet_worker_ui.WORKER_MANAGER._lock:
                sheet_worker_ui.WORKER_MANAGER.auto_claim_enabled = was_auto

    def test_upload_connection_reuse_download_retry_watchdog_and_circuit_breaker(self) -> None:
        import time
        from unittest.mock import MagicMock, patch
        import httpx
        from facefusion import google_sheets_client, presets

        # 1. Resumable upload reuses a SINGLE httpx.Client across multiple chunks (reconnects only on error)
        multi_mp4 = self.tmp_path / 'multi_chunk.mp4'
        multi_mp4.write_bytes(b'x' * 3000)

        drive = GoogleDriveClient(self.oauth)
        drive.CHUNK_SIZE = 1000  # 3 chunks of 1000 bytes each
        drive._default_folder_cache = 'folder_123'

        def _fake_authed_req(method: str, url: str, **_kw: Any) -> Any:
            if 'upload/drive/v3/files' in url:
                resp = MagicMock()
                resp.status_code = 200
                resp.headers = {'Location': 'https://upload.example.com/session_abc'}
                resp.text = '{}'
                return resp
            if url.endswith('/permissions'):
                resp = MagicMock()
                resp.status_code = 200
                resp.text = '{}'
                return resp
            raise AssertionError(f'Unexpected URL: {url}')

        drive.http.request = _fake_authed_req  # type: ignore[method-assign]
        created_clients = {'count': 0, 'puts': 0}

        class FakeUploadClient:
            def __init__(self) -> None:
                created_clients['count'] += 1
                self.closed = False

            def put(self, _url: str, content: bytes, headers: Dict[str, str]) -> Any:
                created_clients['puts'] += 1
                resp = MagicMock()
                if headers['Content-Range'].endswith('2999/3000'):
                    resp.status_code = 200
                    resp.json.return_value = {'id': 'uploaded_vid_999', 'webViewLink': 'https://drive.google.com/file/d/uploaded_vid_999/view'}
                else:
                    resp.status_code = 308
                return resp

            def close(self) -> None:
                self.closed = True

        with patch.object(google_sheets_client, '_make_http_client', side_effect=lambda **_kw: FakeUploadClient()):
            link, warn = drive.upload_and_share_detailed(multi_mp4, 'out.mp4')
        self.assertEqual(created_clients['puts'], 3)
        self.assertEqual(created_clients['count'], 1, 'Must reuse 1 persistent HTTP client across all 3 chunks!')
        self.assertIn('uploaded_vid_999', link)
        self.assertEqual(warn, '')

        # 2. Download retries cleanly after transient proxy drop & rejects >4GB oversized file
        dl_attempts = {'count': 0}

        class FakeDownloadStream:
            def __init__(self, fail_midway: bool, oversize: bool = False) -> None:
                self.status_code = 200
                self.headers = {
                    'Content-Type': 'video/mp4',
                    'Content-Length': str(drive.MAX_MEDIA_DOWNLOAD_BYTES + 10) if oversize else '12'
                }
                self._fail = fail_midway

            def __enter__(self) -> 'FakeDownloadStream':
                return self

            def __exit__(self, *_a: Any) -> None:
                return None

            def iter_bytes(self, chunk_size: int = 1024) -> Any:
                yield b'hello_'
                if self._fail:
                    raise httpx.ReadError('proxy connection reset')
                yield b'world!'

        class FakeDownloadClient:
            def __init__(self, oversize: bool = False) -> None:
                self._oversize = oversize

            def __enter__(self) -> 'FakeDownloadClient':
                return self

            def __exit__(self, *_a: Any) -> None:
                return None

            def stream(self, *_a: Any, **_kw: Any) -> FakeDownloadStream:
                dl_attempts['count'] += 1
                return FakeDownloadStream(fail_midway=(dl_attempts['count'] == 1), oversize=self._oversize)

        with patch.object(google_sheets_client, '_make_http_client', side_effect=lambda **_kw: FakeDownloadClient()), patch('time.sleep'):
            dl_path, _ = drive._download_from_http('https://cdn.example.com/clip.mp4', self.tmp_path / 'dl_test', 'raw_target', '.mp4', None)
        self.assertEqual(dl_attempts['count'], 2)
        self.assertEqual(dl_path.read_bytes(), b'hello_world!')
        self.assertEqual(list((self.tmp_path / 'dl_test').glob('*.part')), [])

        with patch.object(google_sheets_client, '_make_http_client', side_effect=lambda **_kw: FakeDownloadClient(oversize=True)):
            with self.assertRaisesRegex(ValueError, '体积过大'):
                drive._download_from_http('https://cdn.example.com/huge.mp4', self.tmp_path / 'dl_huge', 'raw_target', '.mp4', None)

        # 3. Hung headless-run subprocess triggers watchdog timeout and terminates process tree
        mgr = sheet_worker.SheetWorkerManager(
            config_path=self.tmp_path / 'wd_cfg.json',
            token_path=self.token_path
        )
        mgr.ensure_background_threads = lambda: None  # type: ignore[method-assign]
        killed_pids: List[int] = []

        class HungStdout:
            def __init__(self) -> None:
                self.unblock = False

            def read(self, _n: int = 256) -> bytes:
                while not self.unblock:
                    time.sleep(0.05)
                return b''

        class FakeHungPopen:
            def __init__(self, *_a: Any, **_kw: Any) -> None:
                self.pid = 999999
                self.stdout = HungStdout()
                self._exited = False

            def poll(self) -> Optional[int]:
                return 1 if self._exited else None

            def kill(self) -> None:
                self._exited = True
                self.stdout.unblock = True

            def wait(self, timeout: Optional[float] = None) -> int:
                return 1

            def __enter__(self) -> 'FakeHungPopen':
                return self

            def __exit__(self, *_a: Any) -> None:
                return None

        def _fake_tree_kill(proc: Any) -> None:
            if proc is not None and getattr(proc, 'pid', None):
                killed_pids.append(proc.pid)
                proc.kill()

        hung_task = sheet_worker.LocalTask(
            task_id='FF-1115-090909-HUNG',
            initial_row_number=2,
            source_url='https://ex.com/s.jpg',
            target_url='https://ex.com/v.mp4',
            preset_name='没问题选这个',
            claim_mode='自动接单'
        )
        t_dir = self.tmp_path / 'wd_t'
        j_dir = self.tmp_path / 'wd_j'
        t_dir.mkdir(parents=True, exist_ok=True)
        j_dir.mkdir(parents=True, exist_ok=True)

        with (
            patch.object(sheet_worker, 'SWAP_IDLE_TIMEOUT_SEC', 0.2),
            patch.object(sheet_worker, 'terminate_process_tree', side_effect=_fake_tree_kill),
            patch('subprocess.Popen', side_effect=FakeHungPopen)
        ):
            with self.assertRaisesRegex(ValueError, '超时无响应'):
                mgr._execute_headless_swap(
                    hung_task, Path('s.jpg'), Path('t.mp4'), self.tmp_path / 'wd_out.mp4',
                    t_dir, j_dir, presets.BUILTINS['clear']['values']
                )
        self.assertIn(999999, killed_pids)

        # 4. Circuit-breaker on Drive storageQuotaExceeded: saves Task #1 backup, disables auto_claim,
        #    and safely returns queued Tasks #2 & #3 back to Google Sheets (待处理) without wasting GPU!
        cb_rows = [
            ['源人脸图片', '目标视频', '任务编号', '任务状态', '领取节点', '成品链接', '处理备注'],
            ['https://ex.com/1.jpg', 'https://ex.com/1.mp4', 'FF-1115-100001-Q001', SHEET_STATUS_IN_PROGRESS, '节点X', '', ''],
            ['https://ex.com/2.jpg', 'https://ex.com/2.mp4', 'FF-1115-100002-Q002', SHEET_STATUS_IN_PROGRESS, '节点X', '', ''],
            ['https://ex.com/3.jpg', 'https://ex.com/3.mp4', 'FF-1115-100003-Q003', SHEET_STATUS_IN_PROGRESS, '节点X', '', ''],
        ]
        cb_sim = InMemorySheetsHttp(cb_rows)
        mgr.config.worker_name = '节点X'
        mgr.config.spreadsheet_url = '1AbCdEfGhIjKlMnOpQrStUvWxYz'
        mgr._make_coordinator = lambda: GoogleSheetsCoordinator(self.oauth, '1AbCdEfGhIjKlMnOpQrStUvWxYz', http_helper=cb_sim)  # type: ignore[method-assign]
        mgr.auto_claim_enabled = True

        t1 = sheet_worker.LocalTask('FF-1115-100001-Q001', 2, 'https://ex.com/1.jpg', 'https://ex.com/1.mp4', '没问题选这个', '手动领取')
        t2 = sheet_worker.LocalTask('FF-1115-100002-Q002', 3, 'https://ex.com/2.jpg', 'https://ex.com/2.mp4', '没问题选这个', '手动领取')
        t3 = sheet_worker.LocalTask('FF-1115-100003-Q003', 4, 'https://ex.com/3.jpg', 'https://ex.com/3.mp4', '没问题选这个', '手动领取')
        mgr.queued_tasks.extend([t2, t3])

        from PIL import Image
        face_jpg = self.tmp_path / 'cb_face.jpg'
        Image.new('RGB', (128, 128), (180, 140, 120)).save(face_jpg)
        ffmpeg_bin = media_normalizer._resolve_tool_binary('ffmpeg')
        cb_vid = self.tmp_path / 'cb_vid.mp4'
        subprocess.run([
            ffmpeg_bin, '-y', '-hide_banner', '-loglevel', 'error',
            '-f', 'lavfi', '-i', 'testsrc=size=320x240:rate=30:duration=1',
            '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(cb_vid)
        ], check=True)

        orig_backup_dir = sheet_worker.FAILED_OUTPUT_BACKUP_DIR
        sheet_worker.FAILED_OUTPUT_BACKUP_DIR = self.tmp_path / 'cb_backups'
        try:
            with (
                patch.object(GoogleDriveClient, 'download_media_with_name', side_effect=lambda _u, _d, _s, ext, progress_cb=None: (face_jpg, 'f.jpg') if ext == '.jpg' else (cb_vid, 'v1.mp4')),
                patch.object(mgr, '_execute_headless_swap', side_effect=lambda task, source_image, target_video, output_video, **_kw: output_video.write_bytes(b'rendered-mp4')),
                patch.object(GoogleDriveClient, 'upload_and_share_detailed', side_effect=ValueError('当前 Google Drive 云端硬盘空间已满（storageQuotaExceeded）'))
            ):
                mgr._run_single_task(t1)

            self.assertFalse(mgr.auto_claim_enabled, 'Auto-claim must automatically turn off on storageQuotaExceeded!')
            self.assertEqual(len(mgr.queued_tasks), 0, 'Queued tasks must be drained & returned to sheet!')
            self.assertEqual(len(mgr.saved_backup_files()), 1)

            _, _, after_cb = mgr._make_coordinator().fetch_tasks_snapshot()
            by_r = {r.row_number: r for r in after_cb}
            self.assertEqual(by_r[2].status, SHEET_STATUS_FAILED)
            self.assertTrue(by_r[3].is_claimable, 'Unstarted queued row 3 must be returned as claimable!')
            self.assertTrue(by_r[4].is_claimable, 'Unstarted queued row 4 must be returned as claimable!')

            # 5. Low free-disk preflight guard returns task to 待处理 BEFORE downloading or running GPU
            pre_task = sheet_worker.LocalTask('FF-1115-100004-DISK', 3, 'https://ex.com/2.jpg', 'https://ex.com/2.mp4', '没问题选这个', '自动接单')
            cb_sim.rows[2][2] = 'FF-1115-100004-DISK'
            cb_sim.rows[2][3] = SHEET_STATUS_IN_PROGRESS
            cb_sim.rows[2][4] = '节点X'
            mgr.auto_claim_enabled = True
            with patch.object(sheet_worker, 'MIN_FREE_DISK_BYTES', 10 ** 18):
                mgr._run_single_task(pre_task)
            self.assertEqual(pre_task.local_status, sheet_worker.LOCAL_STATUS_RETURNED)
            self.assertFalse(mgr.auto_claim_enabled)
            _, _, after_disk = mgr._make_coordinator().fetch_tasks_snapshot()
            self.assertTrue([r for r in after_disk if r.row_number == 3][0].is_claimable)

            # 6. Runtime sweep cleans orphan sheet_temp folders while preserving active task & cleans stale Gradio temp
            active_tmp = sheet_worker.SHEET_TEMP_ROOT / 'FF-ACTIVE-TASK'
            orphan_tmp = sheet_worker.SHEET_TEMP_ROOT / 'FF-ORPHAN-STALE'
            active_tmp.mkdir(parents=True, exist_ok=True)
            orphan_tmp.mkdir(parents=True, exist_ok=True)
            (orphan_tmp / 'frame001.jpg').write_bytes(b'stale')
            fake_gradio_dir = self.tmp_path / 'gradio_tmp'
            fake_gradio_dir.mkdir(parents=True, exist_ok=True)
            old_file = fake_gradio_dir / 'expired.json'
            old_file.write_text('{}', encoding='utf-8')
            import os
            old_ts = time.time() - (sheet_worker.GRADIO_TEMP_TTL_SEC + 600)
            os.utime(old_file, (old_ts, old_ts))

            mgr.current_task = sheet_worker.LocalTask('FF-ACTIVE-TASK', 2, 'https://ex.com/1.jpg', 'https://ex.com/1.mp4', '没问题选这个', '手动领取')
            with patch.object(sheet_worker, 'GRADIO_TEMP_DIR', fake_gradio_dir):
                mgr.sweep_runtime_temp_dirs(force=True)
            mgr.current_task = None
            self.assertTrue(active_tmp.exists())
            self.assertFalse(orphan_tmp.exists())
            self.assertFalse(old_file.exists())
        finally:
            sheet_worker.FAILED_OUTPUT_BACKUP_DIR = orig_backup_dir

    def test_oauth_preconnect_deadlock_immunity_and_manual_url_fallback(self) -> None:
        import socket
        import time
        import urllib.parse
        import urllib.request
        from unittest.mock import patch
        from facefusion import google_sheets_client as gsc

        secret_json = self.tmp_path / 'desktop_client.json'
        secret_json.write_text(json.dumps({
            'installed': {
                'client_id': 'test-client-id.apps.googleusercontent.com',
                'client_secret': 'test-client-secret',
                'auth_uri': 'https://accounts.google.com/o/oauth2/v2/auth',
                'token_uri': 'https://oauth2.googleapis.com/token',
                'redirect_uris': ['http://localhost']
            }
        }), encoding='utf-8')

        class _MockResp:
            def __init__(self, status_code: int, body: Dict[str, Any]) -> None:
                self.status_code = status_code
                self._body = body
                self.text = json.dumps(body)

            def json(self) -> Dict[str, Any]:
                return self._body

        class _MockHttpClient:
            def __enter__(self) -> '_MockHttpClient':
                return self

            def __exit__(self, *args: Any) -> None:
                return None

            def post(self, url: str, data: Optional[Dict[str, Any]] = None) -> _MockResp:
                return _MockResp(200, {
                    'access_token': 'acc-tok-new',
                    'refresh_token': 'ref-tok-new',
                    'expires_in': 3600
                })

            def get(self, url: str, params: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None) -> _MockResp:
                return _MockResp(200, {
                    'user': {'displayName': 'Operator', 'emailAddress': 'tester@example.com'}
                })

        test_token = self.tmp_path / 'preconnect_token.json'
        oauth = GoogleOAuthManager(token_path=test_token)

        with patch.object(gsc, '_make_http_client', return_value=_MockHttpClient()):
            t0 = time.monotonic()
            session = oauth.start_auth_session(secret_json, timeout_sec=10)
            parsed_redir = urllib.parse.urlparse(session.redirect_uri)
            port = parsed_redir.port
            assert port is not None

            # Socket #1: Chromium-style idle speculative preconnect socket (sends 0 bytes!)
            idle_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            idle_sock.connect(('127.0.0.1', port))
            try:
                # Socket #2: Real OAuth callback must succeed immediately without waiting on Socket #1!
                cb_url = f'{session.redirect_uri}/?state={urllib.parse.quote(session.expected_state)}&code=valid_oauth_code_123456'
                with urllib.request.urlopen(cb_url, timeout=3.0) as resp:
                    html_body = resp.read().decode('utf-8')
                self.assertIn('授权成功', html_body)
                self.assertTrue(session.done_event.wait(timeout=2.0))
                self.assertLess(time.monotonic() - t0, 2.5, 'Preconnect socket must NOT block OAuth callback!')
                self.assertTrue(oauth.has_valid_refresh_token())
                self.assertIn('tester@example.com', oauth.read_token_data().get('authorized_email', ''))
            finally:
                idle_sock.close()
                session.cancel()

            # Test non-blocking SheetWorkerManager session + manual callback URL paste + auto-connect!
            manual_token = self.tmp_path / 'manual_token.json'
            mgr = sheet_worker.SheetWorkerManager(
                config_path=self.tmp_path / 'manual_worker.json',
                token_path=manual_token
            )
            sim = InMemorySheetsHttp([
                ['源人脸图片', '目标视频'],
                ['https://ex.com/s.jpg', 'https://ex.com/v.mp4'],
            ])
            mgr.update_config('Worker-Test', '1AbCdEfGhIjKlMnOpQrStUvWxYz', '', '', uploaded_secret_file=str(secret_json))
            mgr._make_coordinator = lambda: GoogleSheetsCoordinator(mgr.oauth, '1AbCdEfGhIjKlMnOpQrStUvWxYz', http_helper=sim)  # type: ignore[method-assign]
            mgr.ensure_background_threads = lambda: None  # type: ignore[method-assign]

            start_t = time.monotonic()
            auth_url, _ = mgr.start_google_auth_session(open_browser=False)
            self.assertLess(time.monotonic() - start_t, 0.4, 'UI OAuth session starter must be non-blocking!')
            self.assertTrue(mgr.auth_in_progress)
            self.assertIn('accounts.google.com', auth_url)

            assert mgr._pending_oauth_session is not None
            pasted_url = (
                f'{mgr._pending_oauth_session.redirect_uri}/'
                f'?state={urllib.parse.quote(mgr._pending_oauth_session.expected_state)}'
                f'&code=manual_pasted_code_987654321&scope=spreadsheets'
            )
            done_msg = mgr.complete_google_auth_manual(pasted_url)
            self.assertFalse(mgr.auth_in_progress)
            self.assertTrue(mgr.oauth.has_valid_refresh_token())
            self.assertIn('已自动连接表格', done_msg)
            self.assertEqual(len(mgr.cached_sheet_rows), 1)

    def test_ui_handlers_never_raise_gradio_error_and_match_output_counts(self) -> None:
        from unittest.mock import patch
        from facefusion.uis.components import sheet_worker as sw_ui

        secret_json = self.tmp_path / 'ui_client.json'
        secret_json.write_text(json.dumps({
            'installed': {
                'client_id': 'ui-client-id.apps.googleusercontent.com',
                'client_secret': 'ui-client-secret',
                'redirect_uris': ['http://localhost']
            }
        }), encoding='utf-8')

        isolated_mgr = sheet_worker.SheetWorkerManager(
            config_path=self.tmp_path / 'ui_isolated_cfg.json',
            token_path=self.tmp_path / 'ui_isolated_tok.json'
        )
        isolated_mgr.ensure_background_threads = lambda: None  # type: ignore[method-assign]

        try:
            with (
                patch.object(sw_ui, 'WORKER_MANAGER', isolated_mgr),
                patch.object(sheet_worker.webbrowser, 'open', return_value=True)
            ):
                # 1. Authorize without client_secret.json -> must NOT raise gradio.Error
                r_auth_missing = sw_ui.on_authorize('工位1', '', None, '', None)
                self.assertEqual(len(r_auth_missing), 8)

                # 2. Upload client_secret.json -> instant validation, 7 outputs
                r_upload = sw_ui.on_upload_secret_file(str(secret_json))
                self.assertEqual(len(r_upload), 7)
                self.assertIn('已就绪客户端文件', r_upload[0])

                # 3. Save & connect with empty worker_name -> must NOT raise gradio.Error (25 outputs)
                r_save_no_worker = sw_ui.on_save_and_connect('', 'https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz/edit', None, '', None, False)
                self.assertEqual(len(r_save_no_worker), 25)

                # 4. Save & connect with valid worker + URL while not authorized -> auto-starts OAuth without raising!
                r_save_bridge = sw_ui.on_save_and_connect('工位1', 'https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz/edit', None, '', None, False)
                self.assertEqual(len(r_save_bridge), 25)
                self.assertTrue(isolated_mgr.auth_in_progress)

                # 5. Refresh, claim, manual submit with invalid input, timer tick -> zero exceptions!
                r_ref = sw_ui.on_refresh_sheet(False)
                self.assertEqual(len(r_ref), 19)
                r_claim = sw_ui.on_claim_selected([], False)
                self.assertEqual(len(r_claim), 19)
                r_manual_bad = sw_ui.on_manual_auth_submit('short', '工位1', '1AbCdEfGhIjKlMnOpQrStUvWxYz', None, '', False)
                self.assertEqual(len(r_manual_bad), 25)
                r_tick = sw_ui.on_timer_tick(-1, '', -1, False, '', False)
                self.assertEqual(len(r_tick), 26)
        finally:
            if isolated_mgr._pending_oauth_session is not None:
                isolated_mgr._pending_oauth_session.cancel()

    def test_zero_china_mirrors_and_model_bootstrap_failover_resume_and_crc32(self) -> None:
        import os
        from unittest.mock import patch
        import httpx
        import facefusion.choices
        from facefusion import core as ff_core
        from facefusion import download as ff_download
        from facefusion import filesystem as ff_fs
        from facefusion import hash_helper, model_bootstrap

        # 1. Strict official global CDN verification (no third-party mirrors or .cn URLs)
        all_provider_urls = [
            u
            for spec in facefusion.choices.download_provider_set.values()
            for u in spec.get('urls', [])
        ]
        self.assertEqual(set(all_provider_urls), {'https://github.com', 'https://huggingface.co'})
        for u in all_provider_urls:
            self.assertNotIn('mirror', u.lower())
            self.assertFalse(u.lower().endswith('.cn'))

        # 2. Upstream content_analyser integrity hash ('3c6ce25e') and pre_check (no external curl needed)
        self.assertTrue(ff_core.common_pre_check())

        # 3. Isolated FACEFUSION_MODELS_DIR seeds .hash manifests without touching bundled models
        isolated_models_dir = self.tmp_path / 'isolated_models'
        with patch.dict(os.environ, {'FACEFUSION_MODELS_DIR': str(isolated_models_dir)}):
            resolved_onnx = Path(ff_fs.resolve_relative_path('../.assets/models/yoloface_8n.onnx'))
            resolved_hash = Path(ff_fs.resolve_relative_path('../.assets/models/yoloface_8n.hash'))
            self.assertEqual(resolved_onnx.parent, isolated_models_dir.resolve())
            self.assertTrue(resolved_hash.is_file(), 'Pinned .hash must be auto-seeded into isolated models dir')
            self.assertEqual(resolved_hash.stat().st_size, 8)

            missing = model_bootstrap.find_missing_preset_models(verify_crc=False)
            self.assertEqual(len(missing), 19)
            self.assertEqual(model_bootstrap.main(['--check-only']), 10)

            # 4. Verify candidate failover + HTTP 206 Range resume + CRC32 validation
            payload = b'OFFICIAL_ONNX_BINARY_PAYLOAD_' * 64
            expected_crc = hash_helper.create_hash(payload)
            demo_dir = isolated_models_dir
            demo_onnx = demo_dir / 'demo_test.onnx'
            demo_hash = demo_dir / 'demo_test.hash'
            demo_part = demo_dir / 'demo_test.onnx.part'
            demo_hash.write_text(expected_crc, encoding='ascii')
            # Pre-seed first 40 bytes in .part to verify Range: bytes=40- resume!
            demo_part.write_bytes(payload[:40])

            seen_requests: list[tuple[str, dict[str, str]]] = []

            def _mock_handler(request: httpx.Request) -> httpx.Response:
                seen_requests.append((str(request.url), dict(request.headers)))
                if 'huggingface.co' in request.url.host:
                    return httpx.Response(503, content=b'Service Unavailable')
                rng = request.headers.get('range', '')
                if rng == 'bytes=40-':
                    rem = payload[40:]
                    return httpx.Response(
                        206,
                        headers={
                            'Content-Range': f'bytes 40-{len(payload) - 1}/{len(payload)}',
                            'Content-Length': str(len(rem)),
                        },
                        content=rem,
                    )
                return httpx.Response(
                    200,
                    headers={'Content-Length': str(len(payload))},
                    content=payload,
                )

            mock_transport = httpx.MockTransport(_mock_handler)
            primary_hf_url = 'https://huggingface.co/facefusion/models-3.0.0/resolve/main/demo_test.onnx'
            cands = ff_download._candidate_download_urls(primary_hf_url)
            self.assertEqual(
                cands,
                [
                    'https://huggingface.co/facefusion/models-3.0.0/resolve/main/demo_test.onnx',
                    'https://github.com/facefusion/facefusion-assets/releases/download/models-3.0.0/demo_test.onnx',
                ],
            )

            with patch.object(
                ff_download,
                '_make_download_client',
                side_effect=lambda timeout: httpx.Client(transport=mock_transport, timeout=timeout),
            ):
                ok = ff_download._stream_download_with_failover(primary_hf_url, str(demo_onnx))
                self.assertTrue(ok)
                self.assertFalse(demo_part.exists())
                self.assertTrue(demo_onnx.is_file())
                self.assertEqual(demo_onnx.read_bytes(), payload)
                self.assertTrue(hash_helper.validate_hash(str(demo_onnx)))
                # First hit huggingface (503), then failed over to github with Range: bytes=40-!
                self.assertGreaterEqual(len(seen_requests), 2)
                self.assertIn('huggingface.co', seen_requests[0][0])
                self.assertIn('github.com', seen_requests[1][0])
                self.assertEqual(seen_requests[1][1].get('range'), 'bytes=40-')

    def test_portable_packager_excludes_onnx_and_userdata_while_keeping_hashes(self) -> None:
        import importlib.util

        repo_root = Path(__file__).resolve().parents[3]
        script_path = repo_root / 'create-portable-zip.py'
        spec = importlib.util.spec_from_file_location('create_portable_zip_mod', script_path)
        assert spec is not None and spec.loader is not None
        packager = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(packager)

        code_only_files = packager.collect_shareable_files(include_runtime_and_tools=False)
        code_rels = [p.relative_to(repo_root).as_posix() for p in code_only_files]

        self.assertTrue(all(not r.startswith('userdata/') for r in code_rels))
        self.assertTrue(all(not r.startswith('runtime/') for r in code_rels))
        self.assertTrue(all(not r.startswith('tools/') for r in code_rels))
        self.assertTrue(all(not r.endswith('.onnx') for r in code_rels))
        self.assertTrue(all('__pycache__' not in r and not r.endswith('.pyc') for r in code_rels))

        hash_rels = [r for r in code_rels if r.startswith('app/facefusion/.assets/models/') and r.endswith('.hash')]
        self.assertGreaterEqual(len(hash_rels), 19, 'All pinned .hash files must be included in packages!')

        full_files = packager.collect_shareable_files(include_runtime_and_tools=True)
        full_rels = [p.relative_to(repo_root).as_posix() for p in full_files]
        self.assertTrue(all(not r.startswith('userdata/') for r in full_rels))
        self.assertTrue(all(not r.endswith('.onnx') for r in full_rels))
        self.assertTrue(all(not r.lower().endswith('/ffplay.exe') for r in full_rels))
        self.assertIn('runtime/python/python.exe', full_rels)
        self.assertTrue(any(r.lower().endswith('/ffmpeg.exe') for r in full_rels))


if __name__ == '__main__':
    unittest.main()
