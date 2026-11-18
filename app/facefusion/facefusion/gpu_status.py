"""Thread-safe GPU telemetry shared by the manual and sheet UI panels."""
import copy
import json
import math
import os
import threading
import time
import uuid

from facefusion.gpu_metrics import Collector

TIER_MARKER = 'gpu_task_tier='


class GpuStatus:
    def __init__(self, collector_factory=Collector, autostart=True):
        self.lock = threading.RLock(); self.records = {}; self.devices = {}
        self.collector_factory = collector_factory; self.collector = None
        self.autostart = autostart; self.thread = None
        self.enabled = False
        self.wake = threading.Event(); self.closed = threading.Event()
        self.last_request = 0; self.last_sample = 0

    def _start(self):
        if self.autostart and self.enabled:
            with self.lock:
                if self.thread is None:
                    self.thread = threading.Thread(target=self._loop, name='GpuTelemetry', daemon=True)
                    self.thread.start()
            self.wake.set()

    def enable(self):
        self.enabled = True
        self._start()

    def begin(self, source, task_id, pid, devices=None):
        with self.lock:
            previous = self.records.get(source, {})
            token = uuid.uuid4().hex
            self.records[source] = dict(token=token, task_id=str(task_id)[:200], pid=pid,
                devices=list(devices or [0]), active=True, status='准备处理', tier=None, workers=None,
                total_mib=None, free_at_selection_mib=None, current_mib=None,
                peak_mib=previous.get('peak_mib') if source == 'sheet' and previous.get('task_id') == task_id else None)
        self._start()
        return token

    def finish(self, source, token, status='GPU处理已结束'):
        with self.lock:
            record = self.records.get(source)
            if record and record['token'] == token:
                record.update(active=False, status=status, current_mib=None)

    def finish_task(self, source, task_id):
        with self.lock:
            record = self.records.get(source)
            if record and record['active'] and record['task_id'] == task_id:
                record.update(active=False, status='处理已结束', current_mib=None)

    def update_tier(self, source, workers, free_mib=None, total_mib=None, task_id=None):
        with self.lock:
            record = self.records.get(source)
            if record and record['active'] and (task_id is None or record['task_id'] == task_id):
                record.update(tier=workers.get('tier', '兼容流程'), workers=dict(workers),
                              total_mib=total_mib, free_at_selection_mib=free_mib, status='处理中')

    def note_retry(self, source, task_id=None):
        with self.lock:
            record = self.records.get(source)
            if record and record['active'] and (task_id is None or record['task_id'] == task_id):
                record['status'] = '显存不足，降档重试中'

    def snapshot(self, source, request=True):
        with self.lock:
            if request:
                self.last_request = time.monotonic()
                self.enabled = True
            result = copy.deepcopy(self.records.get(source, {}))
            ids = result.get('devices') or [0]
            result['device'] = copy.deepcopy(self.devices.get(int(ids[0])))
            result['sample_age'] = time.monotonic() - self.last_sample if self.last_sample else None
        if request:
            self._start()
        return result

    def sample_once(self):
        with self.lock:
            tasks = {key: dict(value) for key, value in self.records.items() if value['active']}
        if self.collector is None:
            self.collector = self.collector_factory()
        devices, amounts = self.collector.read(tasks)
        with self.lock:
            self.devices = devices; self.last_sample = time.monotonic()
            for source, original in tasks.items():
                record = self.records.get(source)
                if not record or not record['active'] or record['token'] != original['token']:
                    continue
                current = amounts.get(source)
                record['current_mib'] = current
                if current is not None:
                    record['peak_mib'] = max(current, record['peak_mib'] or 0)

    def _loop(self):
        try:
            while not self.closed.is_set():
                self.wake.wait(1); self.wake.clear()
                with self.lock:
                    active = any(item['active'] for item in self.records.values())
                    interested = time.monotonic() - self.last_request < 6
                if (active or interested) and time.monotonic() - self.last_sample >= .8:
                    try:
                        self.sample_once()
                    except Exception:
                        # Telemetry must never fail a video or block the UI.
                        with self.lock:
                            self.devices = {}; self.last_sample = time.monotonic()
                            for item in self.records.values():
                                item['current_mib'] = None
        finally:
            if self.collector:
                self.collector.close()

    def close(self):
        self.closed.set(); self.wake.set()
        if self.thread:
            self.thread.join(timeout=4)
        elif self.collector:
            self.collector.close()


GPU_STATUS = GpuStatus()


def publish_tier(workers, free_mib, total_mib):
    from facefusion import logger, state_manager
    if state_manager.get_item('command') in (None, 'run'):
        GPU_STATUS.update_tier('manual', workers, free_mib, total_mib)
    logger.info(TIER_MARKER + json.dumps(dict(workers=workers, free_mib=free_mib, total_mib=total_mib), ensure_ascii=False), __name__)


def consume_tier(task_id, line):
    if TIER_MARKER not in line:
        return False
    try:
        data = json.loads(line.split(TIER_MARKER, 1)[1])
        workers = data['workers']
        if not isinstance(workers, dict) or workers.get('tier') not in ['低', '中', '高', '高+', '兼容流程']:
            return True
        workers = dict(tier=workers['tier'], **{key: int(workers[key]) for key in ['metadata','swap','restore','enhance','temple'] if key in workers})
        if any(not 0 <= value <= 64 for key,value in workers.items() if key != 'tier'):
            return True
        free = float(data['free_mib']) if data.get('free_mib') is not None else None
        total = float(data['total_mib']) if data.get('total_mib') is not None else None
        if any(value is not None and (not math.isfinite(value) or value < 0) for value in (free, total)):
            return True
        GPU_STATUS.update_tier('sheet', workers, free, total, task_id=task_id)
    except (ValueError, KeyError, TypeError):
        pass
    return True
