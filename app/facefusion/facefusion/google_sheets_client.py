"""Google OAuth 2.0, Sheets v4 anti-conflict coordinator, and Drive v3 uploader/downloader."""
from __future__ import annotations

import base64
import datetime
import hashlib
import html
import http.server
import json
import mimetypes
import os
import random
import re
import secrets
import string
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import httpx

from facefusion.userdata_paths import get_userdata_dir

OAUTH_AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
OAUTH_TOKEN_URL = 'https://oauth2.googleapis.com/token'
SHEETS_API_BASE = 'https://sheets.googleapis.com/v4/spreadsheets'
DRIVE_API_BASE = 'https://www.googleapis.com/drive/v3'
DRIVE_UPLOAD_BASE = 'https://www.googleapis.com/upload/drive/v3'

GOOGLE_SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive'
]

DEFAULT_TOKEN_PATH = get_userdata_dir() / 'facefusion-google-token.json'

# Sheet statuses kept intentionally coarse on Google Sheets to avoid API waste.
SHEET_STATUS_IN_PROGRESS = '已领取制作中'
SHEET_STATUS_COMPLETED = '已完成'
SHEET_STATUS_FAILED = '制作失败'
SHEET_STATUS_PENDING = '待处理'

CLAIMABLE_STATUS_SET = {
    '',
    '待处理',
    '待领取',
    '待制作',
    '未领取',
    '排队待领',
    'pending',
    'todo',
    'ready'
}

HEADER_ALIASES: Dict[str, List[str]] = {
    'source_image': ['源人脸图片', '源图片', '源图链接', '人脸图片', '人脸链接', '图片链接', '源人脸', 'source', 'source_image'],
    'target_video': ['目标视频', '视频链接', '原视频', '原片链接', '口播视频', '目标视频链接', 'target', 'target_video'],
    'preset': ['场景预设', '参数预设', '预设', '换脸预设', '场景参数预设', 'preset'],
    'task_id': ['任务编号', '任务id', '任务ID', 'task_id'],
    'status': ['任务状态', '制作状态', '状态', 'status'],
    'worker': ['领取节点', '领取人', '接单节点', '处理节点', '制作节点', '接单人', 'worker'],
    'output_url': ['成品链接', '成片链接', '结果链接', '输出链接', '成品网盘链接', 'output_url'],
    'note': ['处理备注', '执行备注', '系统备注', '制作备注']
}

DEFAULT_SYSTEM_HEADERS: Dict[str, str] = {
    'task_id': '任务编号',
    'status': '任务状态',
    'worker': '领取节点',
    'output_url': '成品链接',
    'note': '处理备注'
}


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix='.' + path.stem + '-', suffix='.tmp', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def normalize_header_text(value: Any) -> str:
    if value is None:
        return ''
    text = str(value).replace('\ufeff', '').replace('\u200b', '').strip()
    return re.sub(r'\s+', '', text)


def col_index_to_letter(index: int) -> str:
    """Convert 0-based column index to A1 column letters (0 -> A, 25 -> Z, 26 -> AA)."""
    if index < 0:
        raise ValueError(f'Invalid column index: {index}')
    letters: List[str] = []
    current = index + 1
    while current > 0:
        current, remainder = divmod(current - 1, 26)
        letters.append(chr(ord('A') + remainder))
    return ''.join(reversed(letters))


def quote_sheet_title(title: str) -> str:
    escaped = title.replace("'", "''")
    return f"'{escaped}'"


def is_probably_url(value: str) -> bool:
    text = (value or '').strip()
    return text.startswith('http://') or text.startswith('https://') or 'drive.google.com' in text


def parse_spreadsheet_ref(url_or_id: str) -> Tuple[str, Optional[int]]:
    """Extract (spreadsheet_id, optional_gid) from a Google Sheets URL or raw ID."""
    raw = (url_or_id or '').strip()
    if not raw:
        raise ValueError('请先填写 Google 表格链接或表格 ID。')
    match = re.search(r'/spreadsheets/d/([a-zA-Z0-9-_]+)', raw)
    spreadsheet_id = match.group(1) if match else raw
    if not re.fullmatch(r'[a-zA-Z0-9-_]{15,}', spreadsheet_id):
        raise ValueError('无法识别有效的 Google 表格链接或 ID，请检查后重试。')
    gid_match = re.search(r'[#?&]gid=(\d+)', raw)
    gid = int(gid_match.group(1)) if gid_match else None
    return spreadsheet_id, gid


def parse_drive_id(url_or_id: str) -> Optional[str]:
    """Extract Google Drive file/folder ID from common share links or raw ID."""
    raw = (url_or_id or '').strip()
    if not raw:
        return None
    patterns = [
        r'/file/d/([a-zA-Z0-9_-]{15,})',
        r'/folders/([a-zA-Z0-9_-]{15,})',
        r'[?&]id=([a-zA-Z0-9_-]{15,})'
    ]
    for pattern in patterns:
        match = re.search(pattern, raw)
        if match:
            return match.group(1)
    if not raw.startswith(('http://', 'https://')) and re.fullmatch(r'[a-zA-Z0-9_-]{15,}', raw):
        return raw
    return None


def generate_task_id(now: Optional[datetime.datetime] = None) -> str:
    ts = (now or datetime.datetime.now()).strftime('%m%d-%H%M%S')
    suffix = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(4))
    return f'FF-{ts}-{suffix}'


def sanitize_filename_stem(raw_name: str, fallback: str = 'video', max_len: int = 48) -> str:
    """Extract a clean, human-readable filename stem without path or illegal characters."""
    base = Path((raw_name or '').strip()).stem if raw_name else ''
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', '_', base)
    base = re.sub(r'\s+', '_', base).strip('._ ')
    if not base or base.lower() in {'download', 'uc', 'view', 'edit', 'raw_target'}:
        return fallback
    return base[:max_len].rstrip('._ ') or fallback


def _normalize_proxy_url(proxy_val: str) -> Optional[str]:
    clean = (proxy_val or '').strip()
    if not clean:
        return None
    if '://' not in clean:
        clean = f'http://{clean}'
    # Standard portable bundle does not ship socksio; only mount HTTP(S) proxy schemes automatically.
    if clean.lower().startswith(('http://', 'https://')):
        return clean
    return None


def _make_http_client(
    timeout: Any = 30.0,
    follow_redirects: bool = False
) -> httpx.Client:
    """
    Create an httpx.Client that:
    1) Respects HTTP_PROXY / HTTPS_PROXY env vars when set, while keeping 127.0.0.1/localhost direct.
    2) Falls back to OS System Proxy (via urllib.request.getproxies()) when env vars are unset.
    """
    env_keys = ('HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY', 'https_proxy', 'http_proxy', 'all_proxy')
    has_env_proxy = any(os.environ.get(k, '').strip() for k in env_keys)
    mounts: Dict[str, httpx.BaseTransport] = {
        'http://127.0.0.1': httpx.HTTPTransport(),
        'http://localhost': httpx.HTTPTransport()
    }
    if not has_env_proxy:
        try:
            sys_proxies = urllib.request.getproxies() or {}
        except Exception:
            sys_proxies = {}
        http_p = _normalize_proxy_url(sys_proxies.get('http', '') or sys_proxies.get('https', ''))
        https_p = _normalize_proxy_url(sys_proxies.get('https', '') or sys_proxies.get('http', ''))
        if http_p:
            mounts['http://'] = httpx.HTTPTransport(proxy=http_p)
        if https_p:
            mounts['https://'] = httpx.HTTPTransport(proxy=https_p)
    return httpx.Client(timeout=timeout, follow_redirects=follow_redirects, mounts=mounts)


@dataclass
class SheetTaskRow:
    row_number: int  # 1-based sheet row number at snapshot time
    source_url: str
    target_url: str
    preset_name: str
    task_id: str
    status: str
    worker: str
    output_url: str
    note: str

    @property
    def is_claimable(self) -> bool:
        if not is_probably_url(self.source_url) or not is_probably_url(self.target_url):
            return False
        if self.task_id.strip():
            return False
        if self.output_url.strip():
            return False
        normalized_status = self.status.strip().lower()
        return normalized_status in CLAIMABLE_STATUS_SET

    @property
    def is_failed_retryable(self) -> bool:
        if not is_probably_url(self.source_url) or not is_probably_url(self.target_url):
            return False
        if self.output_url.strip():
            return False
        return self.status.strip() == SHEET_STATUS_FAILED


class _OAuthLoopbackServer(http.server.ThreadingHTTPServer):
    """Multi-threaded loopback server so idle Chromium speculative-preconnect sockets never block OAuth."""
    daemon_threads = True
    allow_reuse_address = True


