"""Background task queue, 20-min auto-claimer, and isolated headless-run executor for Google Sheets."""
from __future__ import annotations

import collections
import copy
import datetime
import json
import os
import queue
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Sequence, Tuple

from facefusion import presets, process_manager
from facefusion.execution import get_available_execution_providers
from facefusion.google_sheets_client import (
    DEFAULT_TOKEN_PATH,
    SHEET_STATUS_COMPLETED,
    SHEET_STATUS_FAILED,
    SHEET_STATUS_PENDING,
    GoogleDriveClient,
    GoogleOAuthManager,
    GoogleSheetsCoordinator,
    PendingOAuthSession,
    SheetTaskRow,
    _atomic_write_json,
    parse_spreadsheet_ref,
    sanitize_filename_stem,
)
from facefusion.media_normalizer import (
    inspect_and_normalize_video,
    prepare_source_image,
    terminate_process_tree,
)
from facefusion.userdata_paths import get_app_root, get_userdata_dir

APP_ROOT = get_app_root()
USERDATA_DIR = get_userdata_dir()
WORKER_CONFIG_PATH = USERDATA_DIR / 'facefusion-sheet-worker.json'
CLIENT_SECRET_COPY_PATH = USERDATA_DIR / 'facefusion-google-client-secret.json'
FAILED_OUTPUT_BACKUP_DIR = USERDATA_DIR / 'sheet_saved_outputs'
LOCAL_RECENT_OUTPUT_DIR = USERDATA_DIR / 'sheet_outputs'
SHEET_TEMP_ROOT = USERDATA_DIR / 'sheet_temp'
GRADIO_TEMP_DIR = USERDATA_DIR / 'temp' / 'gradio'
MAX_RECENT_LOCAL_OUTPUTS = 10
MAX_FAILED_BACKUP_FILES = 20
MIN_FREE_DISK_BYTES = 3 * 1024 * 1024 * 1024  # 3 GB minimum free space before starting a new task
SWAP_IDLE_TIMEOUT_SEC = 600.0                 # 10 min watchdog if headless-run freezes with zero output
TEMP_SWEEP_INTERVAL_SEC = 20 * 60             # Sweep orphan sheet_temp & stale Gradio temp every 20 min
GRADIO_TEMP_TTL_SEC = 12 * 3600               # Clean Gradio upload cache files older than 12 hours when idle
DISK_COUNT_CACHE_TTL_SEC = 15.0               # Avoid 2s UI timer scanning MP4 folders on disk repeatedly

# Shared in-process GPU mutex coordinating Manual UI tab and Sheet Worker tab
GPU_EXECUTION_LOCK = threading.Lock()
TASK_ID_EXTRACT_RE = re.compile(r'(FF-\d{4}-\d{6}-[A-Z0-9]{4})')
SWAPPED_TOTAL_RE = re.compile(r'swapped_frames_total=(\d+)')

AUTO_CLAIM_INTERVAL_MINUTES = 20
AUTO_CLAIM_INTERVAL_SEC = AUTO_CLAIM_INTERVAL_MINUTES * 60  # 20 minutes
AUTO_MAX_ACTIVE_TASKS = 2          # Max 1 in production + 1 queued backlog for auto-claim

# Rich local statuses (NOT spammed to Google Sheets; Sheets only gets 已领取制作中 -> 已完成/制作失败)
LOCAL_STATUS_QUEUED = '排队等待中'
LOCAL_STATUS_WAITING_GPU = '等待手动界面释放显卡'
LOCAL_STATUS_DOWNLOADING = '1/4 下载素材中'
LOCAL_STATUS_NORMALIZING = '2/4 视频检测/标准化中'
LOCAL_STATUS_SWAPPING = '3/4 换脸制作中'
LOCAL_STATUS_UPLOADING = '4/4 上传网盘并回写表格中'
LOCAL_STATUS_COMPLETED = '已完成'
LOCAL_STATUS_FAILED = '制作失败'
LOCAL_STATUS_RETURNED = '已退回表格'
LOCAL_STATUS_PAUSED_AUTH = '已暂停(待重授权恢复)'

TQDM_PROGRESS_RE = re.compile(r'(?:([a-zA-Z_]+):\s*)?(\d{1,3})%\|.*?\|\s*(\d+/\d+)\s*\[([^\]]+)\]')


def _robust_rmtree(target: Path, retries: int = 4, delay_sec: float = 0.25) -> None:
    """Reliably remove a directory on Windows even if child process file handles release slightly late."""
    if not target.exists():
        return

    def _on_rm_error(func: Callable[..., Any], path_str: str, _exc_info: Any) -> None:
        try:
            os.chmod(path_str, stat.S_IWRITE)
            func(path_str)
        except OSError:
            pass

    for attempt in range(retries):
        if not target.exists():
            return
        shutil.rmtree(target, ignore_errors=False, onerror=_on_rm_error) if attempt == 0 else shutil.rmtree(target, ignore_errors=True)
        if not target.exists():
            return
        if attempt < retries - 1:
            time.sleep(delay_sec * (attempt + 1))


def _is_fatal_auth_error(err_text: str) -> bool:
    text = err_text or ''
    return any(
        marker in text
        for marker in (
            'invalid_grant',
            '授权令牌已过期',
            '尚未完成 Google 账号授权',
            '授权凭证缺少',
            '授权凭证文件损坏'
        )
    )


def _is_fatal_drive_or_quota_error(err_text: str) -> bool:
    text = err_text or ''
    return any(
        marker in text
        for marker in (
            'storageQuotaExceeded',
            '云端硬盘空间已满',
            '无权写入所配置的成品文件夹'
        )
    )


def save_and_prune_recent_output(
    source_mp4: Path,
    filename: str,
    output_dir: Optional[Path] = None,
    max_keep: int = MAX_RECENT_LOCAL_OUTPUTS
) -> Path:
    """Save a local copy of a completed MP4 and prune oldest files beyond `max_keep` (default 10)."""
    target_dir = output_dir if output_dir is not None else LOCAL_RECENT_OUTPUT_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / filename
    if source_mp4.resolve() != dest.resolve():
        shutil.copy2(source_mp4, dest)
    existing_mtimes = [
        p.stat().st_mtime
        for p in target_dir.glob('*.mp4')
        if p.is_file() and p.resolve() != dest.resolve()
    ]
    now_ts = max([time.time()] + [m + 0.002 for m in existing_mtimes])
    try:
        os.utime(dest, (now_ts, now_ts))
    except OSError:
        pass

    if max_keep >= 0:
        mp4_files = [p for p in target_dir.glob('*.mp4') if p.is_file()]
        mp4_files.sort(key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
        for stale in mp4_files[max_keep:]:
            try:
                stale.unlink(missing_ok=True)
            except OSError:
                pass
    return dest


@dataclass
class WorkerConfig:
    worker_name: str = ''
    spreadsheet_url: str = ''
    sheet_title: str = ''
    drive_folder_url: str = ''
    client_secret_path: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'worker_name': self.worker_name,
            'spreadsheet_url': self.spreadsheet_url,
            'sheet_title': self.sheet_title,
            'drive_folder_url': self.drive_folder_url,
            'client_secret_path': self.client_secret_path
        }

    @classmethod
    def load(cls, path: Path = WORKER_CONFIG_PATH) -> 'WorkerConfig':
        if not path.is_file():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(raw, dict):
                return cls()
            return cls(
                worker_name=str(raw.get('worker_name') or '').strip(),
                spreadsheet_url=str(raw.get('spreadsheet_url') or '').strip(),
                sheet_title=str(raw.get('sheet_title') or '').strip(),
                drive_folder_url=str(raw.get('drive_folder_url') or '').strip(),
                client_secret_path=str(raw.get('client_secret_path') or '').strip()
            )
        except (OSError, ValueError):
            return cls()

    def save(self, path: Path = WORKER_CONFIG_PATH) -> None:
        _atomic_write_json(path, self.to_dict())


@dataclass
class LocalTask:
    task_id: str
    initial_row_number: int
    source_url: str
    target_url: str
    preset_name: str
    claim_mode: str  # '手动领取' or '自动接单'
    claimed_at: str = field(default_factory=lambda: datetime.datetime.now().strftime('%H:%M:%S'))
    local_status: str = LOCAL_STATUS_QUEUED
    progress_pct: float = 0.0
    progress_detail: str = '等待前序任务完成'
    output_url: str = ''
    note: str = ''
    started_mono: float = 0.0
    finished_at: str = ''
    abort_requested: bool = False
    abort_return_to_sheet: bool = True
    local_backup_path: str = ''
    upload_filename: str = ''
    final_note_base: str = ''


