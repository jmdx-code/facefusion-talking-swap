import threading
import time
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from facefusion import sheet_worker as worker
from facefusion.google_sheets_client import SheetTaskRow


class AutoClaimSequenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        root=Path(self.temp.name)
        self.manager=worker.SheetWorkerManager(config_path=root/'config.json',token_path=root/'token.json')
        self.manager.ensure_background_threads=Mock()
        self.manager.oauth.has_valid_refresh_token=Mock(return_value=True)
        self.manager.config.worker_name='test-worker'
        self.manager.config.spreadsheet_url='test-sheet'
        self.manager.refresh_sheet_tasks=Mock()
        self.manager.sweep_runtime_temp_dirs=Mock()
        self.coordinator=Mock()
        self.coordinator.claim_next_available.return_value=([],[])
        self.manager._make_coordinator=Mock(return_value=self.coordinator)

    def tearDown(self):
        self.temp.cleanup()

    def row(self,number):
        return SheetTaskRow(number+1,'https://example.com/source.png',f'https://example.com/{number}.mp4','',f'FF-1117-010101-{number:04d}','已领取制作中','test-worker','','')

    def finish(self,abort=False):
        task=self.manager.queued_tasks.popleft()
        self.manager.current_task=task
        task.abort_requested=abort
        task.local_status=worker.LOCAL_STATUS_COMPLETED
        self.manager._finish_local_task(task)

    def test_enable_checks_now_then_claims_only_one_and_parks_until_complete(self):
        with patch.object(worker.time,'time',return_value=1000):
            self.manager.set_auto_claim(True)
            self.assertEqual(self.manager.next_auto_poll_ts,1000)
            self.coordinator.claim_next_available.return_value=([self.row(1)],[])
            self.manager.trigger_auto_claim_once(automatic=True)
            self.coordinator.claim_next_available.assert_called_once_with(max_count=1,worker_name='test-worker')
            self.assertEqual(self.manager.active_task_count(),1)
            self.assertEqual(self.manager.next_auto_poll_ts,0)
            self.manager.trigger_auto_claim_once(automatic=True)
            self.coordinator.claim_next_available.assert_called_once()

    def test_finished_task_immediately_requests_next_but_empty_response_waits_twenty_minutes(self):
        self.coordinator.claim_next_available.side_effect=[([self.row(1)],[]),([self.row(2)],[]),([],[])]
        with patch.object(worker.time,'time',return_value=1000):
            self.manager.set_auto_claim(True)
            for _ in range(2):
                self.manager.trigger_auto_claim_once(automatic=True)
                self.assertEqual(self.manager.active_task_count(),1)
                self.finish()
                self.assertEqual(self.manager.next_auto_poll_ts,1000)
            self.manager.trigger_auto_claim_once(automatic=True)
            self.assertEqual(self.manager.active_task_count(),0)
            self.assertEqual(self.manager.next_auto_poll_ts,2200)
            self.assertEqual(self.coordinator.claim_next_available.call_count,3)

    def test_timer_waits_until_idle_deadline(self):
        self.manager.auto_claim_enabled=True
        for now,expected in [(2199,0),(2200,1)]:
            self.manager.next_auto_poll_ts=2200
            with patch.object(worker.time,'time',return_value=now),patch.object(worker.time,'sleep',side_effect=[None,InterruptedError('end test tick')]),patch.object(self.manager,'trigger_auto_claim_once') as poll:
                with self.assertRaises(InterruptedError):self.manager._auto_claim_loop()
                self.assertEqual(poll.call_count,expected)
                if expected:poll.assert_called_once_with(automatic=True)

    def test_enabling_while_existing_queue_does_not_prefetch(self):
        self.manager._enqueue_claimed_rows([self.row(1),self.row(2)],'手动领取')
        with patch.object(worker.time,'time',return_value=1000):
            self.manager.set_auto_claim(True)
            self.assertEqual(self.manager.next_auto_poll_ts,0)
            self.finish()
            self.assertEqual(self.manager.next_auto_poll_ts,0)
            self.finish()
            self.assertEqual(self.manager.next_auto_poll_ts,1000)
        self.coordinator.claim_next_available.assert_not_called()

    def test_disable_or_circuit_breaker_prevents_follow_up(self):
        for circuit in [False,True]:
            self.manager._enqueue_claimed_rows([self.row(1)],'自动接单')
            self.manager.auto_claim_enabled=True
            task=self.manager.queued_tasks.popleft();self.manager.current_task=task
            if circuit:self.manager._trip_circuit_breaker('test fault')
            else:self.manager.set_auto_claim(False)
            self.manager._finish_local_task(task)
            self.assertEqual(self.manager.next_auto_poll_ts,0)
            self.manager.trigger_auto_claim_once(automatic=True)
        self.coordinator.claim_next_available.assert_not_called()

    def test_cancelled_or_returned_task_is_not_immediately_reclaimed(self):
        with patch.object(worker.time,'time',return_value=1000):
            self.manager.auto_claim_enabled=True
            self.manager._enqueue_claimed_rows([self.row(1)],'自动接单')
            self.finish(abort=True)
            self.assertEqual(self.manager.next_auto_poll_ts,2200)
            self.manager._enqueue_claimed_rows([self.row(2)],'手动领取')
            self.manager.next_auto_poll_ts=0
            self.manager.release_all_queued_tasks()
            self.assertEqual(self.manager.next_auto_poll_ts,2200)

    def test_completion_before_poll_returns_does_not_lose_immediate_follow_up(self):
        with patch.object(worker.time,'time',return_value=1000):
            self.manager.auto_claim_enabled=True
            self.coordinator.claim_next_available.return_value=([self.row(1)],[])
            self.manager.refresh_sheet_tasks.side_effect=self.finish
            self.manager.trigger_auto_claim_once(automatic=True)
            self.assertEqual(self.manager.active_task_count(),0)
            self.assertEqual(self.manager.next_auto_poll_ts,1000)

    def test_network_error_backs_off_without_spinning_or_disabling_auto(self):
        with patch.object(worker.time,'time',return_value=1000):
            self.manager.auto_claim_enabled=True
            self.coordinator.claim_next_available.side_effect=TimeoutError('network timed out')
            self.assertIn('异常',self.manager.trigger_auto_claim_once(automatic=True))
            self.assertTrue(self.manager.auto_claim_enabled)
            self.assertFalse(self.manager._auto_claim_busy)
            self.assertEqual(self.manager.next_auto_poll_ts,2200)

    def test_dashboard_shows_completion_follow_up_instead_of_running_countdown(self):
        from facefusion.uis.components import sheet_worker as ui
        self.manager.cached_file_counts=Mock(return_value=(0,0))
        self.manager.auto_claim_enabled=True
        self.manager._enqueue_claimed_rows([self.row(1)],'自动接单')
        with patch.object(ui,'WORKER_MANAGER',self.manager),patch.object(worker.time,'time',return_value=1000):
            self.assertIn('完整完成后',ui._build_dashboard_html())
            self.assertNotIn('0分00秒后巡检',ui._build_dashboard_html())
            self.manager.queued_tasks.clear();self.manager.next_auto_poll_ts=2200
            self.assertIn('20分00秒后再检查',ui._build_dashboard_html())

    def test_concurrent_poll_cannot_claim_second_task_and_disable_stops_continuation(self):
        entered,release=threading.Event(),threading.Event()
        def claim(**kwargs):
            entered.set();release.wait(3)
            return [self.row(1)],[]
        self.manager.auto_claim_enabled=True
        self.coordinator.claim_next_available.side_effect=claim
        thread=threading.Thread(target=lambda:self.manager.trigger_auto_claim_once(automatic=True))
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertIn('正在进行',self.manager.trigger_auto_claim_once(automatic=True))
            self.manager.set_auto_claim(False)
        finally:
            release.set();thread.join(3)
        self.assertFalse(thread.is_alive())
        self.coordinator.claim_next_available.assert_called_once()
        # An already submitted claim can finish after disabling. It is kept
        # as an owned task, but finishing it must not trigger another claim.
        self.finish()
        self.assertEqual(self.manager.next_auto_poll_ts,0)

    def test_actual_scheduler_and_executor_complete_two_tasks_then_go_idle(self):
        events=[];empty=threading.Event();counter=[0]
        def claim(**kwargs):
            self.assertEqual(kwargs['max_count'],1)
            self.assertEqual(self.manager.active_task_count(),0)
            counter[0]+=1
            if counter[0]>1:self.assertTrue(events[-1].endswith('writeback'))
            if counter[0]<=2:return [self.row(counter[0])],[]
            empty.set();return [],[]
        def process(task):
            for phase in ['download','swap','upload','writeback']:
                self.assertEqual(self.coordinator.claim_next_available.call_count,len(self.manager.history_tasks)+1)
                self.assertEqual(self.manager.active_task_count(),1)
                events.append(task.task_id+':'+phase)
            task.local_status=worker.LOCAL_STATUS_COMPLETED
        self.coordinator.claim_next_available.side_effect=claim
        self.manager._run_single_task=process
        self.manager.set_auto_claim(True)
        threads=[threading.Thread(target=self.manager._execution_loop),threading.Thread(target=self.manager._auto_claim_loop)]
        for thread in threads:thread.start()
        try:
            self.assertTrue(empty.wait(8),'scheduler did not chain completed tasks')
            deadline=time.monotonic()+2
            while self.manager._auto_claim_busy and time.monotonic()<deadline:threading.Event().wait(.01)
            self.assertEqual(len(events),8)
            self.assertEqual(self.manager.active_task_count(),0)
            self.assertEqual(self.coordinator.claim_next_available.call_count,3)
            self.assertAlmostEqual(self.manager.next_auto_poll_ts-time.time(),1200,delta=2)
        finally:
            self.manager._stop_threads=True;self.manager._wake_event.set()
            for thread in threads:thread.join(3)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