@dataclass
class PendingOAuthSession:
    client_id: str
    client_secret: str
    token_uri: str
    redirect_uri: str
    code_verifier: str
    expected_state: str
    authorization_url: str
    created_mono: float
    done_event: threading.Event
    lock: threading.Lock
    server: Optional[http.server.HTTPServer] = None
    completed: bool = False
    cancelled: bool = False
    result_payload: Optional[Dict[str, Any]] = None
    error_message: str = ''

    def cancel(self) -> None:
        self.cancelled = True
        self.done_event.set()
        srv = self.server
        if srv is not None:
            try:
                srv.server_close()
            except Exception:
                pass


class GoogleOAuthManager:
    """Manages OAuth 2.0 client credentials, interactive/non-blocking browser login, and token refresh."""

    def __init__(self, token_path: Path = DEFAULT_TOKEN_PATH) -> None:
        self.token_path = Path(token_path)
        self._lock = threading.RLock()

    @staticmethod
    def load_client_config(client_secret_path: Path) -> Dict[str, Any]:
        path = Path(client_secret_path)
        if not path.is_file():
            raise ValueError(f'找不到客户端授权文件：{path}')
        try:
            raw = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise ValueError('客户端授权文件不是有效的 JSON 格式。') from exc

        # Case 1: User directly provided an authorized_user token JSON.
        if isinstance(raw, dict) and raw.get('refresh_token') and raw.get('client_id') and raw.get('client_secret'):
            return {'mode': 'authorized_user', 'data': raw}

        # Case 2: Standard OAuth 2.0 Client ID JSON downloaded from Google Cloud Console.
        block = None
        if isinstance(raw, dict):
            if raw.get('installed'):
                block = raw.get('installed')
            elif raw.get('web'):
                raise ValueError(
                    '当前上传的是“Web 应用（web）”类型的 OAuth 客户端文件，不支持本地随机端口登录回调（会报 redirect_uri_mismatch）。'
                    '请在 Google Cloud Console 创建并下载“桌面应用（Desktop app）”类型的客户端 JSON。'
                )
        if not isinstance(block, dict) or not block.get('client_id') or not block.get('client_secret'):
            if isinstance(raw, dict) and raw.get('type') == 'service_account':
                raise ValueError('当前检测到的是服务账号文件（service_account），请提供 OAuth 2.0 客户端授权文件（桌面应用 client_secret.json）。')
            raise ValueError('无法识别该客户端授权文件，请从 Google Cloud Console 下载“桌面应用（Desktop app）”类型的 OAuth 客户端 JSON。')
        return {'mode': 'client_secret', 'data': block}

    def import_token_or_client(self, file_path: Path) -> str:
        """If file is already an authorized_user token, store it directly; otherwise return mode."""
        parsed = self.load_client_config(file_path)
        if parsed['mode'] == 'authorized_user':
            data = parsed['data']
            token_payload = {
                'client_id': data['client_id'],
                'client_secret': data['client_secret'],
                'refresh_token': data['refresh_token'],
                'access_token': data.get('token') or data.get('access_token', ''),
                'token_uri': data.get('token_uri', OAUTH_TOKEN_URL),
                'expires_at': 0.0,
                'authorized_email': data.get('authorized_email', '')
            }
            with self._lock:
                _atomic_write_json(self.token_path, token_payload)
            return 'imported_token'
        return 'client_secret'

    def clear_token(self) -> None:
        with self._lock:
            if self.token_path.exists():
                try:
                    self.token_path.unlink()
                except OSError:
                    pass

    def has_valid_refresh_token(self) -> bool:
        with self._lock:
            if not self.token_path.is_file():
                return False
            try:
                data = json.loads(self.token_path.read_text(encoding='utf-8'))
                return bool(data.get('refresh_token') and data.get('client_id') and data.get('client_secret'))
            except (OSError, ValueError):
                return False

    def read_token_data(self) -> Dict[str, Any]:
        with self._lock:
            if not self.token_path.is_file():
                raise ValueError('尚未完成 Google 账号授权，请先上传客户端授权文件并点击“打开浏览器登录授权”。')
            try:
                data = json.loads(self.token_path.read_text(encoding='utf-8'))
            except (OSError, ValueError) as exc:
                raise ValueError('本地 Google 授权凭证文件损坏，请重新授权。') from exc
            if not data.get('refresh_token') or not data.get('client_id') or not data.get('client_secret'):
                raise ValueError('Google 授权凭证缺少 refresh_token，请重新点击浏览器授权。')
            return data

    def _exchange_and_store_code(
        self,
        *,
        auth_code: str,
        client_id: str,
        client_secret: str,
        token_uri: str,
        redirect_uri: str,
        code_verifier: str
    ) -> Dict[str, Any]:
        last_net_err: Optional[Exception] = None
        response: Optional[httpx.Response] = None
        for attempt in range(2):
            try:
                with _make_http_client(timeout=25.0) as client:
                    response = client.post(
                        token_uri,
                        data={
                            'code': auth_code,
                            'client_id': client_id,
                            'client_secret': client_secret,
                            'redirect_uri': redirect_uri,
                            'grant_type': 'authorization_code',
                            'code_verifier': code_verifier
                        }
                    )
                break
            except httpx.HTTPError as exc:
                last_net_err = exc
                if attempt == 0:
                    time.sleep(0.8)
                    continue
                raise ValueError(f'连接 Google 授权服务器失败，请检查代理或网络设置：{exc}') from exc

        assert response is not None
        if response.status_code != 200:
            raise ValueError(f'换取 Google Token 失败（HTTP {response.status_code}）：{response.text[:300]}')

        token_json = response.json()
        refresh_token = token_json.get('refresh_token')
        if not refresh_token:
            if self.has_valid_refresh_token():
                refresh_token = self.read_token_data().get('refresh_token')
        if not refresh_token:
            raise ValueError('Google 未返回 refresh_token，请在 Google 账号权限页移除旧授权后重新点击登录。')

        payload: Dict[str, Any] = {
            'client_id': client_id,
            'client_secret': client_secret,
            'refresh_token': refresh_token,
            'access_token': token_json['access_token'],
            'token_uri': token_uri,
            'expires_at': time.time() + float(token_json.get('expires_in', 3600)),
            'authorized_email': ''
        }
        with self._lock:
            _atomic_write_json(self.token_path, payload)

        email = self.fetch_account_email()
        if email:
            with self._lock:
                payload['authorized_email'] = email
                _atomic_write_json(self.token_path, payload)
        return payload

    def start_auth_session(
        self,
        client_secret_path: Path,
        timeout_sec: int = 300,
        on_complete: Optional[Callable[[Optional[Dict[str, Any]], Optional[str]], None]] = None
    ) -> PendingOAuthSession:
        """
        Start a non-blocking multi-threaded loopback OAuth 2.0 + PKCE session.
        Returns immediately with `PendingOAuthSession` (including `authorization_url`), while a daemon
        thread listens on `127.0.0.1:{random_port}` and invokes `on_complete(payload, error_msg)` once finished.
        """
        parsed = self.load_client_config(client_secret_path)
        if parsed['mode'] != 'client_secret':
            raise ValueError('当前文件已经是授权 Token，无需启动网页登录会话。')

        client_info = parsed['data']
        client_id = client_info['client_id']
        client_secret = client_info['client_secret']
        auth_uri = client_info.get('auth_uri', OAUTH_AUTH_URL)
        token_uri = client_info.get('token_uri', OAUTH_TOKEN_URL)

        code_verifier = secrets.token_urlsafe(64)
        digest = hashlib.sha256(code_verifier.encode('ascii')).digest()
        code_challenge = base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')
        expected_state = secrets.token_urlsafe(24)

        done_event = threading.Event()
        session_lock = threading.Lock()
        oauth_mgr = self
        session_ref: Dict[str, PendingOAuthSession] = {}

        class _CallbackHandler(http.server.BaseHTTPRequestHandler):
            timeout = 5.0

            def do_GET(self) -> None:  # noqa: N802
                sess = session_ref.get('session')
                parsed_url = urllib.parse.urlparse(self.path)
                params = urllib.parse.parse_qs(parsed_url.query)
                state = (params.get('state') or [''])[0]
                error = (params.get('error') or [''])[0]
                code = (params.get('code') or [''])[0]

                if not state and not error and not code:
                    self.send_response(204)
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    return

                if sess is None or sess.cancelled:
                    body = '<h2>当前授权会话已关闭，请返回 FaceFusion 重新点击“登录 Google”。</h2>'
                    self._write_html(body)
                    return

                notify_payload: Optional[Dict[str, Any]] = None
                notify_err: Optional[str] = None

                if state != expected_state:
                    notify_err = 'OAuth 状态校验不匹配（可能点击了旧的登录页面），请返回软件重新点击“登录 Google”。'
                    body = '<h2>⚠️ 授权状态校验未通过</h2><p>可能打开了过期的登录链接，请关闭此页并返回 FaceFusion 重新点击“登录 Google”。</p>'
                elif error:
                    notify_err = f'Google 授权被拒绝或取消：{error}'
                    body = f'<h2>⚠️ 授权未完成：{html.escape(error)}</h2><p>请关闭此页面返回 FaceFusion 重试。</p>'
                else:
                    try:
                        with sess.lock:
                            if sess.completed and sess.result_payload is not None:
                                notify_payload = sess.result_payload
                            else:
                                notify_payload = oauth_mgr._exchange_and_store_code(
                                    auth_code=code,
                                    client_id=client_id,
                                    client_secret=client_secret,
                                    token_uri=token_uri,
                                    redirect_uri=sess.redirect_uri,
                                    code_verifier=code_verifier
                                )
                                sess.completed = True
                                sess.result_payload = notify_payload
                                sess.error_message = ''
                        acct = (notify_payload or {}).get('authorized_email') or '已绑定 Google 账号'
                        body = (
                            f'<h2 style="color:#16a34a;">✅ Google 账号授权成功！</h2>'
                            f'<p style="font-size:16px;color:#334155;">已授权账号：<b>{html.escape(str(acct))}</b></p>'
                            f'<p style="color:#64748b;">软件正在自动保存凭证并连接云表格，您现在可以直接关闭此网页标签返回 FaceFusion。</p>'
                        )
                    except Exception as exc:
                        notify_err = str(exc)
                        body = (
                            f'<h2 style="color:#dc2626;">⚠️ 换取 Google 令牌失败</h2>'
                            f'<p style="color:#334155;">{html.escape(notify_err)}</p>'
                            f'<p style="color:#64748b;">请检查网络连接后返回 FaceFusion 重试。</p>'
                        )

                self._write_html(body)

                if notify_err and not sess.completed:
                    sess.error_message = notify_err
                sess.done_event.set()

                if on_complete is not None:
                    try:
                        on_complete(notify_payload, notify_err)
                    except Exception:
                        pass

            def _write_html(self, body: str) -> None:
                doc = (
                    '<!doctype html><html><head><meta charset="utf-8"><title>FaceFusion Google OAuth</title></head>'
                    '<body style="font-family:system-ui,-apple-system,\'Segoe UI\',sans-serif;'
                    'padding:48px 24px;text-align:center;background:#f8fafc;">'
                    '<div style="max-width:520px;margin:0 auto;background:#fff;padding:32px;border-radius:12px;'
                    f'border:1px solid #e2e8f0;box-shadow:0 4px 12px rgba(15,23,42,0.06);">{body}</div>'
                    '</body></html>'
                )
                encoded = doc.encode('utf-8')
                try:
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.send_header('Content-Length', str(len(encoded)))
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    self.wfile.write(encoded)
                except OSError:
                    pass

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

            def log_error(self, format: str, *args: Any) -> None:  # noqa: A002
                return

        server = _OAuthLoopbackServer(('127.0.0.1', 0), _CallbackHandler)
        server.timeout = 1.0
        port = server.server_address[1]
        redirect_uri = f'http://127.0.0.1:{port}'

        query = urllib.parse.urlencode({
            'client_id': client_id,
            'redirect_uri': redirect_uri,
            'response_type': 'code',
            'scope': ' '.join(GOOGLE_SCOPES),
            'access_type': 'offline',
            'prompt': 'consent',
            'state': expected_state,
            'code_challenge': code_challenge,
            'code_challenge_method': 'S256'
        })
        authorization_url = f'{auth_uri}?{query}'

        session = PendingOAuthSession(
            client_id=client_id,
            client_secret=client_secret,
            token_uri=token_uri,
            redirect_uri=redirect_uri,
            code_verifier=code_verifier,
            expected_state=expected_state,
            authorization_url=authorization_url,
            created_mono=time.monotonic(),
            done_event=done_event,
            lock=session_lock,
            server=server
        )
        session_ref['session'] = session

        def _serve_loop() -> None:
            deadline = time.monotonic() + timeout_sec
            try:
                while not done_event.is_set() and time.monotonic() < deadline:
                    try:
                        server.handle_request()
                    except OSError:
                        break
            finally:
                try:
                    server.server_close()
                except Exception:
                    pass
                if not session.completed and not session.cancelled and not session.error_message:
                    session.error_message = '等待浏览器确认授权已超时（5 分钟），请重新点击“🔑 登录 Google”并在打开的网页中点击允许。'
                    done_event.set()
                    if on_complete is not None:
                        try:
                            on_complete(None, session.error_message)
                        except Exception:
                            pass

        threading.Thread(target=_serve_loop, name='GoogleOAuthLoopback', daemon=True).start()
        return session

    def complete_auth_session_manually(
        self,
        session: PendingOAuthSession,
        raw_callback_or_code: str
    ) -> Dict[str, Any]:
        """
        Complete an active `PendingOAuthSession` when a browser proxy extension intercepted `127.0.0.1`
        and the user pasted the redirected browser address-bar URL (or raw `code`) manually.
        """
        clean = (raw_callback_or_code or '').strip().strip('"').strip("'")
        if not clean:
            raise ValueError('请先粘贴授权后浏览器地址栏显示的完整网址（以 http://127.0.0.1 开头）或 code 授权码。')

        auth_code = ''
        if 'code=' in clean or 'error=' in clean or clean.startswith(('http://', 'https://', '?')):
            query_str = urllib.parse.urlparse(clean).query if '://' in clean else clean.lstrip('?')
            params = urllib.parse.parse_qs(query_str)
            err = (params.get('error') or [''])[0]
            if err:
                raise ValueError(f'该回调链接显示授权未通过（{err}），请重新点击“登录 Google”并点击允许。')
            state = (params.get('state') or [''])[0]
            if state and state != session.expected_state:
                raise ValueError('粘贴的回调网址不属于当前最新一次登录请求，请复制最新网页地址栏的网址，或重新点击“登录 Google”。')
            auth_code = (params.get('code') or [''])[0].strip()
        else:
            auth_code = clean

        if len(auth_code) < 8 or any(ch.isspace() for ch in auth_code):
            raise ValueError('未能从输入内容中识别出有效的授权码（code），请复制点击“允许”后浏览器地址栏的整串网址粘贴。')

        with session.lock:
            if session.completed and session.result_payload is not None:
                return session.result_payload
            payload = self._exchange_and_store_code(
                auth_code=auth_code,
                client_id=session.client_id,
                client_secret=session.client_secret,
                token_uri=session.token_uri,
                redirect_uri=session.redirect_uri,
                code_verifier=session.code_verifier
            )
            session.completed = True
            session.result_payload = payload
            session.error_message = ''
            session.done_event.set()
            try:
                if session.server is not None:
                    session.server.server_close()
            except Exception:
                pass
            return payload

    def authorize_interactive(
        self,
        client_secret_path: Path,
        timeout_sec: int = 180,
        open_browser: bool = True,
        url_callback: Optional[Callable[[str], None]] = None
    ) -> Dict[str, Any]:
        """Run loopback OAuth 2.0 flow with PKCE and persist refresh_token (blocking convenience wrapper)."""
        parsed = self.load_client_config(client_secret_path)
        if parsed['mode'] == 'authorized_user':
            self.import_token_or_client(client_secret_path)
            self.get_access_token(force_refresh=True)
            email = self.fetch_account_email()
            return self.read_token_data() | {'authorized_email': email}

        session = self.start_auth_session(client_secret_path, timeout_sec=timeout_sec)
        if url_callback:
            url_callback(session.authorization_url)
        if open_browser:
            try:
                webbrowser.open(session.authorization_url)
            except Exception:
                pass

        finished = session.done_event.wait(timeout=timeout_sec)
        if not finished:
            session.cancel()
            raise ValueError(f'等待浏览器登录授权超时（{timeout_sec} 秒），请重试并在弹出的网页完成确认。')
        if session.completed and session.result_payload is not None:
            return session.result_payload
        if session.error_message:
            raise ValueError(session.error_message)
        raise ValueError('未获取到授权码，请重试。')

    def get_access_token(self, force_refresh: bool = False) -> str:
        with self._lock:
            data = self.read_token_data()
            now = time.time()
            if not force_refresh and data.get('access_token') and float(data.get('expires_at', 0)) - now > 120:
                return str(data['access_token'])

            token_uri = data.get('token_uri', OAUTH_TOKEN_URL)
            response: Optional[httpx.Response] = None
            for attempt in range(2):
                try:
                    with _make_http_client(timeout=25.0) as client:
                        response = client.post(
                            token_uri,
                            data={
                                'client_id': data['client_id'],
                                'client_secret': data['client_secret'],
                                'refresh_token': data['refresh_token'],
                                'grant_type': 'refresh_token'
                            }
                        )
                    break
                except httpx.HTTPError as exc:
                    if attempt == 0:
                        time.sleep(0.8)
                        continue
                    raise ValueError(f'连接 Google 刷新访问令牌失败，请检查代理或网络：{exc}') from exc

            assert response is not None
            if response.status_code != 200:
                body_preview = response.text[:240]
                if 'invalid_grant' in response.text:
                    raise ValueError(
                        'Google 授权令牌已过期或被撤销（invalid_grant）。'
                        '提示：若您的 Google Cloud OAuth 同意屏幕处于“测试版（Testing）”，令牌满 7 天会自动过期，'
                        '建议在 Google Cloud 将其设为“发布应用（In production）”，并在当前界面重新点击“🔑 登录 Google”。'
                    )
                raise ValueError(f'刷新 Google 访问令牌失败（HTTP {response.status_code}），可能需要重新授权：{body_preview}')
            refreshed = response.json()
            data['access_token'] = refreshed['access_token']
            data['expires_at'] = time.time() + float(refreshed.get('expires_in', 3600))
            if refreshed.get('refresh_token'):
                data['refresh_token'] = refreshed['refresh_token']
            _atomic_write_json(self.token_path, data)
            return str(data['access_token'])

    def fetch_account_email(self) -> str:
        try:
            token = self.get_access_token()
            with _make_http_client(timeout=20.0) as client:
                resp = client.get(
                    f'{DRIVE_API_BASE}/about',
                    params={'fields': 'user(displayName,emailAddress)'},
                    headers={'Authorization': f'Bearer {token}'}
                )
            if resp.status_code == 200:
                user = resp.json().get('user', {})
                email = user.get('emailAddress', '')
                name = user.get('displayName', '')
                if email:
                    with self._lock:
                        if self.token_path.is_file():
                            current = json.loads(self.token_path.read_text(encoding='utf-8'))
                            current['authorized_email'] = email
                            _atomic_write_json(self.token_path, current)
                return f'{name} <{email}>'.strip() if name else email
        except Exception:
            pass
        return ''