def resolve_preset_values_by_label(preset_text: str) -> Tuple[str, Dict[str, Any], str]:
    """
    Resolve a sheet cell preset name (label, key, or custom preset name) to validated FaceFusion values.
    Returns (resolved_display_label, values_dict, warning_if_fallback).
    """
    cleaned = (preset_text or '').strip()
    if cleaned.endswith('（自定义）'):
        cleaned = cleaned[:-len('（自定义）')].strip()

    default_builtin = presets.BUILTINS['clear']
    if not cleaned or cleaned in {'默认', '正常', '默认预设'}:
        return default_builtin['label'], copy.deepcopy(default_builtin['values']), ''

    if cleaned in presets.BUILTINS:
        item = presets.BUILTINS[cleaned]
        return item['label'], copy.deepcopy(item['values']), ''

    for item in presets.BUILTINS.values():
        if item['label'] == cleaned:
            return item['label'], copy.deepcopy(item['values']), ''

    try:
        custom_map = presets.read_custom()
        if cleaned in custom_map:
            return f'{cleaned}（自定义）', copy.deepcopy(custom_map[cleaned]), ''
    except ValueError:
        pass

    return (
        default_builtin['label'],
        copy.deepcopy(default_builtin['values']),
        f'未知预设“{cleaned}”，已回退默认预设'
    )


def build_headless_command(
    python_executable: str,
    facefusion_script: Path,
    source_image: Path,
    target_video: Path,
    output_video: Path,
    temp_dir: Path,
    jobs_dir: Path,
    preset_values: Dict[str, Any],
    video_encoder: Optional[str] = None,
    config_path: Optional[Path] = None
) -> List[str]:
    """
    Build a deterministic, fully isolated `headless-run` command.
    Intentionally decoupled from Manual UI Tab `state_manager` mutations so switching Manual UI
    to `reference` mode or `cpu` never pollutes background Sheet Worker tasks.
    """
    try:
        available_providers = get_available_execution_providers()
    except Exception:
        available_providers = ['cuda']
    if 'cuda' in available_providers:
        exec_providers = ['cuda']
    elif available_providers:
        exec_providers = [available_providers[0]]
    else:
        exec_providers = ['cpu']

    out_encoder = video_encoder or 'h264_nvenc'
    padding = [str(int(v)) for v in preset_values.get('face_mask_padding', [0, 0, 0, 0])]

    cmd: List[str] = [
        python_executable,
        str(facefusion_script),
        'headless-run',
    ]
    if config_path is not None:
        cmd.extend(['--config-path', str(config_path)])
    cmd.extend([
        '--temp-path', str(temp_dir),
        '--jobs-path', str(jobs_dir),
        '-s', str(source_image),
        '-t', str(target_video),
        '-o', str(output_video),
        '--execution-providers', *[str(p) for p in exec_providers],
        '--execution-thread-count', '2',
        '--video-memory-strategy', 'moderate',
        '--workflow-strategy', 'disk',
        '--temp-frame-format', 'jpeg',
        '--face-selector-mode', 'one',
        '--face-selector-order', 'large-small',
        '--processors', *[str(p) for p in preset_values['processors']],
        '--face-swapper-model', str(preset_values['face_swapper_model']),
        '--face-swapper-pixel-boost', str(preset_values['face_swapper_pixel_boost']),
        '--face-swapper-weight', str(preset_values['face_swapper_weight']),
        '--expression-restorer-model', str(preset_values['expression_restorer_model']),
        '--expression-restorer-factor', str(int(preset_values['expression_restorer_factor'])),
        '--expression-restorer-areas', *[str(a) for a in preset_values['expression_restorer_areas']],
        '--face-enhancer-model', str(preset_values['face_enhancer_model']),
        '--face-enhancer-blend', str(int(preset_values['face_enhancer_blend'])),
        '--face-enhancer-weight', str(preset_values['face_enhancer_weight']),
        '--face-mask-types', *[str(t) for t in preset_values['face_mask_types']],
        '--face-mask-areas', *[str(a) for a in preset_values['face_mask_areas']],
        '--face-mask-regions', *[str(r) for r in preset_values['face_mask_regions']],
        '--face-mask-blur', str(preset_values['face_mask_blur']),
        '--face-mask-padding', *padding,
        '--face-parser-model', str(preset_values['face_parser_model']),
        '--face-occluder-model', str(preset_values['face_occluder_model']),
        '--output-video-encoder', str(out_encoder),
        '--output-audio-encoder', 'aac',
        '--output-video-quality', '90',
        '--log-level', 'info'
    ])
    return cmd