class _GoogleAuthedHttp:
    """Helper executing Google REST calls with automatic 401 token refresh & transient 429/5xx retries."""

    def __init__(self, oauth: GoogleOAuthManager) -> None:
        self.oauth = oauth

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
    ) -> httpx.Response:
        refreshed_once = False
        last_error: Optional[Exception] = None
        for attempt in range(max_retries + 1):
            token = self.oauth.get_access_token(force_refresh=refreshed_once)
            req_headers = {'Authorization': f'Bearer {token}'}
            if headers:
                req_headers.update(headers)
            try:
                with _make_http_client(timeout=timeout, follow_redirects=True) as client:
                    response = client.request(method, url, params=params, json=json_body, headers=req_headers)
                if response.status_code == 401 and not refreshed_once:
                    refreshed_once = True
                    continue
                if response.status_code in (429, 500, 502, 503, 504) and attempt < max_retries:
                    delay = min(8.0, (2 ** attempt) + random.uniform(0.1, 0.6))
                    time.sleep(delay)
                    continue
                return response
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt < max_retries:
                    time.sleep(min(6.0, (2 ** attempt) + 0.3))
                    continue
                raise ValueError(f'网络连接 Google 服务异常：{exc}') from exc
        raise ValueError(f'Google API 请求失败：{last_error}')


class GoogleSheetsCoordinator:
    """Header-driven, row/column-shift-safe Google Sheets task coordinator with conflict detection."""

    def __init__(
        self,
        oauth: GoogleOAuthManager,
        spreadsheet_url_or_id: str,
        sheet_title: Optional[str] = None,
        custom_headers: Optional[Dict[str, str]] = None,
        http_helper: Optional[_GoogleAuthedHttp] = None
    ) -> None:
        self.oauth = oauth
        self.http = http_helper or _GoogleAuthedHttp(oauth)
        self.spreadsheet_id, self.gid_hint = parse_spreadsheet_ref(spreadsheet_url_or_id)
        self.sheet_title = (sheet_title or '').strip() or None
        self.custom_headers = custom_headers or {}
        self._metadata_cache: Optional[Tuple[str, List[Tuple[str, int]]]] = None

    def fetch_spreadsheet_metadata(self, use_cache: bool = False) -> Tuple[str, List[Tuple[str, int]]]:
        """Return (spreadsheet_title, [(sheet_title, sheet_gid), ...])."""
        if use_cache and self._metadata_cache is not None:
            return self._metadata_cache
        resp = self.http.request(
            'GET',
            f'{SHEETS_API_BASE}/{self.spreadsheet_id}',
            params={'fields': 'properties.title,sheets.properties(sheetId,title)'}
        )
        if resp.status_code != 200:
            raise ValueError(f'无法读取 Google 表格信息（HTTP {resp.status_code}），请确认当前授权账号对该表格有编辑权限：{resp.text[:240]}')
        data = resp.json()
        doc_title = data.get('properties', {}).get('title', self.spreadsheet_id)
        sheets: List[Tuple[str, int]] = []
        for item in data.get('sheets', []):
            props = item.get('properties', {})
            sheets.append((str(props.get('title', 'Sheet1')), int(props.get('sheetId', 0))))
        if not sheets:
            raise ValueError('该 Google 表格中没有任何工作表页签。')
        self._metadata_cache = (doc_title, sheets)
        return doc_title, sheets

    def resolve_sheet_title(self) -> str:
        _, sheets = self.fetch_spreadsheet_metadata(use_cache=True)
        valid_titles = {title for title, _ in sheets}
        if self.sheet_title and self.sheet_title in valid_titles:
            return self.sheet_title
        if self.gid_hint is not None:
            for title, gid in sheets:
                if gid == self.gid_hint:
                    self.sheet_title = title
                    return title
        self.sheet_title = sheets[0][0]
        return self.sheet_title

    def _read_range_rows(self, sheet_title: str, sub_range: str = '') -> List[List[str]]:
        quoted = quote_sheet_title(sheet_title)
        range_a1 = f'{quoted}!{sub_range}' if sub_range else quoted
        encoded_range = urllib.parse.quote(range_a1, safe='')
        resp = self.http.request(
            'GET',
            f'{SHEETS_API_BASE}/{self.spreadsheet_id}/values/{encoded_range}',
            params={'majorDimension': 'ROWS', 'valueRenderOption': 'FORMATTED_VALUE'}
        )
        if resp.status_code != 200:
            raise ValueError(f'读取工作表“{sheet_title}”失败（HTTP {resp.status_code}）：{resp.text[:240]}')
        values = resp.json().get('values', [])
        return [[str(cell) for cell in row] for row in values]

    def _read_all_rows(self, sheet_title: str) -> List[List[str]]:
        return self._read_range_rows(sheet_title)

    def _batch_update_values(self, updates: List[Dict[str, Any]]) -> None:
        if not updates:
            return
        resp = self.http.request(
            'POST',
            f'{SHEETS_API_BASE}/{self.spreadsheet_id}/values:batchUpdate',
            json_body={
                'valueInputOption': 'RAW',
                'data': updates
            }
        )
        if resp.status_code != 200:
            raise ValueError(f'写入 Google 表格失败（HTTP {resp.status_code}）：{resp.text[:240]}')

    def resolve_columns(
        self,
        header_row: Sequence[str],
        ensure_system_columns: bool = False,
        sheet_title: Optional[str] = None
    ) -> Dict[str, int]:
        """Locate column indices dynamically from row 1 labels; optionally append missing system columns."""
        normalized_to_idx: Dict[str, int] = {}
        for idx, raw_label in enumerate(header_row):
            norm = normalize_header_text(raw_label).lower()
            if norm and norm not in normalized_to_idx:
                normalized_to_idx[norm] = idx

        mapping: Dict[str, int] = {}
        for role, default_aliases in HEADER_ALIASES.items():
            candidates: List[str] = []
            custom_name = self.custom_headers.get(role, '').strip()
            if custom_name:
                candidates.append(custom_name)
            candidates.extend(default_aliases)
            for alias in candidates:
                norm_alias = normalize_header_text(alias).lower()
                if norm_alias in normalized_to_idx:
                    mapping[role] = normalized_to_idx[norm_alias]
                    break

        missing_inputs = [
            label for role, label in [('source_image', '源人脸图片'), ('target_video', '目标视频')]
            if role not in mapping
        ]
        if missing_inputs:
            raise ValueError(
                '表格第 1 行缺少必要的列标签：' + '、'.join(missing_inputs) +
                '。请确保第 1 行包含“源人脸图片”和“目标视频”表头（列顺序不限）。'
            )

        if ensure_system_columns:
            target_sheet = sheet_title or self.resolve_sheet_title()
            next_col = len(header_row)
            header_updates: List[Dict[str, Any]] = []
            for sys_role, default_label in DEFAULT_SYSTEM_HEADERS.items():
                if sys_role not in mapping:
                    label_to_write = self.custom_headers.get(sys_role, '').strip() or default_label
                    col_letter = col_index_to_letter(next_col)
                    header_updates.append({
                        'range': f'{quote_sheet_title(target_sheet)}!{col_letter}1',
                        'values': [[label_to_write]]
                    })
                    mapping[sys_role] = next_col
                    next_col += 1
            if header_updates:
                self._batch_update_values(header_updates)

        return mapping

    @staticmethod
    def _cell(row: Sequence[str], col_idx: Optional[int]) -> str:
        if col_idx is None or col_idx < 0 or col_idx >= len(row):
            return ''
        return str(row[col_idx]).strip()

    def fetch_tasks_snapshot(self, ensure_system_columns: bool = False) -> Tuple[str, Dict[str, int], List[SheetTaskRow]]:
        """Read row 1 headers dynamically and parse all data rows."""
        title = self.resolve_sheet_title()
        all_rows = self._read_all_rows(title)
        if not all_rows:
            raise ValueError(f'工作表“{title}”为空，请先在第 1 行填写表头（如：源人脸图片、目标视频、场景预设）。')
        col_map = self.resolve_columns(all_rows[0], ensure_system_columns=ensure_system_columns, sheet_title=title)
        tasks: List[SheetTaskRow] = []
        for offset, row in enumerate(all_rows[1:], start=2):
            source_url = self._cell(row, col_map.get('source_image'))
            target_url = self._cell(row, col_map.get('target_video'))
            preset_name = self._cell(row, col_map.get('preset'))
            task_id = self._cell(row, col_map.get('task_id'))
            status = self._cell(row, col_map.get('status'))
            worker = self._cell(row, col_map.get('worker'))
            output_url = self._cell(row, col_map.get('output_url'))
            note = self._cell(row, col_map.get('note'))
            if not any([source_url, target_url, task_id, status, output_url]):
                continue
            tasks.append(SheetTaskRow(
                row_number=offset,
                source_url=source_url,
                target_url=target_url,
                preset_name=preset_name,
                task_id=task_id,
                status=status,
                worker=worker,
                output_url=output_url,
                note=note
            ))
        return title, col_map, tasks

    def claim_specific_tasks(
        self,
        targets: Sequence[Tuple[int, str, str]],
        worker_name: str,
        verify_delay_sec: float = 1.5,
        allow_retry_failed: bool = False
    ) -> Tuple[List[SheetTaskRow], List[str]]:
        """
        Two-phase optimistic claim with read-after-write verification.
        targets: sequence of (row_hint, source_url, target_url).
        Returns (claimed_rows, conflict_messages).
        """
        clean_worker = (worker_name or '').strip()
        if not clean_worker:
            raise ValueError('请先填写自定义的“本机节点名称”（例如：Alex、Worker-01），不能使用空节点名领取任务。')
        if not targets:
            return [], []

        def _can_claim(row_item: SheetTaskRow) -> bool:
            return row_item.is_claimable or (allow_retry_failed and row_item.is_failed_retryable)

        sheet_title, col_map, latest_rows = self.fetch_tasks_snapshot(ensure_system_columns=True)
        by_row_num: Dict[int, SheetTaskRow] = {item.row_number: item for item in latest_rows}

        used_rows: set[int] = set()
        conflicts: List[str] = []
        staged_claims: List[Tuple[SheetTaskRow, str]] = []
        updates: List[Dict[str, Any]] = []
        now_text = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')

        for row_hint, expected_src, expected_tgt in targets:
            matched: Optional[SheetTaskRow] = None
            candidate = by_row_num.get(row_hint)
            if (
                candidate
                and candidate.row_number not in used_rows
                and candidate.source_url == expected_src
                and candidate.target_url == expected_tgt
            ):
                matched = candidate
            else:
                # Row may have shifted due to insertions/deletions/sorting above it!
                # Search claimable matching pair first, then any matching pair to report who claimed it.
                for item in latest_rows:
                    if item.row_number not in used_rows and item.source_url == expected_src and item.target_url == expected_tgt and _can_claim(item):
                        matched = item
                        break
                if matched is None:
                    for item in latest_rows:
                        if item.row_number not in used_rows and item.source_url == expected_src and item.target_url == expected_tgt:
                            matched = item
                            break

            if matched is None:
                conflicts.append(f'原第 {row_hint} 行需求在表格中已被删除或链接已修改，已跳过。')
                continue

            used_rows.add(matched.row_number)
            if not _can_claim(matched):
                owner = matched.worker or matched.status or matched.task_id or '其他同事'
                conflicts.append(f'第 {matched.row_number} 行已被 [{owner}] 领取（状态：{matched.status or "处理中"}），已自动跳过。')
                continue

            is_reclaim = matched.is_failed_retryable and not matched.is_claimable
            new_task_id = generate_task_id()
            staged_claims.append((matched, new_task_id))
            quoted = quote_sheet_title(sheet_title)
            row_idx = matched.row_number
            note_label = f'{now_text} 失败重领制作' if is_reclaim else f'{now_text} 已领取'
            for role, value in [
                ('task_id', new_task_id),
                ('status', SHEET_STATUS_IN_PROGRESS),
                ('worker', clean_worker),
                ('note', note_label)
            ]:
                letter = col_index_to_letter(col_map[role])
                updates.append({
                    'range': f'{quoted}!{letter}{row_idx}',
                    'values': [[value]]
                })

        if not staged_claims:
            return [], conflicts

        self._batch_update_values(updates)

        if verify_delay_sec > 0:
            time.sleep(verify_delay_sec)

        verify_title, verify_cols, verified_rows = self.fetch_tasks_snapshot(ensure_system_columns=False)
        by_task_id: Dict[str, List[SheetTaskRow]] = {}
        for item in verified_rows:
            if item.task_id:
                by_task_id.setdefault(item.task_id, []).append(item)

        claimed: List[SheetTaskRow] = []
        rollback_updates: List[Dict[str, Any]] = []

        for original_row, assigned_id in staged_claims:
            matches = by_task_id.get(assigned_id, [])
            if len(matches) == 1:
                actual = matches[0]
                if (
                    actual.worker == clean_worker
                    and actual.source_url == original_row.source_url
                    and actual.target_url == original_row.target_url
                ):
                    claimed.append(actual)
                    continue
                # Rare row-shift collision onto a different row: clean up if safe.
                if actual.worker == clean_worker and not actual.output_url:
                    quoted = quote_sheet_title(verify_title)
                    for role in ('task_id', 'status', 'worker', 'note'):
                        if role in verify_cols:
                            letter = col_index_to_letter(verify_cols[role])
                            rollback_updates.append({
                                'range': f'{quoted}!{letter}{actual.row_number}',
                                'values': [['']]
                            })
                conflicts.append(f'第 {original_row.row_number} 行在领取瞬间发生表格行变动，为安全起见已取消该条领取，请刷新后重试。')
            else:
                # Overwritten by a concurrent worker! Find who currently owns that media pair.
                winner = '其他节点'
                for item in verified_rows:
                    if item.source_url == original_row.source_url and item.target_url == original_row.target_url and item.worker:
                        winner = item.worker
                        break
                conflicts.append(f'第 {original_row.row_number} 行刚被 [{winner}] 抢先一步领取，已自动放弃避免重复制作。')

        if rollback_updates:
            try:
                self._batch_update_values(rollback_updates)
            except Exception:
                pass

        return claimed, conflicts

    def claim_next_available(
        self,
        max_count: int,
        worker_name: str,
        verify_delay_sec: float = 1.5
    ) -> Tuple[List[SheetTaskRow], List[str]]:
        """Claim up to `max_count` unclaimed tasks in top-to-bottom order, skipping raced rows if needed."""
        if max_count <= 0:
            return [], []

        claimed_total: List[SheetTaskRow] = []
        conflicts_total: List[str] = []
        tried_pairs: set[Tuple[int, str, str]] = set()

        # Up to 2 passes in case one of the top rows lost a race to a colleague.
        for _ in range(2):
            remaining = max_count - len(claimed_total)
            if remaining <= 0:
                break
            _, _, rows = self.fetch_tasks_snapshot(ensure_system_columns=True)
            batch_targets: List[Tuple[int, str, str]] = []
            for item in rows:
                key = (item.row_number, item.source_url, item.target_url)
                if item.is_claimable and key not in tried_pairs:
                    batch_targets.append(key)
                    tried_pairs.add(key)
                    if len(batch_targets) >= remaining:
                        break
            if not batch_targets:
                break
            claimed, conflicts = self.claim_specific_tasks(
                batch_targets,
                worker_name=worker_name,
                verify_delay_sec=verify_delay_sec
            )
            claimed_total.extend(claimed)
            conflicts_total.extend(conflicts)

        return claimed_total, conflicts_total

    def _locate_row_num_by_task_id(
        self,
        clean_id: str,
        ensure_system_columns: bool = True
    ) -> Tuple[str, Dict[str, int], Optional[int]]:
        """
        Dynamically read Row 1 headers + ONLY the `task_id` column to locate `clean_id`.
        100% safe against column/row moves while avoiding multi-MB full-sheet reads on large sheets.
        """
        sheet_title = self.resolve_sheet_title()
        header_rows = self._read_range_rows(sheet_title, '1:1')
        if not header_rows:
            raise ValueError(f'工作表“{sheet_title}”为空，请先在第 1 行填写表头。')
        col_map = self.resolve_columns(header_rows[0], ensure_system_columns=ensure_system_columns, sheet_title=sheet_title)
        id_col_idx = col_map['task_id']
        id_letter = col_index_to_letter(id_col_idx)

        col_rows = self._read_range_rows(sheet_title, f'{id_letter}:{id_letter}')
        is_single_col_slice = all(len(r) <= 1 for r in col_rows)
        for offset, row in enumerate(col_rows[1:], start=2):
            cell_val = self._cell(row, 0 if is_single_col_slice else id_col_idx)
            if cell_val == clean_id:
                return sheet_title, col_map, offset
        return sheet_title, col_map, None

    def update_row_by_task_id(self, task_id: str, field_values: Dict[str, str]) -> int:
        """
        Re-read row 1 headers AND locate the current row by `task_id` before writing.
        Safe against any column reordering, row insertion/deletion, or sheet sorting.
        Returns the 1-based row number updated.
        """
        clean_id = (task_id or '').strip()
        if not clean_id:
            raise ValueError('缺少有效的任务编号，无法回写表格。')

        sheet_title, col_map, target_row_num = self._locate_row_num_by_task_id(clean_id, ensure_system_columns=True)
        if target_row_num is None:
            raise ValueError(f'在表格中未找到任务编号为 {clean_id} 的行（可能已被人工删除）。')

        quoted = quote_sheet_title(sheet_title)
        updates: List[Dict[str, Any]] = []
        for role, value in field_values.items():
            if role not in col_map:
                continue
            letter = col_index_to_letter(col_map[role])
            updates.append({
                'range': f'{quoted}!{letter}{target_row_num}',
                'values': [[value]]
            })
        self._batch_update_values(updates)
        return target_row_num

    def complete_task(self, task_id: str, output_url: str, note: str) -> int:
        return self.update_row_by_task_id(task_id, {
            'status': SHEET_STATUS_COMPLETED,
            'output_url': output_url,
            'note': note
        })

    def fail_task(self, task_id: str, error_note: str) -> int:
        return self.update_row_by_task_id(task_id, {
            'status': SHEET_STATUS_FAILED,
            'note': error_note[:300]
        })

    def release_queued_task(self, task_id: str) -> int:
        """Return an unstarted queued task back to the pool so colleagues can claim it."""
        clean_id = (task_id or '').strip()
        if not clean_id:
            return 0
        sheet_title, col_map, target_row_num = self._locate_row_num_by_task_id(clean_id, ensure_system_columns=True)
        if target_row_num is None:
            return 0

        single_rows = self._read_range_rows(sheet_title, f'{target_row_num}:{target_row_num}')
        if len(single_rows) == 1:
            row_cells = single_rows[0]
        elif target_row_num - 1 < len(single_rows):
            row_cells = single_rows[target_row_num - 1]
        else:
            row_cells = []

        cur_status = self._cell(row_cells, col_map.get('status'))
        cur_output = self._cell(row_cells, col_map.get('output_url'))
        if cur_status == SHEET_STATUS_COMPLETED or cur_output:
            return target_row_num

        quoted = quote_sheet_title(sheet_title)
        updates = []
        for role, val in [('task_id', ''), ('status', SHEET_STATUS_PENDING), ('worker', ''), ('note', '')]:
            if role in col_map:
                letter = col_index_to_letter(col_map[role])
                updates.append({
                    'range': f'{quoted}!{letter}{target_row_num}',
                    'values': [[val]]
                })
        self._batch_update_values(updates)
        return target_row_num

    def find_owned_in_progress_tasks(self, worker_name: str) -> List[SheetTaskRow]:
        """Find unfinished tasks currently marked as IN_PROGRESS on the sheet under `worker_name`."""
        clean_worker = (worker_name or '').strip()
        if not clean_worker:
            return []
        _, _, rows = self.fetch_tasks_snapshot(ensure_system_columns=False)
        owned: List[SheetTaskRow] = []
        for item in rows:
            if (
                item.status.strip() == SHEET_STATUS_IN_PROGRESS
                and item.worker.strip() == clean_worker
                and bool(item.task_id.strip())
                and not item.output_url.strip()
                and is_probably_url(item.source_url)
                and is_probably_url(item.target_url)
            ):
                owned.append(item)
        return owned