class SheetWorkerManager:
    """Thread-safe orchestrator for sheet polling, manual/auto task claiming, and background execution."""

    def __init__(
        self,
        config_path: Path = WORKER_CONFIG_PATH,
        token_path: Path = DEFAULT_TOKEN_PATH,
        client_secret_copy_path: Optional[Path] = None
    ) -> None:
        self._lock = threading.RLock()
        self._wake_event = threading.Event()
        self.config_path = Path(config_path)
        if client_secret_copy_path is not None:
            self.client_secret_copy_path = Path(client_secret_copy_path)
        elif self.config_path != WORKER_CONFIG_PATH:
            self.client_secret_copy_path = self.config_path.parent / 'facefusion-google-client-secret.json'
        else:
            self.client_secret_copy_path = CLIENT_SECRET_COPY_PATH
        self.config = WorkerConfig.load(self.config_path)
        self.oauth = GoogleOAuthManager(token_path=token_path)

        # Active non-blocking OAuth loopback session state
        self._pending_oauth_session: Optional[PendingOAuthSession] = None
        self.pending_auth_url: str = ''
        self.auth_in_progress: bool = False

        # Cached sheet snapshot from last refresh
        self.spreadsheet_title: str = ''
        self.available_sheets: List[str] = []
        self.last_refresh_at: str = '尚未刷新'
        self.cached_sheet_rows: List[SheetTaskRow] = []
        self.snapshot_revision: int = 0

        # Local execution queue & history
        self.current_task: Optional[LocalTask] = None
        self._current_proc: Optional[subprocess.Popen[Any]] = None
        self.queued_tasks: Deque[LocalTask] = collections.deque()
        self.history_tasks: Deque[LocalTask] = collections.deque(maxlen=50)

        # Auto-claim state (20-min interval, cap at 2 active: 1 running + 1 queued)
        self.auto_claim_enabled: bool = False
        self.next_auto_poll_ts: float = 0.0
        self._auto_claim_busy: bool = False

        # Live logs, banner message & lightweight UI change version
        self.logs: Deque[str] = collections.deque(maxlen=150)
        if self.oauth.has_valid_refresh_token() and self.config.worker_name.strip() and self.config.spreadsheet_url.strip():
            self.last_banner: str = '已检测到历史绑定配置，正在自动连接云表格...'
        elif self.has_client_secret_file() and not self.oauth.has_valid_refresh_token():
            self.last_banner = '已就绪客户端 JSON 文件，请点击【第 2 步：🔑 登录 Google】完成网页授权。'
        else:
            self.last_banner = '首次使用请按下方 1 → 2 → 3 步：填写工位名称、登录 Google 账号、粘贴云表格链接并保存连接。'
        self.last_connect_failed: bool = False
        self.ui_state_version: int = 1

        # Cached local MP4 counts to avoid 2s UI timer scanning directories repeatedly
        self._counts_cache_mono: float = 0.0
        self._cached_backup_cnt: int = 0
        self._cached_recent_cnt: int = 0
        self._last_temp_sweep_mono: float = 0.0

        self._stop_threads = False
        self._startup_autoload_started = False
        self._orphan_temp_cleaned = False
        self._worker_thread: Optional[threading.Thread] = None
        self._scheduler_thread: Optional[threading.Thread] = None

    def bump_ui_version(self) -> None:
        with self._lock:
            self.ui_state_version += 1

    def invalidate_disk_counts_cache(self) -> None:
        with self._lock:
            self._counts_cache_mono = 0.0

    def cached_file_counts(self) -> Tuple[int, int]:
        """Return `(saved_backup_count, recent_local_output_count)` with TTL caching."""
        now_mono = time.monotonic()
        with self._lock:
            if self._counts_cache_mono > 0 and (now_mono - self._counts_cache_mono) < DISK_COUNT_CACHE_TTL_SEC:
                return self._cached_backup_cnt, self._cached_recent_cnt
        backup_cnt = len(self.saved_backup_files())
        recent_cnt = len(self.recent_local_output_files())
        with self._lock:
            self._cached_backup_cnt = backup_cnt
            self._cached_recent_cnt = recent_cnt
            self._counts_cache_mono = time.monotonic()
        return backup_cnt, recent_cnt

    def cleanup_orphan_temp_dirs(self) -> None:
        """Remove stale task workspaces under `SHEET_TEMP_ROOT` left by prior forced exits."""
        with self._lock:
            if self._orphan_temp_cleaned or self.current_task is not None:
                return
            self._orphan_temp_cleaned = True
        self.sweep_runtime_temp_dirs(force=True)

    def sweep_runtime_temp_dirs(self, force: bool = False) -> None:
        """Periodically purge stale `sheet_temp/*` orphan folders and expired Gradio upload cache."""
        now_mono = time.monotonic()
        with self._lock:
            if not force and self._last_temp_sweep_mono > 0 and (now_mono - self._last_temp_sweep_mono) < TEMP_SWEEP_INTERVAL_SEC:
                return
            self._last_temp_sweep_mono = now_mono
            active_id = self.current_task.task_id if self.current_task is not None else ''

        if SHEET_TEMP_ROOT.is_dir():
            for child in list(SHEET_TEMP_ROOT.iterdir()):
                if active_id and child.name == active_id:
                    continue
                _robust_rmtree(child, retries=2, delay_sec=0.1)

        if GRADIO_TEMP_DIR.is_dir() and process_manager.is_pending():
            cutoff_ts = time.time() - GRADIO_TEMP_TTL_SEC
            for entry in list(GRADIO_TEMP_DIR.iterdir()):
                try:
                    if entry.stat().st_mtime < cutoff_ts:
                        if entry.is_dir():
                            shutil.rmtree(entry, ignore_errors=True)
                        else:
                            entry.unlink(missing_ok=True)
                except OSError:
                    pass

    def ensure_background_threads(self) -> None:
        self.cleanup_orphan_temp_dirs()
        with self._lock:
            if self._worker_thread is None or not self._worker_thread.is_alive():
                self._worker_thread = threading.Thread(target=self._execution_loop, name='SheetTaskExecutor', daemon=True)
                self._worker_thread.start()
            if self._scheduler_thread is None or not self._scheduler_thread.is_alive():
                self._scheduler_thread = threading.Thread(target=self._auto_claim_loop, name='SheetAutoClaimer', daemon=True)
                self._scheduler_thread.start()

    def _require_worker_name(self) -> str:
        with self._lock:
            name = self.config.worker_name.strip()
        if not name:
            raise ValueError('请先在上方配置区自定义填写“本机节点名称”（例如：Alex、Worker-01），用于多人协同防冲突标识。')
        return name

    def trigger_startup_autoload_once(self) -> None:
        """Silently load initial sheet snapshot & recover unfinished owned tasks once on UI startup."""
        with self._lock:
            if self._startup_autoload_started:
                return
            if not self.config.worker_name.strip() or not self.config.spreadsheet_url or not self.oauth.has_valid_refresh_token():
                return
            self._startup_autoload_started = True

        def _bg_init() -> None:
            try:
                doc_title, _, selected = self.test_and_load_sheets()
                with self._lock:
                    self.last_connect_failed = False
                    has_active = self.active_task_count() > 0
                if not has_active:
                    self.set_banner(f'已自动连接《{doc_title}》工作表“{selected}”。')
            except Exception as exc:
                with self._lock:
                    self.last_connect_failed = True
                self.set_banner(f'启动自动读取表格未成功（请展开上方配置检查授权账号/表格权限，或点“刷新表格需求”重试）：{exc}')

        threading.Thread(target=_bg_init, name='SheetStartupLoader', daemon=True).start()

    def log(self, message: str) -> None:
        stamp = datetime.datetime.now().strftime('%H:%M:%S')
        with self._lock:
            self.logs.append(f'[{stamp}] {message}')
            self.ui_state_version += 1

    def set_banner(self, message: str) -> None:
        with self._lock:
            self.last_banner = message
            self.ui_state_version += 1
        self.log(message)

    def _sync_cached_row_local(
        self,
        task_id: str,
        *,
        status: Optional[str] = None,
        output_url: Optional[str] = None,
        note: Optional[str] = None,
        clear_claim: bool = False
    ) -> None:
        """Update in-memory `cached_sheet_rows` immediately without an extra full-sheet HTTP download."""
        clean_id = (task_id or '').strip()
        if not clean_id:
            return
        with self._lock:
            changed = False
            for row in self.cached_sheet_rows:
                if row.task_id == clean_id:
                    if clear_claim:
                        row.task_id = ''
                        row.status = SHEET_STATUS_PENDING
                        row.worker = ''
                        row.note = ''
                    else:
                        if status is not None:
                            row.status = status
                        if output_url is not None:
                            row.output_url = output_url
                        if note is not None:
                            row.note = note
                    changed = True
                    break
            if changed:
                self.snapshot_revision += 1
                self.ui_state_version += 1

    def active_task_count(self) -> int:
        with self._lock:
            return (1 if self.current_task is not None else 0) + len(self.queued_tasks)

    def resolve_client_secret_path(self) -> Optional[Path]:
        with self._lock:
            cfg_path = (self.config.client_secret_path or '').strip()
        if cfg_path:
            p = Path(cfg_path)
            if p.is_file() and p.resolve() != self.oauth.token_path.resolve():
                return p
        if self.client_secret_copy_path.is_file():
            return self.client_secret_copy_path
        return None

    def has_client_secret_file(self) -> bool:
        return self.resolve_client_secret_path() is not None

    def import_uploaded_secret_file(self, uploaded_secret_file: str) -> Tuple[str, str]:
        """
        Validate & persist an uploaded OAuth JSON file immediately.
        Returns `(mode, status_message)` where `mode` is `'client_secret'` or `'imported_token'`.
        """
        src = Path(uploaded_secret_file)
        if not src.is_file():
            raise ValueError('找不到上传的 JSON 文件，请重新选择上传。')
        mode = self.oauth.import_token_or_client(src)
        with self._lock:
            if mode == 'client_secret':
                self.client_secret_copy_path.parent.mkdir(parents=True, exist_ok=True)
                if src.resolve() != self.client_secret_copy_path.resolve():
                    shutil.copy2(src, self.client_secret_copy_path)
                self.config.client_secret_path = str(self.client_secret_copy_path)
                self.config.save(self.config_path)
                self.ui_state_version += 1
            else:
                if self._pending_oauth_session is not None:
                    self._pending_oauth_session.cancel()
                    self._pending_oauth_session = None
                self.pending_auth_url = ''
                self.auth_in_progress = False
                self.config.client_secret_path = str(self.oauth.token_path)
                self.config.save(self.config_path)
                self.ui_state_version += 1

        if mode == 'client_secret':
            msg = '客户端 JSON 校验通过！请点击下方【🔑 登录 Google】打开网页完成授权。'
            self.set_banner(msg)
            return mode, msg

        email = self.oauth.fetch_account_email()
        msg = f'已直接导入有效 Google 授权凭证（{email or "已就绪"}），无需再次网页登录。'
        self.set_banner(msg)
        return mode, msg

    def update_config(
        self,
        worker_name: str,
        spreadsheet_url: str,
        sheet_title: str,
        drive_folder_url: str,
        uploaded_secret_file: Optional[str] = None
    ) -> str:
        with self._lock:
            old_url = self.config.spreadsheet_url
            old_sheet = self.config.sheet_title
            new_url = (spreadsheet_url or '').strip()
            new_sheet = (sheet_title or '').strip()

            # If user switched to a different spreadsheet URL while dropdown still held the old spreadsheet's sheet name,
            # clear sheet_title so resolve_sheet_title() automatically picks the new spreadsheet's #gid or first tab!
            if old_url and new_url and new_url != old_url:
                try:
                    old_sid, _ = parse_spreadsheet_ref(old_url)
                except ValueError:
                    old_sid = old_url
                try:
                    new_sid, new_gid = parse_spreadsheet_ref(new_url)
                except ValueError:
                    new_sid, new_gid = new_url, None
                if new_sid != old_sid and new_sheet == old_sheet:
                    new_sheet = ''
                elif new_gid is not None and new_sheet == old_sheet:
                    # Same spreadsheet but user pasted a URL pointing to a specific #gid=... tab
                    new_sheet = ''

            self.config.worker_name = (worker_name or '').strip()[:30]
            self.config.spreadsheet_url = new_url
            self.config.sheet_title = new_sheet
            self.config.drive_folder_url = (drive_folder_url or '').strip()
            self.config.save(self.config_path)
            self.ui_state_version += 1

        if uploaded_secret_file and Path(uploaded_secret_file).is_file():
            mode, msg = self.import_uploaded_secret_file(uploaded_secret_file)
            if mode == 'imported_token':
                return msg
        return '配置已保存。'

    def _try_autoconnect_after_auth(self, email_label: str) -> str:
        """If worker_name and spreadsheet_url are already configured, connect to the sheet right after auth."""
        with self._lock:
            has_worker = bool(self.config.worker_name.strip())
            has_url = bool(self.config.spreadsheet_url.strip())
        if not has_worker or not has_url:
            missing_parts: List[str] = []
            if not has_worker:
                missing_parts.append('第 1 步工位名称')
            if not has_url:
                missing_parts.append('第 3 步云表格链接')
            msg = f'Google 账号授权成功（{email_label}）！请继续填写{"与".join(missing_parts)}，然后点击“保存配置并连接表格”。'
            self.set_banner(msg)
            return msg

        self.set_banner(f'Google 账号授权成功（{email_label}），正在自动连接云表格...')
        try:
            doc_title, _, selected = self.test_and_load_sheets()
            with self._lock:
                self.last_connect_failed = False
                self.ui_state_version += 1
            msg = f'Google 账号授权成功（{email_label}），已自动连接表格《{doc_title} / {selected}》！'
            self.set_banner(msg)
            return msg
        except Exception as exc:
            with self._lock:
                self.last_connect_failed = True
                self.ui_state_version += 1
            msg = f'Google 账号已授权（{email_label}），但读取云表格未成功，请检查表格链接或账号访问权限：{exc}'
            self.set_banner(msg)
            return msg

    def start_google_auth_session(
        self,
        uploaded_secret_file: Optional[str] = None,
        open_browser: bool = True
    ) -> Tuple[str, str]:
        """
        Non-blocking OAuth starter for the Gradio UI.
        Returns `(auth_url, banner_message)` in <50ms so Gradio never hangs or whites out components!
        """
        if uploaded_secret_file and Path(uploaded_secret_file).is_file():
            mode, msg = self.import_uploaded_secret_file(uploaded_secret_file)
            if mode == 'imported_token':
                email = self.oauth.read_token_data().get('authorized_email') or '已导入凭证'
                final_msg = self._try_autoconnect_after_auth(email)
                return '', final_msg

        secret_path = self.resolve_client_secret_path()
        if secret_path is None:
            raise ValueError('请先在第 2 步上传客户端 JSON 文件（从 Google Cloud 下载的桌面应用 client_secret.json）。')

        with self._lock:
            if self._pending_oauth_session is not None:
                self._pending_oauth_session.cancel()
                self._pending_oauth_session = None

        holder: Dict[str, PendingOAuthSession] = {}

        def _on_bg_complete(payload: Optional[Dict[str, Any]], err_msg: Optional[str]) -> None:
            sess_obj = holder.get('session')
            with self._lock:
                if sess_obj is not None and self._pending_oauth_session is not sess_obj:
                    return
                self._pending_oauth_session = None
                self.pending_auth_url = ''
                self.auth_in_progress = False
                self.ui_state_version += 1

            if payload is not None:
                acct = str(payload.get('authorized_email') or '已绑定账号')
                self._try_autoconnect_after_auth(acct)
            elif err_msg:
                self.set_banner(err_msg)

        session = self.oauth.start_auth_session(secret_path, timeout_sec=300, on_complete=_on_bg_complete)
        holder['session'] = session

        with self._lock:
            self._pending_oauth_session = session
            self.pending_auth_url = session.authorization_url
            self.auth_in_progress = True
            self.ui_state_version += 1

        self.log(f'已生成 Google 授权链接（如浏览器未弹出可复制到常用浏览器打开）：{session.authorization_url}')
        if open_browser:
            try:
                webbrowser.open(session.authorization_url)
            except Exception:
                pass

        msg = '已发起 Google 登录授权！请在弹出的浏览器网页中点击“允许”（若未自动弹出，请直接点击第 2 步下方的蓝色授权链接）。'
        self.set_banner(msg)
        return session.authorization_url, msg

    def complete_google_auth_manual(self, raw_callback_or_code: str) -> str:
        """Complete the active pending OAuth session using a user-pasted redirect URL or code."""
        with self._lock:
            session = self._pending_oauth_session
        if session is None:
            if self.oauth.has_valid_refresh_token():
                email = self.oauth.read_token_data().get('authorized_email') or '已绑定账号'
                return self._try_autoconnect_after_auth(email)
            raise ValueError('当前没有等待中的登录会话，请先点击“🔑 登录 Google”打开授权网页。')

        payload = self.oauth.complete_auth_session_manually(session, raw_callback_or_code)
        with self._lock:
            if self._pending_oauth_session is session:
                self._pending_oauth_session = None
            self.pending_auth_url = ''
            self.auth_in_progress = False
            self.ui_state_version += 1

        acct = str(payload.get('authorized_email') or '已绑定账号')
        return self._try_autoconnect_after_auth(acct)

    def authorize_google_interactive(self, uploaded_secret_file: Optional[str] = None) -> str:
        """Synchronous helper preserved for CLI/tests."""
        if uploaded_secret_file:
            self.update_config(
                self.config.worker_name,
                self.config.spreadsheet_url,
                self.config.sheet_title,
                self.config.drive_folder_url,
                uploaded_secret_file=uploaded_secret_file
            )
            if self.oauth.has_valid_refresh_token() and self.config.client_secret_path == str(self.oauth.token_path):
                email = self.oauth.fetch_account_email()
                return f'Google 账号授权就绪：{email or "Token 有效"}'

        secret_path = self.resolve_client_secret_path()
        if secret_path is None:
            raise ValueError('请先选择上传 Google OAuth 客户端授权文件（client_secret.json）。')

        self.log('正在打开浏览器进行 Google 账号登录授权，请在浏览器中点击允许...')
        token_info = self.oauth.authorize_interactive(
            secret_path,
            timeout_sec=180,
            open_browser=True,
            url_callback=lambda auth_url: self.log(f'如默认浏览器未弹出或登录账号不对，请复制此授权链接打开：{auth_url}')
        )
        email = token_info.get('authorized_email') or '已授权'
        msg = f'Google 账号授权成功：{email}'
        self.set_banner(msg)
        return msg

    def logout_google_account(self) -> str:
        """Clear saved Google OAuth token so the operator can switch to a different Google account."""
        with self._lock:
            if self._pending_oauth_session is not None:
                self._pending_oauth_session.cancel()
                self._pending_oauth_session = None
            self.pending_auth_url = ''
            self.auth_in_progress = False
        self.oauth.clear_token()
        with self._lock:
            self.auto_claim_enabled = False
            self.next_auto_poll_ts = 0.0
            self.cached_sheet_rows = []
            self.spreadsheet_title = ''
            self.available_sheets = []
            self.last_refresh_at = '已解绑'
            self.last_connect_failed = False
            self.snapshot_revision += 1
            self.ui_state_version += 1
        msg = '已解绑当前 Google 账号，可点击“🔑 登录 Google”切换新账号登录。'
        self.set_banner(msg)
        return msg

    def get_auth_status_text(self) -> str:
        with self._lock:
            in_prog = self.auth_in_progress
        if in_prog:
            return '⏳ 等待浏览器确认授权（请在网页点击允许）'
        if not self.oauth.has_valid_refresh_token():
            if self.has_client_secret_file():
                return '🟡 已就绪客户端文件，请点“🔑 登录 Google”'
            return '⚪ 未授权：请先上传客户端 JSON 文件'
        try:
            data = self.oauth.read_token_data()
            email = data.get('authorized_email') or '已绑定账号'
            return f'🟢 已授权：{email}'
        except ValueError as exc:
            return f'⚠️ 授权异常：{exc}'

    def _make_coordinator(self) -> GoogleSheetsCoordinator:
        with self._lock:
            url = self.config.spreadsheet_url
            sheet = self.config.sheet_title
        return GoogleSheetsCoordinator(self.oauth, url, sheet_title=sheet or None)

    def test_and_load_sheets(self) -> Tuple[str, List[str], str]:
        coord = self._make_coordinator()
        doc_title, sheets = coord.fetch_spreadsheet_metadata(use_cache=True)
        titles = [name for name, _ in sheets]
        resolved = coord.resolve_sheet_title()
        with self._lock:
            self.spreadsheet_title = doc_title
            self.available_sheets = titles
            if self.config.sheet_title != resolved:
                self.config.sheet_title = resolved
                self.config.save(self.config_path)
            selected = self.config.sheet_title
        self.refresh_sheet_tasks()
        self.recover_owned_in_progress_tasks(quiet_if_empty=True)
        return doc_title, titles, selected

    def refresh_sheet_tasks(self) -> List[SheetTaskRow]:
        try:
            coord = self._make_coordinator()
            resolved_title, _, rows = coord.fetch_tasks_snapshot(ensure_system_columns=False)
        except Exception:
            with self._lock:
                self.last_connect_failed = True
            raise
        stamp = datetime.datetime.now().strftime('%H:%M:%S')
        with self._lock:
            self.last_connect_failed = False
            self.config.sheet_title = resolved_title
            self.cached_sheet_rows = rows
            self.last_refresh_at = stamp
            self.snapshot_revision += 1
            self.ui_state_version += 1
        claimable_cnt = sum(1 for r in rows if r.is_claimable)
        in_prog_cnt = sum(1 for r in rows if not r.is_claimable and r.status != SHEET_STATUS_COMPLETED and r.status != SHEET_STATUS_FAILED and (r.task_id or r.status))
        self.log(f'已刷新工作表“{resolved_title}”：待领取 {claimable_cnt} 条，制作中 {in_prog_cnt} 条，总有效记录 {len(rows)} 条。')
        return rows

    def _enqueue_claimed_rows(self, claimed_rows: Sequence[SheetTaskRow], mode_label: str) -> List[LocalTask]:
        self.ensure_background_threads()
        added: List[LocalTask] = []
        with self._lock:
            existing_ids = {t.task_id for t in self.queued_tasks}
            if self.current_task:
                existing_ids.add(self.current_task.task_id)
            for row in claimed_rows:
                if row.task_id in existing_ids:
                    continue
                local_task = LocalTask(
                    task_id=row.task_id,
                    initial_row_number=row.row_number,
                    source_url=row.source_url,
                    target_url=row.target_url,
                    preset_name=row.preset_name or '没问题选这个',
                    claim_mode=mode_label
                )
                self.queued_tasks.append(local_task)
                added.append(local_task)
            if added:
                self.ui_state_version += 1
        if added:
            self._wake_event.set()
        return added

    def claim_manual_selection(
        self,
        selected_tokens: Sequence[str],
        allow_retry_failed: bool = False
    ) -> Tuple[int, List[str]]:
        """
        Manual claim by user checkbox selection (NO backlog limit).
        selected_tokens encode JSON `{"row": int, "src": str, "tgt": str}`.
        """
        worker = self._require_worker_name()
        if not selected_tokens:
            raise ValueError('请先在待领需求列表中勾选至少一条要领取的需求。')

        targets: List[Tuple[int, str, str]] = []
        for token in selected_tokens:
            try:
                obj = json.loads(token)
                targets.append((int(obj['row']), str(obj['src']), str(obj['tgt'])))
            except (ValueError, KeyError, TypeError):
                continue
        if not targets:
            raise ValueError('所选需求格式无效，请先点击“刷新表格需求”后再勾选。')

        coord = self._make_coordinator()
        if allow_retry_failed:
            claimed_rows, conflicts = coord.claim_specific_tasks(
                targets,
                worker_name=worker,
                allow_retry_failed=True
            )
        else:
            claimed_rows, conflicts = coord.claim_specific_tasks(
                targets,
                worker_name=worker
            )
        added = self._enqueue_claimed_rows(claimed_rows, mode_label='手动领取')

        # Refresh local view so claimed/conflicted items disappear from claimable list
        try:
            self.refresh_sheet_tasks()
        except Exception:
            pass

        parts: List[str] = []
        if added:
            ids_str = '、'.join(t.task_id for t in added[:4]) + (' 等' if len(added) > 4 else '')
            parts.append(f'成功手动领取 {len(added)} 条需求加入本机队列（{ids_str}）。')
        else:
            parts.append('本次未能领取到任何需求。')
        if conflicts:
            parts.append('冲突提示：' + '；'.join(conflicts))

        summary = ' '.join(parts)
        self.set_banner(summary)
        return len(added), conflicts

    def recover_owned_in_progress_tasks(self, quiet_if_empty: bool = False) -> Tuple[int, str]:
        """Resume unfinished `已领取制作中` tasks belonging to `self.config.worker_name` (e.g. after app restart)."""
        with self._lock:
            worker_name = self.config.worker_name.strip()
            has_sheet = bool(self.config.spreadsheet_url)
        if not worker_name:
            msg = '请先自定义填写并保存“本机节点名称”，才能恢复归属该节点的未完成任务。'
            if not quiet_if_empty:
                raise ValueError(msg)
            return 0, msg
        if not has_sheet or not self.oauth.has_valid_refresh_token():
            msg = '请先完成 Google 授权并配置表格链接。'
            if not quiet_if_empty:
                raise ValueError(msg)
            return 0, msg

        coord = self._make_coordinator()
        owned_rows = coord.find_owned_in_progress_tasks(worker_name)
        added = self._enqueue_claimed_rows(owned_rows, mode_label='断点恢复')
        if added:
            ids_str = '、'.join(t.task_id for t in added[:4]) + (' 等' if len(added) > 4 else '')
            msg = f'已恢复本机 [{worker_name}] 上次中断未完成的 {len(added)} 条任务（{ids_str}），已加入本机制作队列！'
            self.set_banner(msg)
            return len(added), msg
        msg = f'表格中没有发现归属本机 [{worker_name}] 且未入队的遗留制作中任务。'
        if not quiet_if_empty:
            self.set_banner(msg)
        return 0, msg

    def set_auto_claim(self, enabled: bool) -> str:
        self.ensure_background_threads()
        if enabled:
            self._require_worker_name()
            with self._lock:
                has_sheet = bool(self.config.spreadsheet_url.strip())
            if not has_sheet or not self.oauth.has_valid_refresh_token():
                raise ValueError('开启自动接单前，请先完成 Google 授权并填写表格链接。')
        with self._lock:
            self.auto_claim_enabled = bool(enabled)
            if not self.auto_claim_enabled:
                self.next_auto_poll_ts = 0.0
                msg = '已关闭自动接单模式（已在队列中的任务将继续完成）。'
                self.set_banner(msg)
                return msg
            # Trigger immediate first check in background scheduler without blocking UI
            self.next_auto_poll_ts = time.time()
        msg = f'已开启自动接单模式：每间隔 {AUTO_CLAIM_INTERVAL_MINUTES} 分钟巡检一次表格，本机活跃上限 2 条（1 制作中 + 1 排队中），饱和时不领新单。'
        self.set_banner(msg)
        return msg

    def trigger_auto_claim_once(self) -> str:
        """Run one saturation-aware auto-claim check right now."""
        with self._lock:
            if self._auto_claim_busy:
                return '自动接单巡检正在进行中，请稍候...'
            self._auto_claim_busy = True
            if self.auto_claim_enabled:
                self.next_auto_poll_ts = time.time() + AUTO_CLAIM_INTERVAL_SEC
            active = self.active_task_count()
            worker_name = self.config.worker_name.strip()
            has_sheet = bool(self.config.spreadsheet_url)

        try:
            if not worker_name:
                msg = '自动接单跳过：尚未填写自定义的“本机节点名称”，请先在上方配置区设置并保存。'
                self.log(msg)
                return msg
            if not has_sheet or not self.oauth.has_valid_refresh_token():
                msg = '自动接单跳过：尚未完成 Google 授权或未填写表格链接。'
                self.log(msg)
                return msg

            if active >= AUTO_MAX_ACTIVE_TASKS:
                msg = f'自动接单巡检：当前本机已有 {active} 条未完成任务（已达上限：1制作中+1排队），饱和跳过，不读取新任务。'
                self.log(msg)
                return msg

            slots = AUTO_MAX_ACTIVE_TASKS - active
            coord = self._make_coordinator()
            claimed_rows, conflicts = coord.claim_next_available(
                max_count=slots,
                worker_name=worker_name
            )
            added = self._enqueue_claimed_rows(claimed_rows, mode_label='自动接单')
            try:
                self.refresh_sheet_tasks()
            except Exception:
                pass

            if added:
                ids = '、'.join(t.task_id for t in added)
                msg = f'自动接单成功领取 {len(added)} 条新任务（{ids}），当前本机活跃任务 {self.active_task_count()}/{AUTO_MAX_ACTIVE_TASKS}。'
            else:
                msg = '自动接单巡检完成：当前表格暂无可领取的新需求。'
            if conflicts:
                msg += '（自动避让已领行：' + '；'.join(conflicts) + '）'
            self.set_banner(msg)
            return msg
        except Exception as exc:
            err_str = str(exc)
            if _is_fatal_auth_error(err_str):
                self._trip_circuit_breaker(f'自动接单检测到授权失效（{err_str}）', can_write_sheet=False)
            err = f'自动接单巡检异常：{err_str}'
            self.log(err)
            return err
        finally:
            with self._lock:
                self._auto_claim_busy = False

    def _auto_claim_loop(self) -> None:
        while not self._stop_threads:
            time.sleep(1.0)
            should_poll = False
            with self._lock:
                if self.auto_claim_enabled and self.next_auto_poll_ts > 0 and time.time() >= self.next_auto_poll_ts:
                    self.next_auto_poll_ts = time.time() + AUTO_CLAIM_INTERVAL_SEC
                    should_poll = True
            if should_poll:
                self.trigger_auto_claim_once()

    def abort_current_task(self, return_to_sheet: bool = True) -> str:
        with self._lock:
            task = self.current_task
            proc = self._current_proc
            if not task:
                return '当前没有正在制作中的任务。'
            task.abort_requested = True
            task.abort_return_to_sheet = return_to_sheet
        if proc is not None:
            terminate_process_tree(proc)
        action_desc = '中止并退回表格待领池' if return_to_sheet else '中止'
        msg = f'已请求{action_desc}当前任务 {task.task_id}...'
        self.set_banner(msg)
        return msg

    def _trip_circuit_breaker(self, reason: str, can_write_sheet: bool = True) -> str:
        """
        Stop auto-claim and drain unstarted queued tasks when a fatal account/quota/disk fault occurs,
        preventing wasted GPU hours and avoiding mass `制作失败` pollution on Google Sheets.
        """
        with self._lock:
            was_auto = self.auto_claim_enabled
            self.auto_claim_enabled = False
            self.next_auto_poll_ts = 0.0
            pending_items = list(self.queued_tasks)
            self.queued_tasks.clear()
            self.ui_state_version += 1

        returned_cnt = 0
        if pending_items:
            if can_write_sheet:
                try:
                    coord = self._make_coordinator()
                except Exception:
                    coord = None
                for item in pending_items:
                    if coord is not None:
                        try:
                            coord.release_queued_task(item.task_id)
                            self._sync_cached_row_local(item.task_id, clear_claim=True)
                            returned_cnt += 1
                        except Exception:
                            pass
                    item.local_status = LOCAL_STATUS_RETURNED
                    item.progress_detail = f'熔断保护已自动退回表格（{reason}）'
                    item.finished_at = datetime.datetime.now().strftime('%H:%M:%S')
                    with self._lock:
                        self.history_tasks.appendleft(item)
            else:
                for item in pending_items:
                    item.local_status = LOCAL_STATUS_PAUSED_AUTH
                    item.progress_detail = '授权失效已暂停队列，重新登录授权后点“恢复本机中断的制作中任务”即可继续'
                    item.finished_at = datetime.datetime.now().strftime('%H:%M:%S')
                    with self._lock:
                        self.history_tasks.appendleft(item)

        parts = [f'已触发环境故障保护（{reason}）']
        if was_auto:
            parts.append('已自动关闭自动接单')
        if pending_items:
            if can_write_sheet:
                parts.append(f'已将队列中未开始的 {returned_cnt}/{len(pending_items)} 条任务安全退回表格待领池')
            else:
                parts.append(f'已暂停本机未开始的 {len(pending_items)} 条排队任务（避免消耗算力，重授权后可一键恢复）')
        summary = '；'.join(parts) + '。'
        self.set_banner(summary)
        return summary

    def _check_preflight_resources(self) -> Optional[str]:
        """Verify sufficient local free disk space and backup cap before starting a heavy video task."""
        try:
            probe_dir = USERDATA_DIR if USERDATA_DIR.exists() else APP_ROOT
            free_bytes = shutil.disk_usage(probe_dir).free
            if free_bytes < MIN_FREE_DISK_BYTES:
                free_gb = free_bytes / (1024 ** 3)
                need_gb = MIN_FREE_DISK_BYTES / (1024 ** 3)
                return f'本机剩余磁盘空间不足（仅剩 {free_gb:.1f} GB，安全阈值 {need_gb:.1f} GB），请清理磁盘后再继续接单'
        except OSError:
            pass

        backup_cnt = len(self.saved_backup_files())
        if backup_cnt >= MAX_FAILED_BACKUP_FILES:
            return (
                f'本地待补传保底目录已积压 {backup_cnt} 个成品视频（达上限 {MAX_FAILED_BACKUP_FILES} 个），'
                f'请先排查网络/网盘空间并点击“补传本地保底成品”后再继续制作'
            )
        return None

    def release_queued_tasks(self, selected_task_ids: Optional[Sequence[str]] = None) -> str:
        """
        Return selected (or all, when `selected_task_ids` is empty/None) unstarted queued tasks
        back to Google Sheets so colleagues can claim them.
        """
        selected_set = {str(tid).strip() for tid in (selected_task_ids or []) if str(tid).strip()}
        with self._lock:
            if not self.queued_tasks:
                return '当前本机排队队列为空，没有需要退回的任务。'
            if selected_set:
                to_release = [t for t in self.queued_tasks if t.task_id in selected_set]
                if not to_release:
                    return '所选排队任务已开始制作或不在排队列表中。'
                remaining = [t for t in self.queued_tasks if t.task_id not in selected_set]
                self.queued_tasks = collections.deque(remaining)
            else:
                to_release = list(self.queued_tasks)
                self.queued_tasks.clear()

        coord = self._make_coordinator()
        released_count = 0
        errors: List[str] = []
        for item in to_release:
            try:
                coord.release_queued_task(item.task_id)
                self._sync_cached_row_local(item.task_id, clear_claim=True)
                item.local_status = LOCAL_STATUS_RETURNED
                item.progress_detail = '已退回表格待领池'
                item.finished_at = datetime.datetime.now().strftime('%H:%M:%S')
                with self._lock:
                    self.history_tasks.appendleft(item)
                    self.ui_state_version += 1
                released_count += 1
            except Exception as exc:
                errors.append(f'{item.task_id}: {exc}')

        if not self.cached_sheet_rows:
            try:
                self.refresh_sheet_tasks()
            except Exception:
                pass

        scope_text = '选中的' if selected_set else '全部未开始的'
        msg = f'已将 {released_count} 条{scope_text}排队任务退回表格（清空任务编号并恢复待领）。'
        if errors:
            msg += ' 部分回写异常：' + '；'.join(errors)
        self.set_banner(msg)
        return msg

    def release_all_queued_tasks(self) -> str:
        """Return all unstarted queued tasks back to Google Sheets so colleagues can claim them."""
        return self.release_queued_tasks(None)

    def _execution_loop(self) -> None:
        while not self._stop_threads:
            task: Optional[LocalTask] = None
            with self._lock:
                if self.queued_tasks:
                    task = self.queued_tasks.popleft()
                    self.current_task = task
                    self.ui_state_version += 1
            if task is None:
                self._wake_event.wait(timeout=1.5)
                self._wake_event.clear()
                continue

            self._run_single_task(task)
            with self._lock:
                self.current_task = None
                self._current_proc = None
                self.history_tasks.appendleft(task)
                self.ui_state_version += 1
            self.sweep_runtime_temp_dirs()

    @staticmethod
    def _acquire_gpu_for_task(task: LocalTask) -> None:
        """Acquire the shared GPU mutex politely, waiting if Manual Tab is currently running."""
        while True:
            if task.abort_requested:
                raise InterruptedError('任务已由用户手动中止。')
            if process_manager.is_pending() and GPU_EXECUTION_LOCK.acquire(timeout=0.5):
                return
            task.local_status = LOCAL_STATUS_WAITING_GPU
            task.progress_detail = '检测到手动换脸界面正在使用显卡，排队等待释放...'
            time.sleep(0.5)

    def recent_local_output_files(self) -> List[Path]:
        if not LOCAL_RECENT_OUTPUT_DIR.is_dir():
            return []
        files = [p for p in LOCAL_RECENT_OUTPUT_DIR.glob('*.mp4') if p.is_file() and p.stat().st_size > 0]
        files.sort(key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
        return files

    def open_local_outputs_folder(self) -> str:
        LOCAL_RECENT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        target_dir = LOCAL_RECENT_OUTPUT_DIR
        if not self.recent_local_output_files() and self.saved_backup_files():
            target_dir = FAILED_OUTPUT_BACKUP_DIR
        if os.name == 'nt' and hasattr(os, 'startfile'):
            os.startfile(str(target_dir))  # type: ignore[attr-defined]
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', str(target_dir)])
        else:
            subprocess.Popen(['xdg-open', str(target_dir)])
        msg = f'已在资源管理器中打开本地成品目录：{target_dir}（自动保留最近 {MAX_RECENT_LOCAL_OUTPUTS} 条成品）'
        self.log(msg)
        return msg

    def saved_backup_files(self) -> List[Path]:
        if not FAILED_OUTPUT_BACKUP_DIR.is_dir():
            return []
        return sorted(p for p in FAILED_OUTPUT_BACKUP_DIR.glob('*.mp4') if p.is_file() and p.stat().st_size > 0)

    def retry_saved_uploads(self) -> str:
        """Re-upload already-rendered MP4s from `sheet_saved_outputs/` without re-running GPU face swap."""
        backups = self.saved_backup_files()
        if not backups:
            msg = '本地保底目录中没有待补传的已生成成品视频。'
            self.set_banner(msg)
            return msg

        drive = GoogleDriveClient(self.oauth)
        coord = self._make_coordinator()
        with self._lock:
            folder_ref = self.config.drive_folder_url
            hist_by_id = {t.task_id: t for t in self.history_tasks}

        success_cnt = 0
        errors: List[str] = []
        for mp4_path in backups:
            id_match = TASK_ID_EXTRACT_RE.search(mp4_path.name)
            if not id_match:
                continue
            task_id = id_match.group(1)
            hist_task = hist_by_id.get(task_id)
            base_note = (hist_task.final_note_base if hist_task and hist_task.final_note_base else '本机保底成品补传完成')
            try:
                self.log(f'正在补传本地保底成品：{mp4_path.name} -> Google Drive...')
                share_link, perm_warn = drive.upload_and_share_detailed(
                    mp4_path,
                    mp4_path.name,
                    folder_url_or_id=folder_ref
                )
                note = f'{base_note} ({perm_warn})' if perm_warn else base_note
                updated_row = coord.complete_task(task_id, share_link, note)
                try:
                    save_and_prune_recent_output(mp4_path, mp4_path.name)
                except OSError:
                    pass
                mp4_path.unlink(missing_ok=True)
                self.invalidate_disk_counts_cache()
                self._sync_cached_row_local(task_id, status=SHEET_STATUS_COMPLETED, output_url=share_link, note=note)
                if hist_task:
                    hist_task.output_url = share_link
                    hist_task.note = note
                    hist_task.local_status = LOCAL_STATUS_COMPLETED
                    hist_task.progress_pct = 100.0
                    hist_task.progress_detail = f'补传成功并回写第 {updated_row} 行'
                    hist_task.local_backup_path = ''
                success_cnt += 1
                self.log(f'补传完成：{task_id} 已回写第 {updated_row} 行 -> {share_link}')
            except Exception as exc:
                errors.append(f'{task_id}: {exc}')

        self.invalidate_disk_counts_cache()
        if not self.cached_sheet_rows:
            try:
                self.refresh_sheet_tasks()
            except Exception:
                pass

        parts: List[str] = []
        if success_cnt:
            parts.append(f'已成功补传并回写 {success_cnt} 条保底成品视频（免重新换脸）！')
        if errors:
            parts.append('部分补传仍失败（文件继续保留在本机）：' + '；'.join(errors))
        if not parts:
            parts.append('未识别到带有效任务编号（FF-...）的待补传视频文件。')
        summary = ' '.join(parts)
        self.set_banner(summary)
        return summary

    def _run_single_task(self, task: LocalTask) -> None:
        task.started_mono = time.monotonic()

        preflight_block = self._check_preflight_resources()
        if preflight_block:
            task.finished_at = datetime.datetime.now().strftime('%H:%M:%S')
            task.local_status = LOCAL_STATUS_RETURNED
            task.progress_detail = f'启动前保护退回表格：{preflight_block}'
            try:
                coord_pre = self._make_coordinator()
                coord_pre.release_queued_task(task.task_id)
                self._sync_cached_row_local(task.task_id, clear_claim=True)
            except Exception as pre_exc:
                self.log(f'退回当前任务 {task.task_id} 时出错：{pre_exc}')
            self._trip_circuit_breaker(preflight_block, can_write_sheet=True)
            return

        work_dir = SHEET_TEMP_ROOT / task.task_id
        work_dir.mkdir(parents=True, exist_ok=True)

        output_file = work_dir / f'{task.task_id}_output.mp4'
        swap_completed = False
        upload_filename = f'成品_{task.task_id}.mp4'
        base_final_note = ''

        self.log(f'开始处理任务 {task.task_id}（预设：{task.preset_name}）')

        try:
            drive = GoogleDriveClient(self.oauth)
            coord = self._make_coordinator()

            # 1. Download source image & target video (CPU/network only; can overlap before GPU step)
            task.local_status = LOCAL_STATUS_DOWNLOADING
            task.progress_pct = 2.0
            task.progress_detail = '正在下载源人脸图片...'
            self.bump_ui_version()
            raw_source, _ = drive.download_media_with_name(task.source_url, work_dir, 'raw_source', '.jpg')
            if task.abort_requested:
                raise InterruptedError('任务已由用户手动中止。')

            def _on_vid_dl(done: int, total: int) -> None:
                if task.abort_requested:
                    raise InterruptedError('任务已由用户手动中止。')
                mb_done = done / (1024 * 1024)
                if total > 0:
                    pct = min(18.0, 4.0 + (done / total) * 14.0)
                    mb_total = total / (1024 * 1024)
                    task.progress_pct = round(pct, 1)
                    task.progress_detail = f'正在下载目标视频：{mb_done:.1f} / {mb_total:.1f} MB'
                else:
                    task.progress_detail = f'正在下载目标视频：{mb_done:.1f} MB'
                self.bump_ui_version()

            raw_video, orig_vid_name = drive.download_media_with_name(
                task.target_url, work_dir, 'raw_target', '.mp4', progress_cb=_on_vid_dl
            )
            vid_stem = sanitize_filename_stem(orig_vid_name, fallback='成品', max_len=42)
            upload_filename = f'{vid_stem}_{task.task_id}.mp4'
            task.upload_filename = upload_filename

            # 2 & 3. Acquire shared GPU lock for NVENC video normalization + FaceFusion headless swap
            self._acquire_gpu_for_task(task)
            try:
                task.local_status = LOCAL_STATUS_NORMALIZING
                task.progress_pct = 20.0
                task.progress_detail = '正在校验源图与检测视频编码格式...'
                self.bump_ui_version()
                clean_source, _ = prepare_source_image(raw_source, work_dir)

                def _on_norm_pct(pct: float) -> None:
                    task.progress_pct = round(20.0 + pct * 0.12, 1)
                    task.progress_detail = f'视频编码不规整，正在标准化重编码：{pct:.0f}%'
                    self.bump_ui_version()

                clean_video, was_reencoded, media_note = inspect_and_normalize_video(
                    raw_video,
                    work_dir,
                    progress_cb=_on_norm_pct,
                    should_abort=lambda: task.abort_requested
                )
                self.log(f'{task.task_id} 素材检查完成：{media_note}')

                task.local_status = LOCAL_STATUS_SWAPPING
                task.progress_pct = 33.0
                preset_label, preset_vals, preset_warn = resolve_preset_values_by_label(task.preset_name)
                if preset_warn:
                    self.log(f'{task.task_id}：{preset_warn}')
                task.progress_detail = f'正在启动换脸引擎（预设：{preset_label}）...'
                self.bump_ui_version()

                isolated_temp = work_dir / 'ff_temp'
                isolated_jobs = work_dir / 'ff_jobs'
                isolated_temp.mkdir(parents=True, exist_ok=True)
                isolated_jobs.mkdir(parents=True, exist_ok=True)

                self._execute_headless_swap(
                    task=task,
                    source_image=clean_source,
                    target_video=clean_video,
                    output_video=output_file,
                    temp_dir=isolated_temp,
                    jobs_dir=isolated_jobs,
                    preset_values=preset_vals
                )
                swap_completed = output_file.is_file() and output_file.stat().st_size > 0
            finally:
                GPU_EXECUTION_LOCK.release()

            elapsed_sec = int(time.monotonic() - task.started_mono)
            mins, secs = divmod(elapsed_sec, 60)
            elapsed_str = f'{mins}分{secs:02d}秒' if mins else f'{secs}秒'
            base_final_note = f'耗时 {elapsed_str} | 预设:{preset_label} | {"已转码" if was_reencoded else "免转码"}'
            if preset_warn:
                base_final_note += f' ({preset_warn})'
            task.final_note_base = base_final_note

            # 4. Upload finished MP4 to Google Drive, open public link sharing, write once to Sheet
            task.local_status = LOCAL_STATUS_UPLOADING
            task.progress_pct = 86.0
            task.progress_detail = f'正在上传成品视频（{upload_filename}）至 Google Drive...'
            self.bump_ui_version()

            def _on_upload(done: int, total: int) -> None:
                if task.abort_requested:
                    raise InterruptedError('任务已由用户手动中止。')
                ratio = (done / total) if total > 0 else 0.0
                task.progress_pct = round(86.0 + ratio * 12.0, 1)
                task.progress_detail = f'上传网盘：{done / (1024 * 1024):.1f} / {total / (1024 * 1024):.1f} MB ({ratio * 100:.0f}%)'
                self.bump_ui_version()

            with self._lock:
                folder_ref = self.config.drive_folder_url
            share_link, perm_warn = drive.upload_and_share_detailed(
                output_file, upload_filename, folder_url_or_id=folder_ref, progress_cb=_on_upload
            )
            final_note = f'{base_final_note} ({perm_warn})' if perm_warn else base_final_note

            task.progress_pct = 99.0
            task.progress_detail = '正在按任务编号定位表格行并回写成品链接...'
            self.bump_ui_version()
            updated_row = coord.complete_task(task.task_id, share_link, final_note)

            try:
                save_and_prune_recent_output(output_file, upload_filename)
                self.invalidate_disk_counts_cache()
            except OSError as keep_exc:
                self.log(f'保存本地近期成品副本时出现轻微异常（不影响网盘成品）：{keep_exc}')

            task.output_url = share_link
            task.note = final_note
            task.local_status = LOCAL_STATUS_COMPLETED
            task.progress_pct = 100.0
            task.progress_detail = f'已完成并回写第 {updated_row} 行（{upload_filename}）'
            task.finished_at = datetime.datetime.now().strftime('%H:%M:%S')
            self._sync_cached_row_local(task.task_id, status=SHEET_STATUS_COMPLETED, output_url=share_link, note=final_note)
            self.log(f'任务 {task.task_id} 全部完成！已回写第 {updated_row} 行 -> {share_link}')

        except (KeyboardInterrupt, SystemExit):
            self.log(f'程序正在退出，任务 {task.task_id} 保持表格“已领取制作中”状态，下次启动将自动恢复。')
            raise
        except InterruptedError as exc:
            task.finished_at = datetime.datetime.now().strftime('%H:%M:%S')
            if task.abort_return_to_sheet:
                task.local_status = LOCAL_STATUS_RETURNED
                task.progress_detail = '已手动中止并退回表格待领池'
                self.log(f'任务 {task.task_id} 已手动中止，正在退回表格待领池...')
                try:
                    coord = self._make_coordinator()
                    coord.release_queued_task(task.task_id)
                    self._sync_cached_row_local(task.task_id, clear_claim=True)
                    self.set_banner(f'任务 {task.task_id} 已中止并退回表格（清空任务编号恢复待领）。')
                except Exception as rel_exc:
                    self.log(f'退回中止任务 {task.task_id} 到表格时出错：{rel_exc}')
            else:
                err_msg = str(exc).strip() or '任务已手动中止'
                task.local_status = LOCAL_STATUS_FAILED
                task.progress_detail = f'已中止：{err_msg}'
                self.bump_ui_version()
        except Exception as exc:
            err_msg = str(exc).strip() or exc.__class__.__name__
            backup_hint = ''
            if swap_completed and output_file.is_file() and output_file.stat().st_size > 0:
                try:
                    FAILED_OUTPUT_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
                    saved_path = FAILED_OUTPUT_BACKUP_DIR / upload_filename
                    shutil.copy2(output_file, saved_path)
                    self.invalidate_disk_counts_cache()
                    task.local_backup_path = str(saved_path)
                    backup_hint = f'（成品已保底存至 {saved_path.name}，可点“补传保底成品”免重跑）'
                    self.log(f'任务 {task.task_id} 换脸已完成但上传/回写异常，成品已保底保存至：{saved_path}')
                except OSError as save_exc:
                    self.log(f'保底保存成品视频失败：{save_exc}')

            full_err = f'{err_msg}{backup_hint}'
            is_auth_fatal = _is_fatal_auth_error(err_msg)
            is_drive_fatal = _is_fatal_drive_or_quota_error(err_msg)

            if is_auth_fatal and not swap_completed:
                # Token expired before swap even ran: don't overwrite sheet row to 制作失败 (can't write anyway);
                # pause cleanly so re-authorization + 恢复本机中断任务 resumes it seamlessly.
                task.local_status = LOCAL_STATUS_PAUSED_AUTH
                task.note = full_err
                task.progress_detail = f'授权失效已暂停：{full_err}'
                task.finished_at = datetime.datetime.now().strftime('%H:%M:%S')
                self._trip_circuit_breaker('Google 授权令牌已失效，请重新登录授权', can_write_sheet=False)
            else:
                task.local_status = LOCAL_STATUS_FAILED
                task.note = full_err
                task.progress_detail = f'失败：{full_err}'
                task.finished_at = datetime.datetime.now().strftime('%H:%M:%S')
                self.log(f'任务 {task.task_id} 失败：{full_err}')
                if not is_auth_fatal:
                    try:
                        coord = self._make_coordinator()
                        coord.fail_task(task.task_id, f'失败：{full_err}')
                        self._sync_cached_row_local(task.task_id, status=SHEET_STATUS_FAILED, note=f'失败：{full_err}'[:300])
                    except Exception as sheet_exc:
                        self.log(f'回写任务 {task.task_id} 失败状态到表格时出错：{sheet_exc}')

                if is_auth_fatal:
                    self._trip_circuit_breaker('Google 授权令牌已失效，请重新登录授权后点击“补传保底成品”', can_write_sheet=False)
                elif is_drive_fatal:
                    self._trip_circuit_breaker('Google Drive 云盘空间已满或成品文件夹无写入权限', can_write_sheet=True)
        finally:
            _robust_rmtree(work_dir)

    def _execute_headless_swap(
        self,
        task: LocalTask,
        source_image: Path,
        target_video: Path,
        output_video: Path,
        temp_dir: Path,
        jobs_dir: Path,
        preset_values: Dict[str, Any]
    ) -> None:
        facefusion_py = APP_ROOT / 'facefusion.py'
        isolated_cfg = temp_dir / 'isolated_facefusion.ini'
        isolated_cfg.write_text('', encoding='utf-8')
        encoders_to_try = ['h264_nvenc', 'libx264']

        last_tail = ''
        for idx, encoder in enumerate(encoders_to_try):
            if output_video.exists():
                try:
                    output_video.unlink()
                except OSError:
                    pass

            cmd = build_headless_command(
                python_executable=sys.executable,
                facefusion_script=facefusion_py,
                source_image=source_image,
                target_video=target_video,
                output_video=output_video,
                temp_dir=temp_dir,
                jobs_dir=jobs_dir,
                preset_values=preset_values,
                video_encoder=encoder,
                config_path=isolated_cfg
            )
            env = os.environ.copy()
            env['PYTHONUNBUFFERED'] = '1'
            stage_state: Dict[str, Any] = {
                'saw_analysing': False,
                'saw_extracting': False,
                'saw_processing': False,
                'saw_merging': False,
                'swapped_frames': None
            }

            with subprocess.Popen(
                cmd,
                cwd=str(APP_ROOT),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0
            ) as proc:
                with self._lock:
                    self._current_proc = proc

                recent_lines: Deque[str] = collections.deque(maxlen=35)
                buffer = bytearray()
                chunk_queue: queue.Queue[Optional[bytes]] = queue.Queue()

                def _read_stdout_chunks() -> None:
                    try:
                        if proc.stdout is not None:
                            while True:
                                data = proc.stdout.read(256)
                                if not data:
                                    break
                                chunk_queue.put(data)
                    except Exception:
                        pass
                    finally:
                        chunk_queue.put(None)

                reader_thread = threading.Thread(target=_read_stdout_chunks, daemon=True)
                reader_thread.start()
                last_activity = time.monotonic()

                try:
                    while True:
                        if task.abort_requested:
                            terminate_process_tree(proc)
                            raise InterruptedError('任务已由用户手动中止。')
                        try:
                            chunk = chunk_queue.get(timeout=0.5)
                        except queue.Empty:
                            if (time.monotonic() - last_activity) > SWAP_IDLE_TIMEOUT_SEC:
                                terminate_process_tree(proc)
                                raise ValueError(
                                    f'FaceFusion 换脸子进程超时无响应（连续 {int(SWAP_IDLE_TIMEOUT_SEC)} 秒无输出），已自动强杀清理子进程树。'
                                )
                            continue

                        if chunk is None:
                            break
                        last_activity = time.monotonic()
                        buffer.extend(chunk)
                        parsed_any = False
                        while True:
                            pos_r = buffer.find(b'\r')
                            pos_n = buffer.find(b'\n')
                            cut = -1
                            if pos_r != -1 and pos_n != -1:
                                cut = min(pos_r, pos_n)
                            elif pos_r != -1:
                                cut = pos_r
                            elif pos_n != -1:
                                cut = pos_n
                            if cut == -1:
                                break
                            raw_line = bytes(buffer[:cut]).decode('utf-8', errors='replace').strip()
                            del buffer[:cut + 1]
                            if not raw_line:
                                continue
                            self._parse_headless_line(task, raw_line, recent_lines, stage_state)
                            parsed_any = True
                        if parsed_any:
                            self.bump_ui_version()

                    if buffer:
                        tail_line = bytes(buffer).decode('utf-8', errors='replace').strip()
                        if tail_line:
                            self._parse_headless_line(task, tail_line, recent_lines, stage_state)
                            self.bump_ui_version()

                    ret = proc.wait(timeout=30)
                    if ret == 0 and output_video.is_file() and output_video.stat().st_size > 0:
                        if stage_state.get('swapped_frames') == 0:
                            output_video.unlink(missing_ok=True)
                            raise ValueError('目标视频全程未检测到可替换的人脸（0 帧生效），已拦截上传避免交付未换脸原片，请检查目标视频画面。')
                        return
                finally:
                    terminate_process_tree(proc)

            joined_recent = '\n'.join(recent_lines)
            lower_recent = joined_recent.lower()
            if 'no source face detected' in lower_recent:
                raise ValueError('源人脸图片中未检测到清晰正脸，请更换包含清晰正脸的源图片。')
            if 'content_analyser_blocked' in joined_recent or (
                stage_state['saw_analysing']
                and not stage_state['saw_extracting']
                and not stage_state['saw_processing']
            ):
                raise ValueError('目标视频未通过引擎画面合规检测（可能包含大面积露肤或敏感特写拦截），未能进入换脸阶段。')
            if 'match the target and output extension' in lower_recent:
                raise ValueError('目标视频与输出文件扩展名不一致。')

            last_tail = ' | '.join(list(recent_lines)[-4:])
            # Only retry software libx264 if NVENC failed during the video merging/encoding stage!
            if idx == 0 and len(encoders_to_try) > 1 and stage_state['saw_merging']:
                self.log(f'{task.task_id} 使用 {encoder} 合成导出未成功，自动回退 libx264 重试...')
                continue
            break

        raise ValueError(f'FaceFusion 换脸子进程执行失败：{last_tail or "未生成有效成品视频（请检查源图是否包含清晰人脸）"}')

    @staticmethod
    def _parse_headless_line(
        task: LocalTask,
        line: str,
        recent_lines: Deque[str],
        stage_state: Optional[Dict[str, Any]] = None
    ) -> None:
        cleaned = re.sub(r'\x1b\[[0-9;]*m', '', line).strip()
        if not cleaned:
            return

        swapped_m = SWAPPED_TOTAL_RE.search(cleaned)
        if swapped_m and stage_state is not None:
            stage_state['swapped_frames'] = int(swapped_m.group(1))

        match = TQDM_PROGRESS_RE.search(cleaned)
        if match:
            stage_raw = (match.group(1) or 'processing').strip().lower()
            pct = int(match.group(2))
            frames = match.group(3)
            rate_info = match.group(4).strip()
            if stage_raw == 'analysing':
                if stage_state is not None:
                    stage_state['saw_analysing'] = True
                task.progress_pct = round(33.0 + (pct / 100.0) * 3.0, 1)
                task.progress_detail = f'画面预检：{pct}% ({frames})'
            elif stage_raw == 'extracting':
                if stage_state is not None:
                    stage_state['saw_extracting'] = True
                task.progress_pct = round(36.0 + (pct / 100.0) * 5.0, 1)
                task.progress_detail = f'解包视频帧：{pct}% ({frames})'
            elif stage_raw == 'merging':
                if stage_state is not None:
                    stage_state['saw_merging'] = True
                task.progress_pct = round(83.0 + (pct / 100.0) * 2.0, 1)
                task.progress_detail = f'合成输出视频：{pct}% ({frames})'
            else:
                if stage_state is not None:
                    stage_state['saw_processing'] = True
                task.progress_pct = round(41.0 + (pct / 100.0) * 42.0, 1)
                task.progress_detail = f'换脸推理：{pct}% ({frames} 帧 | {rate_info})'
            return

        recent_lines.append(cleaned)
        lower = cleaned.lower()
        if 'extracting frames' in lower or '提取帧' in cleaned:
            if stage_state is not None:
                stage_state['saw_extracting'] = True
            task.progress_pct = max(task.progress_pct, 36.0)
            task.progress_detail = '正在解包视频帧...'
        elif 'merging video' in lower or '合并视频' in cleaned:
            if stage_state is not None:
                stage_state['saw_merging'] = True
            task.progress_pct = max(task.progress_pct, 83.0)
            task.progress_detail = '正在合成最终音视频...'
        elif 'restoring audio' in lower or 'replacing audio' in lower:
            task.progress_pct = max(task.progress_pct, 85.0)
            task.progress_detail = '正在封装音轨...'


WORKER_MANAGER = SheetWorkerManager()