class GoogleDriveClient:
    """Downloads source media (Drive links or direct URLs) and uploads/shares finished videos."""

    CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB (multiple of 256 KB as required by Google Resumable Upload)
    MAX_MEDIA_DOWNLOAD_BYTES = 4 * 1024 * 1024 * 1024  # 4 GB sanity cap per single input media file
    DOWNLOAD_MAX_RETRIES = 3

    def __init__(self, oauth: GoogleOAuthManager, http_helper: Optional[_GoogleAuthedHttp] = None) -> None:
        self.oauth = oauth
        self.http = http_helper or _GoogleAuthedHttp(oauth)
        self._default_folder_cache: Optional[str] = None

    def download_media(
        self,
        url: str,
        dest_dir: Path,
        filename_stem: str,
        default_ext: str,
        progress_cb: Optional[Callable[[int, int], None]] = None
    ) -> Path:
        local_path, _ = self.download_media_with_name(url, dest_dir, filename_stem, default_ext, progress_cb=progress_cb)
        return local_path

    def download_media_with_name(
        self,
        url: str,
        dest_dir: Path,
        filename_stem: str,
        default_ext: str,
        progress_cb: Optional[Callable[[int, int], None]] = None
    ) -> Tuple[Path, str]:
        """Download media and return (local_path, original_filename_or_empty)."""
        dest_dir.mkdir(parents=True, exist_ok=True)
        raw_url = (url or '').strip()
        if '/folders/' in raw_url and 'drive.google.com' in raw_url:
            raise ValueError('您填写的是 Google Drive“文件夹”链接而非具体文件链接，请替换为具体图片/视频的分享链接。')
        drive_file_id = parse_drive_id(raw_url)
        if drive_file_id and ('drive.google.com' in raw_url or not raw_url.startswith('http')):
            return self._download_from_drive(drive_file_id, dest_dir, filename_stem, default_ext, progress_cb)
        return self._download_from_http(raw_url, dest_dir, filename_stem, default_ext, progress_cb)

    def _download_from_drive(
        self,
        file_id: str,
        dest_dir: Path,
        filename_stem: str,
        default_ext: str,
        progress_cb: Optional[Callable[[int, int], None]]
    ) -> Tuple[Path, str]:
        meta_resp = self.http.request(
            'GET',
            f'{DRIVE_API_BASE}/files/{file_id}',
            params={'fields': 'id,name,mimeType,size', 'supportsAllDrives': 'true'}
        )
        ext = default_ext
        orig_name = ''
        total_bytes = 0
        if meta_resp.status_code == 200:
            meta = meta_resp.json()
            mime_type = str(meta.get('mimeType') or '')
            if mime_type == 'application/vnd.google-apps.folder':
                raise ValueError('您填写的是 Google Drive“文件夹”链接而非具体文件链接，请替换为具体图片/视频文件的分享链接。')
            if mime_type.startswith('application/vnd.google-apps.'):
                raise ValueError(f'该链接是 Google 在线文档类型（{mime_type}），不是可直接下载的图片或视频素材。')
            orig_name = str(meta.get('name') or '')
            orig_ext = Path(orig_name).suffix.lower()
            if orig_ext and len(orig_ext) <= 6:
                ext = orig_ext
            elif mime_type:
                guessed = mimetypes.guess_extension(mime_type)
                if guessed:
                    ext = guessed
            total_bytes = int(meta.get('size') or 0)
        elif meta_resp.status_code in (403, 404):
            # Fallback to public direct export URL if file isn't visible to Drive API metadata
            public_url = f'https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t'
            return self._download_from_http(public_url, dest_dir, filename_stem, default_ext, progress_cb)
        else:
            raise ValueError(f'获取 Google Drive 素材信息失败（HTTP {meta_resp.status_code}）：{meta_resp.text[:200]}')

        if total_bytes > self.MAX_MEDIA_DOWNLOAD_BYTES:
            gb = total_bytes / (1024 ** 3)
            raise ValueError(f'Google Drive 素材文件体积过大（{gb:.1f} GB，上限 4 GB），请检查是否误填了未剪辑母带。')

        dest_dir.mkdir(parents=True, exist_ok=True)
        target_path = dest_dir / f'{filename_stem}{ext}'
        part_path = dest_dir / f'{filename_stem}{ext}.part'
        force_refresh = False
        last_exc: Optional[Exception] = None

        for attempt in range(self.DOWNLOAD_MAX_RETRIES):
            token = self.oauth.get_access_token(force_refresh=force_refresh)
            force_refresh = False
            downloaded = 0
            part_path.unlink(missing_ok=True)
            try:
                with _make_http_client(timeout=httpx.Timeout(30.0, read=120.0), follow_redirects=True) as client:
                    with client.stream(
                        'GET',
                        f'{DRIVE_API_BASE}/files/{file_id}',
                        params={'alt': 'media', 'supportsAllDrives': 'true'},
                        headers={'Authorization': f'Bearer {token}'}
                    ) as stream:
                        if stream.status_code == 401 and attempt < self.DOWNLOAD_MAX_RETRIES - 1:
                            force_refresh = True
                            continue
                        if stream.status_code in (429, 500, 502, 503, 504) and attempt < self.DOWNLOAD_MAX_RETRIES - 1:
                            time.sleep(min(8.0, (2 ** attempt) + 0.5))
                            continue
                        if stream.status_code != 200:
                            body_preview = stream.read()[:200].decode('utf-8', errors='replace')
                            raise ValueError(f'下载 Google Drive 素材失败（HTTP {stream.status_code}）：{body_preview}')
                        if not total_bytes:
                            total_bytes = int(stream.headers.get('Content-Length') or 0)
                        if total_bytes > self.MAX_MEDIA_DOWNLOAD_BYTES:
                            gb = total_bytes / (1024 ** 3)
                            raise ValueError(f'Google Drive 素材文件体积过大（{gb:.1f} GB，上限 4 GB），已拦截下载。')
                        with part_path.open('wb') as out:
                            for chunk in stream.iter_bytes(chunk_size=1024 * 1024):
                                out.write(chunk)
                                downloaded += len(chunk)
                                if downloaded > self.MAX_MEDIA_DOWNLOAD_BYTES:
                                    raise ValueError('下载素材体积超过 4 GB 安全上限，已自动中止保护磁盘空间。')
                                if progress_cb:
                                    progress_cb(downloaded, total_bytes)
                if not part_path.is_file() or part_path.stat().st_size == 0:
                    raise ValueError('下载的 Google Drive 文件为空（0 字节）。')
                os.replace(part_path, target_path)
                return target_path, orig_name
            except (InterruptedError, ValueError):
                part_path.unlink(missing_ok=True)
                raise
            except httpx.HTTPError as exc:
                last_exc = exc
                part_path.unlink(missing_ok=True)
                if attempt < self.DOWNLOAD_MAX_RETRIES - 1:
                    time.sleep(min(6.0, 1.5 * (attempt + 1)))
                    continue
                raise ValueError(f'下载 Google Drive 素材网络连接中断（已重试 {self.DOWNLOAD_MAX_RETRIES} 次）：{exc}') from exc
            finally:
                part_path.unlink(missing_ok=True)

        raise ValueError(f'下载 Google Drive 素材失败：{last_exc}')

    @staticmethod
    def _parse_content_disposition_filename(header_val: str) -> str:
        if not header_val:
            return ''
        utf8_m = re.search(r"filename\*\s*=\s*UTF-8''([^;]+)", header_val, flags=re.IGNORECASE)
        if utf8_m:
            return urllib.parse.unquote(utf8_m.group(1)).strip('"\' ')
        plain_m = re.search(r'filename\s*=\s*"([^"]+)"', header_val, flags=re.IGNORECASE)
        if plain_m:
            return plain_m.group(1).strip()
        bare_m = re.search(r'filename\s*=\s*([^;]+)', header_val, flags=re.IGNORECASE)
        if bare_m:
            return bare_m.group(1).strip('"\' ')
        return ''

    @staticmethod
    def _extract_drive_confirm_url(html_text: str) -> Optional[str]:
        """Extract confirmation download URL from Google Drive's >100MB virus-scan HTML form."""
        action_m = re.search(r'<form[^>]+id="download-form"[^>]+action="([^"]+)"', html_text)
        if not action_m:
            action_m = re.search(r'<form[^>]+action="(https://drive\.usercontent\.google\.com/download[^"]*)"', html_text)
        if not action_m:
            return None
        action_url = action_m.group(1).replace('&amp;', '&')
        inputs = re.findall(r'<input[^>]+type="hidden"[^>]*>', html_text)
        params: Dict[str, str] = {}
        for tag in inputs:
            n_m = re.search(r'name="([^"]+)"', tag)
            v_m = re.search(r'value="([^"]*)"', tag)
            if n_m and v_m:
                params[n_m.group(1)] = v_m.group(1)
        if not params:
            return None
        sep = '&' if '?' in action_url else '?'
        return f'{action_url}{sep}{urllib.parse.urlencode(params)}'

    def _download_from_http(
        self,
        url: str,
        dest_dir: Path,
        filename_stem: str,
        default_ext: str,
        progress_cb: Optional[Callable[[int, int], None]],
        _allow_drive_confirm_retry: bool = True
    ) -> Tuple[Path, str]:
        dest_dir.mkdir(parents=True, exist_ok=True)
        clean_url = url.strip()
        parsed = urllib.parse.urlparse(clean_url)
        url_basename = Path(urllib.parse.unquote(parsed.path)).name
        path_ext = Path(url_basename).suffix.lower()
        ext = path_ext if (path_ext and len(path_ext) <= 6) else default_ext
        orig_name = url_basename if path_ext else ''

        last_exc: Optional[Exception] = None
        for attempt in range(self.DOWNLOAD_MAX_RETRIES):
            downloaded = 0
            cur_ext = ext
            cur_orig = orig_name
            part_path: Optional[Path] = None
            try:
                with _make_http_client(timeout=httpx.Timeout(30.0, read=120.0), follow_redirects=True) as client:
                    with client.stream('GET', clean_url, headers={'User-Agent': 'FaceFusion-SheetWorker/1.0'}) as stream:
                        if stream.status_code in (429, 500, 502, 503, 504) and attempt < self.DOWNLOAD_MAX_RETRIES - 1:
                            time.sleep(min(8.0, (2 ** attempt) + 0.5))
                            continue
                        if stream.status_code != 200:
                            raise ValueError(f'素材链接下载失败（HTTP {stream.status_code}）：{clean_url}')
                        content_type = (stream.headers.get('Content-Type') or '').split(';')[0].strip().lower()
                        if content_type == 'text/html':
                            if _allow_drive_confirm_retry and ('drive.google.com' in clean_url or 'googleusercontent.com' in clean_url):
                                html_preview = stream.read()[:131072].decode('utf-8', errors='replace')
                                confirm_url = self._extract_drive_confirm_url(html_preview)
                                if confirm_url:
                                    return self._download_from_http(
                                        confirm_url,
                                        dest_dir,
                                        filename_stem,
                                        default_ext,
                                        progress_cb,
                                        _allow_drive_confirm_retry=False
                                    )
                            raise ValueError('素材链接返回了网页（HTML）而非图片/视频文件，请确认分享链接已开启访问权限且为直接文件链接。')

                        disp_name = self._parse_content_disposition_filename(stream.headers.get('Content-Disposition') or '')
                        if disp_name:
                            cur_orig = disp_name
                            disp_ext = Path(disp_name).suffix.lower()
                            if disp_ext and len(disp_ext) <= 6:
                                cur_ext = disp_ext

                        if cur_ext == default_ext and content_type:
                            guessed = mimetypes.guess_extension(content_type)
                            if guessed and guessed != '.jpe':
                                cur_ext = guessed
                        target_path = dest_dir / f'{filename_stem}{cur_ext}'
                        part_path = dest_dir / f'{filename_stem}{cur_ext}.part'
                        part_path.unlink(missing_ok=True)
                        total_bytes = int(stream.headers.get('Content-Length') or 0)
                        if total_bytes > self.MAX_MEDIA_DOWNLOAD_BYTES:
                            gb = total_bytes / (1024 ** 3)
                            raise ValueError(f'素材文件体积过大（{gb:.1f} GB，上限 4 GB），已拦截下载。')
                        with part_path.open('wb') as out:
                            for chunk in stream.iter_bytes(chunk_size=1024 * 1024):
                                out.write(chunk)
                                downloaded += len(chunk)
                                if downloaded > self.MAX_MEDIA_DOWNLOAD_BYTES:
                                    raise ValueError('下载素材体积超过 4 GB 安全上限，已自动中止保护磁盘空间。')
                                if progress_cb:
                                    progress_cb(downloaded, total_bytes)

                if part_path is None or not part_path.is_file() or part_path.stat().st_size == 0:
                    raise ValueError('下载的素材文件为空（0 字节）。')
                os.replace(part_path, target_path)
                return target_path, cur_orig
            except (InterruptedError, ValueError):
                if part_path is not None:
                    part_path.unlink(missing_ok=True)
                raise
            except httpx.HTTPError as exc:
                last_exc = exc
                if part_path is not None:
                    part_path.unlink(missing_ok=True)
                if attempt < self.DOWNLOAD_MAX_RETRIES - 1:
                    time.sleep(min(6.0, 1.5 * (attempt + 1)))
                    continue
                raise ValueError(f'下载素材网络连接中断（已重试 {self.DOWNLOAD_MAX_RETRIES} 次）：{exc}') from exc
            finally:
                if part_path is not None:
                    part_path.unlink(missing_ok=True)

        raise ValueError(f'素材链接下载失败：{last_exc}')

    def ensure_output_folder(self, folder_url_or_id: str = '') -> str:
        explicit_id = parse_drive_id(folder_url_or_id)
        if explicit_id:
            return explicit_id
        if self._default_folder_cache:
            return self._default_folder_cache

        folder_name = 'FaceFusion_自动换脸成品'
        query = f"mimeType='application/vnd.google-apps.folder' and name='{folder_name}' and trashed=false"
        resp = self.http.request(
            'GET',
            f'{DRIVE_API_BASE}/files',
            params={'q': query, 'fields': 'files(id,name)', 'pageSize': 1, 'supportsAllDrives': 'true', 'includeItemsFromAllDrives': 'true'}
        )
        if resp.status_code == 200:
            files = resp.json().get('files', [])
            if files:
                self._default_folder_cache = str(files[0]['id'])
                return self._default_folder_cache

        create_resp = self.http.request(
            'POST',
            f'{DRIVE_API_BASE}/files',
            params={'fields': 'id', 'supportsAllDrives': 'true'},
            json_body={'name': folder_name, 'mimeType': 'application/vnd.google-apps.folder'}
        )
        if create_resp.status_code not in (200, 201):
            raise ValueError(f'创建默认 Google Drive 成品文件夹失败（HTTP {create_resp.status_code}）：{create_resp.text[:200]}')
        self._default_folder_cache = str(create_resp.json()['id'])
        return self._default_folder_cache

    def upload_and_share(
        self,
        file_path: Path,
        upload_name: str,
        folder_url_or_id: str = '',
        progress_cb: Optional[Callable[[int, int], None]] = None
    ) -> str:
        share_link, _ = self.upload_and_share_detailed(
            file_path=file_path,
            upload_name=upload_name,
            folder_url_or_id=folder_url_or_id,
            progress_cb=progress_cb
        )
        return share_link

    def upload_and_share_detailed(
        self,
        file_path: Path,
        upload_name: str,
        folder_url_or_id: str = '',
        progress_cb: Optional[Callable[[int, int], None]] = None
    ) -> Tuple[str, str]:
        """Resumable upload to Google Drive + set 'anyone' reader permission (graceful on Workspace 403)."""
        path = Path(file_path)
        if not path.is_file():
            raise ValueError(f'找不到待上传的成品文件：{path}')
        total_size = path.stat().st_size
        if total_size <= 0:
            raise ValueError('成品视频文件大小为 0 字节，中止上传。')

        folder_id = self.ensure_output_folder(folder_url_or_id)
        mime_type = mimetypes.guess_type(upload_name)[0] or 'video/mp4'

        init_resp = self.http.request(
            'POST',
            f'{DRIVE_UPLOAD_BASE}/files',
            params={
                'uploadType': 'resumable',
                'supportsAllDrives': 'true',
                'fields': 'id,name,webViewLink'
            },
            headers={
                'X-Upload-Content-Type': mime_type,
                'X-Upload-Content-Length': str(total_size)
            },
            json_body={
                'name': upload_name,
                'parents': [folder_id]
            }
        )
        if init_resp.status_code not in (200, 201):
            if 'storageQuotaExceeded' in init_resp.text:
                raise ValueError('当前 Google Drive 云端硬盘空间已满（storageQuotaExceeded），请清理网盘空间/回收站或改填团队共享文件夹后点击“补传本地保底成品”。')
            if init_resp.status_code in (403, 404) and folder_url_or_id.strip():
                raise ValueError(f'当前 Google 账号无权写入所配置的成品文件夹（HTTP {init_resp.status_code}），请确认该网盘目录已对此账号开放“编辑者”权限：{init_resp.text[:180]}')
            raise ValueError(f'初始化 Google Drive 上传失败（HTTP {init_resp.status_code}）：{init_resp.text[:240]}')

        session_uri = init_resp.headers.get('Location')
        if not session_uri:
            raise ValueError('Google Drive 未返回上传会话地址（Location）。')

        uploaded = 0
        file_meta: Optional[Dict[str, Any]] = None
        upload_timeout = httpx.Timeout(30.0, write=180.0, read=120.0)
        upload_client = _make_http_client(timeout=upload_timeout)
        try:
            with path.open('rb') as stream:
                while uploaded < total_size:
                    chunk = stream.read(self.CHUNK_SIZE)
                    if not chunk:
                        break
                    start = uploaded
                    end = uploaded + len(chunk) - 1
                    content_range = f'bytes {start}-{end}/{total_size}'

                    put_resp: Optional[httpx.Response] = None
                    for attempt in range(3):
                        try:
                            put_resp = upload_client.put(
                                session_uri,
                                content=chunk,
                                headers={
                                    'Content-Length': str(len(chunk)),
                                    'Content-Range': content_range,
                                    'Content-Type': mime_type
                                }
                            )
                            if put_resp.status_code in (200, 201, 308):
                                break
                        except httpx.HTTPError as exc:
                            try:
                                upload_client.close()
                            except Exception:
                                pass
                            if attempt == 2:
                                raise ValueError(f'上传视频分片网络连接中断（已重试 3 次）：{exc}') from exc
                            upload_client = _make_http_client(timeout=upload_timeout)
                        time.sleep(1.5 * (attempt + 1))

                    if put_resp is None or put_resp.status_code not in (200, 201, 308):
                        code = put_resp.status_code if put_resp is not None else 'network_error'
                        detail = put_resp.text[:200] if put_resp is not None else ''
                        if put_resp is not None and 'storageQuotaExceeded' in put_resp.text:
                            raise ValueError('当前 Google Drive 云端硬盘空间已满（storageQuotaExceeded），请清理网盘空间/回收站后点击“补传本地保底成品”。')
                        raise ValueError(f'上传视频分片到 Google Drive 失败（HTTP {code}）：{detail}')

                    uploaded += len(chunk)
                    if progress_cb:
                        progress_cb(uploaded, total_size)
                    if put_resp.status_code in (200, 201):
                        file_meta = put_resp.json()
        finally:
            try:
                upload_client.close()
            except Exception:
                pass

        if not file_meta or not file_meta.get('id'):
            raise ValueError('视频上传结束后未收到 Google Drive 文件 ID。')

        file_id = str(file_meta['id'])
        share_link = str(file_meta.get('webViewLink') or f'https://drive.google.com/file/d/{file_id}/view?usp=sharing')
        perm_resp = self.http.request(
            'POST',
            f'{DRIVE_API_BASE}/files/{file_id}/permissions',
            params={'supportsAllDrives': 'true'},
            json_body={'role': 'reader', 'type': 'anyone'}
        )
        perm_warning = ''
        if perm_resp.status_code not in (200, 201):
            if perm_resp.status_code == 403:
                perm_warning = '网盘企业策略限制公开外链，已保留共享目录内访问链接'
            else:
                raise ValueError(f'视频已上传但开启公开共享权限失败（HTTP {perm_resp.status_code}）：{perm_resp.text[:200]}')

        return share_link, perm_warning
