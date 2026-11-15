"""Gradio visual dashboard for Google Sheets task claiming, conflict feedback, and progress monitoring."""
from __future__ import annotations

import html
import json
import time
from typing import Any, List, Optional, Sequence, Tuple

import gradio

from facefusion.google_sheets_client import (
    SHEET_STATUS_COMPLETED,
    SHEET_STATUS_FAILED,
    SheetTaskRow,
    is_probably_url,
    parse_drive_id,
    parse_spreadsheet_ref,
)
from facefusion.sheet_worker import (
    AUTO_CLAIM_INTERVAL_MINUTES,
    AUTO_MAX_ACTIVE_TASKS,
    MAX_RECENT_LOCAL_OUTPUTS,
    WORKER_MANAGER,
    LocalTask,
)

MAX_UI_ALL_ROWS = 500

# UI component references
SUMMARY_HTML: Optional[gradio.HTML] = None
BANNER_HTML: Optional[gradio.HTML] = None

BACKUP_ALERT_GROUP: Optional[gradio.Column] = None
BACKUP_ALERT_HTML: Optional[gradio.HTML] = None
RETRY_BACKUP_UPLOAD_BUTTON: Optional[gradio.Button] = None

CONFIG_ACCORDION: Optional[gradio.Accordion] = None
WORKER_NAME_TEXTBOX: Optional[gradio.Textbox] = None
SPREADSHEET_URL_TEXTBOX: Optional[gradio.Textbox] = None
SHEET_TITLE_DROPDOWN: Optional[gradio.Dropdown] = None
DRIVE_FOLDER_TEXTBOX: Optional[gradio.Textbox] = None
CLIENT_SECRET_FILE: Optional[gradio.File] = None
AUTH_STATUS_TEXTBOX: Optional[gradio.Textbox] = None
AUTHORIZE_BUTTON: Optional[gradio.Button] = None
LOGOUT_AUTH_BUTTON: Optional[gradio.Button] = None
AUTH_PENDING_GROUP: Optional[gradio.Column] = None
AUTH_LINK_HTML: Optional[gradio.HTML] = None
MANUAL_AUTH_CODE_TEXTBOX: Optional[gradio.Textbox] = None
MANUAL_AUTH_SUBMIT_BUTTON: Optional[gradio.Button] = None
SAVE_CONNECT_BUTTON: Optional[gradio.Button] = None

REFRESH_BUTTON: Optional[gradio.Button] = None
RECOVER_OWNED_BUTTON: Optional[gradio.Button] = None
AUTO_CLAIM_CHECKBOX: Optional[gradio.Checkbox] = None
AUTO_POLL_NOW_BUTTON: Optional[gradio.Button] = None

SHOW_FAILED_CHECKBOX: Optional[gradio.Checkbox] = None
CLAIMABLE_EMPTY_HTML: Optional[gradio.HTML] = None
CLAIMABLE_ACTION_COL: Optional[gradio.Column] = None
CLAIMABLE_DATAFRAME: Optional[gradio.Dataframe] = None
CLAIMABLE_CHECKBOX_GROUP: Optional[gradio.CheckboxGroup] = None
SELECT_ALL_BUTTON: Optional[gradio.Button] = None
CLEAR_SELECT_BUTTON: Optional[gradio.Button] = None
CLAIM_SELECTED_BUTTON: Optional[gradio.Button] = None
ALL_ROWS_DATAFRAME: Optional[gradio.Dataframe] = None

CURRENT_TASK_HTML: Optional[gradio.HTML] = None
ABORT_CURRENT_BUTTON: Optional[gradio.Button] = None
OPEN_LOCAL_OUTPUTS_BUTTON: Optional[gradio.Button] = None
QUEUE_GROUP: Optional[gradio.Column] = None
QUEUED_SELECT_DROPDOWN: Optional[gradio.Dropdown] = None
RELEASE_QUEUE_BUTTON: Optional[gradio.Button] = None
LOCAL_QUEUE_DATAFRAME: Optional[gradio.Dataframe] = None
WORKER_LOG_TEXTBOX: Optional[gradio.Textbox] = None

SEEN_REVISION_STATE: Optional[gradio.State] = None
SEEN_QUEUE_SIG_STATE: Optional[gradio.State] = None
SEEN_UI_VERSION_STATE: Optional[gradio.State] = None
SEEN_AUTO_ENABLED_STATE: Optional[gradio.State] = None
SEEN_AUTH_SIG_STATE: Optional[gradio.State] = None
LIVE_TIMER: Optional[gradio.Timer] = None


def _is_connection_ready() -> bool:
    cfg = WORKER_MANAGER.config
    return bool(
        cfg.worker_name.strip()
        and cfg.spreadsheet_url.strip()
        and WORKER_MANAGER.oauth.has_valid_refresh_token()
    )


def _secret_file_label() -> str:
    if WORKER_MANAGER.oauth.has_valid_refresh_token():
        return '客户端 JSON（已授权，如需更换再重传）'
    if WORKER_MANAGER.has_client_secret_file():
        return '客户端 JSON（已就绪，无需重传）'
    return '上传客户端 JSON（桌面应用）'


def _build_auth_link_html() -> str:
    with WORKER_MANAGER._lock:
        url = WORKER_MANAGER.pending_auth_url
        in_prog = WORKER_MANAGER.auth_in_progress
    if not in_prog or not url:
        return ''
    safe_url = html.escape(url, quote=True)
    return (
        f'<div>'
        f'<a class="ff-auth-link-btn" href="{safe_url}" target="_blank" rel="noopener noreferrer">'
        f'🌐 点此在当前浏览器打开 Google 登录授权页 ↗'
        f'</a>'
        f'<div class="ff-auth-subhint">'
        f'请在打开的 Google 页面点击“允许”，完成后此处 2 秒内自动变为绿色“已授权”并连接表格。'
        f'</div>'
        f'</div>'
    )


def _auth_sync_signature() -> str:
    with WORKER_MANAGER._lock:
        in_prog = WORKER_MANAGER.auth_in_progress
        url = WORKER_MANAGER.pending_auth_url
        doc_title = WORKER_MANAGER.spreadsheet_title
        sel_sheet = WORKER_MANAGER.config.sheet_title
        conn_failed = WORKER_MANAGER.last_connect_failed
    status_txt = WORKER_MANAGER.get_auth_status_text()
    return f'{status_txt}|{in_prog}|{url}|{doc_title}|{sel_sheet}|{conn_failed}'


def _missing_setup_prompt() -> str:
    cfg = WORKER_MANAGER.config
    missing: List[str] = []
    if not cfg.worker_name.strip():
        missing.append('① 填写本机工位名称')
    if not WORKER_MANAGER.oauth.has_valid_refresh_token():
        if WORKER_MANAGER.has_client_secret_file():
            missing.append('② 点击“🔑 登录 Google”完成网页确认')
        else:
            missing.append('② 上传 JSON 并登录 Google 账号')
    if not cfg.spreadsheet_url.strip():
        missing.append('③ 粘贴云表格链接并点击保存连接')
    joined = '、'.join(missing) if missing else '绑定配置'
    return f'已为您展开上方连接设置，请先完成：{joined}。'


def _short_media_hint(url: str) -> str:
    text = (url or '').strip()
    if not text:
        return '-'
    drive_id = parse_drive_id(text)
    if drive_id:
        return f'云盘文件(...{drive_id[-6:]})'
    tail = text.split('?', 1)[0].rstrip('/').rsplit('/', 1)[-1]
    if tail and len(tail) <= 28:
        return tail
    if len(text) <= 28:
        return text
    return text[:14] + '...' + text[-10:]


def _md_link(url: str, label: str = '点击查看') -> str:
    clean = (url or '').strip()
    if not clean or clean == '-':
        return '-'
    if not is_probably_url(clean):
        return clean
    href = clean if clean.startswith(('http://', 'https://')) else f'https://{clean}'
    safe_href = href.replace('(', '%28').replace(')', '%29').replace(' ', '%20')
    return f'[{label}]({safe_href})'


def _encode_choice_token(row: SheetTaskRow) -> str:
    return json.dumps({'row': row.row_number, 'src': row.source_url, 'tgt': row.target_url}, ensure_ascii=False)


def _step_header_html(badge: str, title: str, hint: str = '') -> str:
    hint_part = f'<span class="ff-step-hint">{html.escape(hint)}</span>' if hint else ''
    return (
        f'<div class="ff-step-head">'
        f'<div class="ff-step-head-left">'
        f'<span class="ff-step-badge">{html.escape(badge)}</span>'
        f'<span class="ff-step-title">{html.escape(title)}</span>'
        f'</div>'
        f'{hint_part}'
        f'</div>'
    )


def _build_claimable_view(
    rows: Sequence[SheetTaskRow],
    include_failed: bool = False
) -> Tuple[List[List[Any]], List[Tuple[str, str]]]:
    table_data: List[List[Any]] = []
    choices: List[Tuple[str, str]] = []
    for item in rows:
        can_take = item.is_claimable or (include_failed and item.is_failed_retryable)
        if not can_take:
            continue
        is_retry = item.is_failed_retryable and not item.is_claimable
        tag = '[失败重做] ' if is_retry else ''
        preset_disp = f'{tag}{item.preset_name or "没问题选这个"}'
        table_data.append([
            item.row_number,
            preset_disp,
            _md_link(item.source_url, '查看源图'),
            _md_link(item.target_url, '查看原片')
        ])
        label = f'{tag}第 {item.row_number} 行  ·  {item.preset_name or "没问题选这个"}  ·  {_short_media_hint(item.target_url)}'
        choices.append((label, _encode_choice_token(item)))
    return table_data, choices


def _build_empty_claimable_html(has_choices: bool) -> str:
    if has_choices:
        return ''
    if not _is_connection_ready():
        return (
            '<div class="ff-empty-box">'
            '👆 <b>尚未连接云表格</b><br>'
            '请先在上方 <b>⚙️ 账号与云表格连接设置</b> 按 1 → 2 → 3 步完成绑定，连接成功后待领需求将自动显示在此处。'
            '</div>'
        )
    if WORKER_MANAGER.last_refresh_at == '尚未刷新':
        return (
            '<div class="ff-empty-box">'
            '正在等待连接或刷新表格，您也可以点击上方 <b>🔄 刷新需求</b> 立即读取。'
            '</div>'
        )
    return (
        '<div class="ff-empty-box">'
        '✨ <b>当前没有待领取的新需求</b><br>'
        '可勾选上方 <b>⚡ 自动接单</b> 挂机巡检，或稍后点击 <b>🔄 刷新需求</b>。'
        '</div>'
    )


def _build_all_rows_table(rows: Sequence[SheetTaskRow]) -> List[List[Any]]:
    if len(rows) > MAX_UI_ALL_ROWS:
        active_or_pending = [
            r for r in rows
            if not (r.status == SHEET_STATUS_COMPLETED or bool(r.output_url))
        ]
        completed_rows = [
            r for r in rows
            if (r.status == SHEET_STATUS_COMPLETED or bool(r.output_url))
        ]
        keep_completed = max(0, MAX_UI_ALL_ROWS - len(active_or_pending))
        display_rows = sorted(
            active_or_pending + (completed_rows[-keep_completed:] if keep_completed > 0 else []),
            key=lambda item: item.row_number
        )
        if len(display_rows) > MAX_UI_ALL_ROWS:
            display_rows = display_rows[-MAX_UI_ALL_ROWS:]
    else:
        display_rows = list(rows)

    data: List[List[Any]] = []
    for item in display_rows:
        state_label = item.status or ('待领取' if item.is_claimable else '-')
        data.append([
            item.row_number,
            item.task_id or '-',
            state_label,
            item.worker or '-',
            item.preset_name or '默认',
            _md_link(item.output_url, '打开成品'),
            item.note or '-'
        ])
    return data


def _build_banner_html(banner_text: str) -> str:
    txt = (banner_text or '').strip()
    if not txt:
        return ''
    warn_keywords = ('失败', '冲突', '抢先', '异常', '暂停', '不足', '未设置', '未授权', '请先', '展开')
    cls = 'ff-notice-bar warn' if any(k in txt for k in warn_keywords) else 'ff-notice-bar'
    icon = '⚠️ ' if 'warn' in cls else '💡 '
    return f'<div class="{cls}">{icon}{html.escape(txt)}</div>'


def _build_backup_alert_state() -> Tuple[bool, str]:
    backup_cnt, _ = WORKER_MANAGER.cached_file_counts()
    if backup_cnt <= 0:
        return False, ''
    msg_html = (
        f'<div style="font-size:0.86rem;color:#92400e;font-weight:600;">'
        f'📦 检测到本机有 <b>{backup_cnt}</b> 条已换脸完成但因网络波动未上传成功的成品视频，无需重新换脸，点击右侧按钮即可直接补传回写！'
        f'</div>'
    )
    return True, msg_html


def _build_dashboard_html() -> str:
    with WORKER_MANAGER._lock:
        rows = list(WORKER_MANAGER.cached_sheet_rows)
        last_ref = WORKER_MANAGER.last_refresh_at
        sheet_name = WORKER_MANAGER.config.sheet_title
        doc_title = WORKER_MANAGER.spreadsheet_title
        worker_name = WORKER_MANAGER.config.worker_name.strip()
        has_url = bool(WORKER_MANAGER.config.spreadsheet_url.strip())
        conn_failed = WORKER_MANAGER.last_connect_failed
        running = WORKER_MANAGER.current_task
        queued_cnt = len(WORKER_MANAGER.queued_tasks)
        auto_on = WORKER_MANAGER.auto_claim_enabled
        next_ts = WORKER_MANAGER.next_auto_poll_ts
    _, recent_local_cnt = WORKER_MANAGER.cached_file_counts()
    has_token = WORKER_MANAGER.oauth.has_valid_refresh_token()

    claimable_cnt = sum(1 for r in rows if r.is_claimable)
    completed_cnt = sum(1 for r in rows if r.status == SHEET_STATUS_COMPLETED or bool(r.output_url))
    failed_cnt = sum(1 for r in rows if r.status == SHEET_STATUS_FAILED)
    in_progress_cnt = sum(
        1 for r in rows
        if not r.is_claimable and r.status not in {SHEET_STATUS_COMPLETED, SHEET_STATUS_FAILED} and (r.task_id or r.status)
    )

    if doc_title and not conn_failed:
        title_disp = f'{doc_title} / {sheet_name}' if sheet_name else doc_title
        conn_pill = f'<span class="ff-pill ff-pill-ok">🟢 已连接：{html.escape(title_disp)}</span>'
    elif conn_failed:
        conn_pill = '<span class="ff-pill ff-pill-warn">🔴 连接异常，请检查设置</span>'
    elif has_url and has_token and worker_name:
        conn_pill = '<span class="ff-pill ff-pill-active">🔵 准备连接表格...</span>'
    else:
        conn_pill = '<span class="ff-pill ff-pill-warn">⚙️ 待绑定云表格（请完成下方 3 步设置）</span>'

    if worker_name:
        worker_pill = f'<span class="ff-pill ff-pill-idle">工位：{html.escape(worker_name)}</span>'
    else:
        worker_pill = '<span class="ff-pill ff-pill-warn">● 待填写工位名</span>'

    running_cnt = 1 if running else 0
    active_total = running_cnt + queued_cnt

    if running:
        local_val = '制作中'
        local_cls = 'ff-kpi-value accent'
        local_sub = f'1 条制作中 · {queued_cnt} 条排队等待'
    elif queued_cnt > 0:
        local_val = '准备启动'
        local_cls = 'ff-kpi-value accent'
        local_sub = f'{queued_cnt} 条排队任务即将开始'
    else:
        local_val = '空闲待命'
        local_cls = 'ff-kpi-value'
        local_sub = '可左侧领单或开启自动接单'

    if auto_on:
        remain = max(0, int(next_ts - time.time()))
        mins, secs = divmod(remain, 60)
        auto_val = '已开启'
        auto_cls = 'ff-kpi-value accent'
        if active_total >= AUTO_MAX_ACTIVE_TASKS:
            auto_sub = f'已满载暂停领新单 · {mins}分{secs:02d}秒后巡检'
        else:
            auto_sub = f'下次巡检：{mins}分{secs:02d}秒后（每{AUTO_CLAIM_INTERVAL_MINUTES}分钟）'
    else:
        auto_val = '未开启'
        auto_cls = 'ff-kpi-value'
        auto_sub = f'开启后每 {AUTO_CLAIM_INTERVAL_MINUTES} 分钟自动补单'

    claim_cls = 'ff-kpi-value accent' if claimable_cnt > 0 else 'ff-kpi-value'

    return (
        f'<div class="ff-sw-dashboard">'
        f'  <div class="ff-sw-topbar">'
        f'    <div class="ff-sw-topbar-group">{conn_pill}{worker_pill}</div>'
        f'    <div class="ff-sw-topbar-group">'
        f'      <span>上次刷新：{html.escape(last_ref)}</span>'
        f'      <span>·</span>'
        f'      <span>本机近期成品：{recent_local_cnt}/{MAX_RECENT_LOCAL_OUTPUTS} 条</span>'
        f'    </div>'
        f'  </div>'
        f'  <div class="ff-kpi-grid">'
        f'    <div class="ff-kpi-card">'
        f'      <div class="ff-kpi-label">待领取需求</div>'
        f'      <div class="{claim_cls}">{claimable_cnt} 条</div>'
        f'      <div class="ff-kpi-sub">全表他人/本机制作中：{in_progress_cnt} 条</div>'
        f'    </div>'
        f'    <div class="ff-kpi-card">'
        f'      <div class="ff-kpi-label">本机运行状态</div>'
        f'      <div class="{local_cls}">{local_val}</div>'
        f'      <div class="ff-kpi-sub">{local_sub}</div>'
        f'    </div>'
        f'    <div class="ff-kpi-card">'
        f'      <div class="ff-kpi-label">自动接单模式</div>'
        f'      <div class="{auto_cls}">{auto_val}</div>'
        f'      <div class="ff-kpi-sub">{auto_sub}</div>'
        f'    </div>'
        f'    <div class="ff-kpi-card">'
        f'      <div class="ff-kpi-label">全表累计完成</div>'
        f'      <div class="ff-kpi-value">{completed_cnt} 条</div>'
        f'      <div class="ff-kpi-sub">制作失败：{failed_cnt} 条</div>'
        f'    </div>'
        f'  </div>'
        f'</div>'
    )


def _build_current_task_state() -> Tuple[str, bool]:
    with WORKER_MANAGER._lock:
        task: Optional[LocalTask] = WORKER_MANAGER.current_task
        queued_cnt = len(WORKER_MANAGER.queued_tasks)

    if task is None:
        if queued_cnt > 0:
            card_html = (
                f'<div class="ff-prog-card running">'
                f'  <div class="ff-prog-top">'
                f'    <span class="ff-prog-title">⏳ 正在启动下一条排队任务...</span>'
                f'    <span class="ff-pill ff-pill-active">队列剩余 {queued_cnt} 条</span>'
                f'  </div>'
                f'  <div class="ff-prog-track"><div class="ff-prog-fill" style="width:2%"></div></div>'
                f'  <div class="ff-prog-detail">正在准备独立工作目录与任务参数...</div>'
                f'</div>'
            )
            return card_html, False

        card_html = (
            '<div class="ff-prog-card">'
            '  <div class="ff-prog-top">'
            '    <span class="ff-prog-title">☕ 本机当前空闲</span>'
            '    <span class="ff-pill ff-pill-idle">等待新任务</span>'
            '  </div>'
            '  <div class="ff-prog-track"><div class="ff-prog-fill" style="width:0%"></div></div>'
            '  <div class="ff-prog-detail">在左侧勾选需求领取，或开启【自动接单】即可开始自动换脸上传。</div>'
            '</div>'
        )
        return card_html, False

    pct = max(0.0, min(100.0, float(task.progress_pct)))
    card_html = (
        f'<div class="ff-prog-card running">'
        f'  <div class="ff-prog-top">'
        f'    <div>'
        f'      <span class="ff-pill ff-pill-active">{html.escape(task.local_status)}</span> '
        f'      <span class="ff-prog-title" style="margin-left:0.35rem;">{html.escape(task.task_id)}</span>'
        f'      <span class="ff-step-hint" style="margin-left:0.4rem;">第 {task.initial_row_number} 行 · {html.escape(task.preset_name)}</span>'
        f'    </div>'
        f'    <span class="ff-prog-pct">{pct:.1f}%</span>'
        f'  </div>'
        f'  <div class="ff-prog-track"><div class="ff-prog-fill" style="width:{pct:.1f}%"></div></div>'
        f'  <div class="ff-prog-detail">{html.escape(task.progress_detail or "正在处理...")}</div>'
        f'</div>'
    )
    return card_html, True


def _build_local_queue_table() -> List[List[Any]]:
    with WORKER_MANAGER._lock:
        running = WORKER_MANAGER.current_task
        queued = list(WORKER_MANAGER.queued_tasks)
        history = list(WORKER_MANAGER.history_tasks)

    items: List[LocalTask] = []
    if running:
        items.append(running)
    items.extend(queued)
    items.extend(history[:25])

    rows: List[List[Any]] = []
    for t in items:
        rows.append([
            t.task_id,
            t.local_status,
            f'{t.progress_pct:.0f}%',
            t.preset_name,
            t.progress_detail,
            _md_link(t.output_url, '打开成品')
        ])
    return rows


def _build_queued_choices() -> Tuple[List[Tuple[str, str]], str]:
    with WORKER_MANAGER._lock:
        queued = list(WORKER_MANAGER.queued_tasks)
    choices = [
        (f'第 {t.initial_row_number} 行 · {t.preset_name}（{t.task_id}）', t.task_id)
        for t in queued
    ]
    sig = ','.join(t.task_id for t in queued)
    return choices, sig


def _build_logs_text() -> str:
    with WORKER_MANAGER._lock:
        lines = list(WORKER_MANAGER.logs)
    return '\n'.join(lines[-50:])


def render() -> None:
    global SUMMARY_HTML, BANNER_HTML
    global BACKUP_ALERT_GROUP, BACKUP_ALERT_HTML, RETRY_BACKUP_UPLOAD_BUTTON
    global CONFIG_ACCORDION, WORKER_NAME_TEXTBOX, SPREADSHEET_URL_TEXTBOX, SHEET_TITLE_DROPDOWN, DRIVE_FOLDER_TEXTBOX
    global CLIENT_SECRET_FILE, AUTH_STATUS_TEXTBOX, AUTHORIZE_BUTTON, LOGOUT_AUTH_BUTTON
    global AUTH_PENDING_GROUP, AUTH_LINK_HTML, MANUAL_AUTH_CODE_TEXTBOX, MANUAL_AUTH_SUBMIT_BUTTON, SAVE_CONNECT_BUTTON
    global REFRESH_BUTTON, RECOVER_OWNED_BUTTON, AUTO_CLAIM_CHECKBOX, AUTO_POLL_NOW_BUTTON
    global SHOW_FAILED_CHECKBOX, CLAIMABLE_EMPTY_HTML, CLAIMABLE_ACTION_COL, CLAIMABLE_DATAFRAME, CLAIMABLE_CHECKBOX_GROUP, SELECT_ALL_BUTTON, CLEAR_SELECT_BUTTON, CLAIM_SELECTED_BUTTON, ALL_ROWS_DATAFRAME
    global CURRENT_TASK_HTML, ABORT_CURRENT_BUTTON, OPEN_LOCAL_OUTPUTS_BUTTON, QUEUE_GROUP, QUEUED_SELECT_DROPDOWN, RELEASE_QUEUE_BUTTON, LOCAL_QUEUE_DATAFRAME, WORKER_LOG_TEXTBOX
    global SEEN_REVISION_STATE, SEEN_QUEUE_SIG_STATE, SEEN_UI_VERSION_STATE, SEEN_AUTO_ENABLED_STATE, SEEN_AUTH_SIG_STATE, LIVE_TIMER

    WORKER_MANAGER.ensure_background_threads()
    WORKER_MANAGER.trigger_startup_autoload_once()
    cfg = WORKER_MANAGER.config
    is_configured = _is_connection_ready()
    should_open_config = (not is_configured) or WORKER_MANAGER.last_connect_failed
    init_q_choices, init_q_sig = _build_queued_choices()
    init_backup_vis, init_backup_html = _build_backup_alert_state()
    init_curr_html, init_is_running = _build_current_task_state()

    with WORKER_MANAGER._lock:
        init_rows = list(WORKER_MANAGER.cached_sheet_rows)
        init_auth_in_prog = WORKER_MANAGER.auth_in_progress
    init_claim_table, init_claim_choices = _build_claimable_view(init_rows, include_failed=False)
    init_has_claimable = len(init_claim_choices) > 0

    SEEN_REVISION_STATE = gradio.State(value=-1)
    SEEN_QUEUE_SIG_STATE = gradio.State(value=init_q_sig)
    SEEN_UI_VERSION_STATE = gradio.State(value=-1)
    SEEN_AUTO_ENABLED_STATE = gradio.State(value=WORKER_MANAGER.auto_claim_enabled)
    SEEN_AUTH_SIG_STATE = gradio.State(value=_auth_sync_signature())
    LIVE_TIMER = gradio.Timer(value=2.0)

    # 1. Top Dashboard Strip + Notice Banner
    SUMMARY_HTML = gradio.HTML(value=_build_dashboard_html(), padding=False)
    BANNER_HTML = gradio.HTML(value=_build_banner_html(WORKER_MANAGER.last_banner), padding=False)

    # 2. Contextual Backup Upload Bar (hidden unless local un-uploaded backups exist)
    with gradio.Column(visible=init_backup_vis, elem_classes=['ff-backup-alert']) as BACKUP_ALERT_GROUP:
        with gradio.Row():
            with gradio.Column(scale=4, min_width=240):
                BACKUP_ALERT_HTML = gradio.HTML(value=init_backup_html, padding=False)
            with gradio.Column(scale=1, min_width=160):
                RETRY_BACKUP_UPLOAD_BUTTON = gradio.Button('☁️ 立即补传保底成品', variant='primary', size='sm')

    # 3. Guided 3-Step Setup Drawer (balanced 3-card horizontal layout, auto-collapses once connected)
    with gradio.Accordion(
        '⚙️ 账号与云表格连接设置（首次使用按 1 → 2 → 3 步完成，连接后自动收起）',
        open=should_open_config
    ) as CONFIG_ACCORDION:
        with gradio.Row():
            with gradio.Column(scale=1, min_width=230, elem_classes=['ff-setup-card']):
                gradio.HTML(_step_header_html('第 1 步', '设置本机名称', '必填'), padding=False)
                WORKER_NAME_TEXTBOX = gradio.Textbox(
                    label='本机工位名称（自定义填写，不读电脑名）',
                    value=cfg.worker_name,
                    placeholder='例如：Alex / Worker-01',
                    max_lines=1
                )

            with gradio.Column(scale=1, min_width=260, elem_classes=['ff-setup-card']):
                gradio.HTML(_step_header_html('第 2 步', '登录 Google 账号', '首次上传一次'), padding=False)
                CLIENT_SECRET_FILE = gradio.File(
                    label=_secret_file_label(),
                    file_types=['.json'],
                    type='filepath',
                    height=96
                )
                AUTH_STATUS_TEXTBOX = gradio.Textbox(
                    label='当前授权状态',
                    value=WORKER_MANAGER.get_auth_status_text(),
                    interactive=False,
                    max_lines=1
                )
                with gradio.Row():
                    AUTHORIZE_BUTTON = gradio.Button('🔑 登录 Google', variant='primary', size='sm', scale=2, min_width=96)
                    LOGOUT_AUTH_BUTTON = gradio.Button('解绑账号', size='sm', scale=1, min_width=68)

                with gradio.Column(visible=init_auth_in_prog, elem_classes=['ff-auth-helper']) as AUTH_PENDING_GROUP:
                    AUTH_LINK_HTML = gradio.HTML(value=_build_auth_link_html(), padding=False)
                    with gradio.Accordion('备用：网页提示无法访问？点此粘贴网址', open=False):
                        MANUAL_AUTH_CODE_TEXTBOX = gradio.Textbox(
                            label='复制点击允许后浏览器地址栏整串网址粘贴到此',
                            placeholder='http://127.0.0.1:xxxxx/?state=...&code=...',
                            max_lines=1
                        )
                        MANUAL_AUTH_SUBMIT_BUTTON = gradio.Button('✅ 确认粘贴网址完成授权', variant='primary', size='sm')

            with gradio.Column(scale=1, min_width=250, elem_classes=['ff-setup-card']):
                gradio.HTML(_step_header_html('第 3 步', '绑定表格并连接', '粘贴网址保存'), padding=False)
                SPREADSHEET_URL_TEXTBOX = gradio.Textbox(
                    label='Google 云表格完整链接',
                    value=cfg.spreadsheet_url,
                    placeholder='https://docs.google.com/spreadsheets/d/...',
                    max_lines=1
                )
                SAVE_CONNECT_BUTTON = gradio.Button('✅ 保存配置并连接表格', variant='primary', size='lg')

        with gradio.Accordion('可选高级项（指定工作表页签 / 自定义网盘目录 / 表头说明 — 默认无需修改）', open=False):
            with gradio.Row():
                initial_sheet_choices = [cfg.sheet_title] if cfg.sheet_title else []
                SHEET_TITLE_DROPDOWN = gradio.Dropdown(
                    label='指定工作表页签（留空自动识别）',
                    choices=initial_sheet_choices,
                    value=cfg.sheet_title or None,
                    allow_custom_value=True
                )
                DRIVE_FOLDER_TEXTBOX = gradio.Textbox(
                    label='指定成品上传 Google Drive 文件夹链接（留空自动创建“FaceFusion_自动换脸成品”）',
                    value=cfg.drive_folder_url,
                    placeholder='https://drive.google.com/drive/folders/...',
                    max_lines=1
                )
            gradio.Markdown(
                '💡 **表格格式说明**：第 1 行包含 **`源人脸图片`**、**`目标视频`**（可选 **`场景预设`**）即可；'
                '系统会自动维护 `任务编号`、`任务状态`、`领取节点`、`成品链接`、`处理备注`，后续任意挪列、插行或排序均不影响回写。'
            )

    # 4. Clean 2-Column Daily Workspace
    with gradio.Row():
        # Left Column: Claiming Tasks
        with gradio.Column(scale=6, min_width=360):
            with gradio.Column(elem_classes=['ff-card-panel']):
                gradio.HTML(_step_header_html('1', '领取表格需求', '可勾选批量领取或开启自动接单'), padding=False)
                with gradio.Row():
                    REFRESH_BUTTON = gradio.Button('🔄 刷新需求', size='sm', scale=1, min_width=92)
                    AUTO_CLAIM_CHECKBOX = gradio.Checkbox(
                        label=f'⚡ 自动接单（每 {AUTO_CLAIM_INTERVAL_MINUTES} 分钟巡检，上限 1 制作 + 1 排队）',
                        value=WORKER_MANAGER.auto_claim_enabled,
                        scale=3,
                        min_width=180
                    )

                CLAIMABLE_EMPTY_HTML = gradio.HTML(
                    value=_build_empty_claimable_html(init_has_claimable),
                    visible=not init_has_claimable,
                    padding=False
                )

                # Contextual claim checklist + buttons: only visible when claimable rows exist!
                with gradio.Column(visible=init_has_claimable) as CLAIMABLE_ACTION_COL:
                    CLAIMABLE_CHECKBOX_GROUP = gradio.CheckboxGroup(
                        label='勾选要领取的需求（若点击瞬间已被同事抢先领取会自动跳过）',
                        choices=init_claim_choices,
                        value=[]
                    )
                    with gradio.Row():
                        CLAIM_SELECTED_BUTTON = gradio.Button('🚀 领取选中需求', variant='primary', size='sm', scale=3, min_width=115)
                        SELECT_ALL_BUTTON = gradio.Button('全选', size='sm', scale=1, min_width=58)
                        CLEAR_SELECT_BUTTON = gradio.Button('清空', size='sm', scale=1, min_width=58)

                    with gradio.Accordion('🔗 预览待领素材链接（查看源图 / 原片）', open=False):
                        CLAIMABLE_DATAFRAME = gradio.Dataframe(
                            headers=['行号', '场景预设', '源人脸图片', '目标视频'],
                            datatype=['number', 'str', 'markdown', 'markdown'],
                            value=init_claim_table,
                            interactive=False,
                            wrap=True
                        )

                SHOW_FAILED_CHECKBOX = gradio.Checkbox(
                    label='同时显示“制作失败”的需求以便勾选重做',
                    value=False
                )

        # Right Column: Progress & Records
        with gradio.Column(scale=6, min_width=360):
            with gradio.Column(elem_classes=['ff-card-panel']):
                gradio.HTML(_step_header_html('2', '本机制作进度', f'本机自动留存最近 {MAX_RECENT_LOCAL_OUTPUTS} 条成品'), padding=False)
                CURRENT_TASK_HTML = gradio.HTML(value=init_curr_html, padding=False)

                with gradio.Row():
                    OPEN_LOCAL_OUTPUTS_BUTTON = gradio.Button(
                        f'📂 打开本地成品目录（最近{MAX_RECENT_LOCAL_OUTPUTS}条）',
                        size='sm',
                        scale=2,
                        min_width=160
                    )
                    ABORT_CURRENT_BUTTON = gradio.Button(
                        '⏹ 中止当前任务并退回',
                        variant='stop',
                        size='sm',
                        scale=1,
                        min_width=130,
                        visible=init_is_running
                    )

                # Contextual Queue Drawer: ONLY shown when there are waiting tasks in local queue
                with gradio.Column(visible=bool(init_q_choices)) as QUEUE_GROUP:
                    with gradio.Row():
                        QUEUED_SELECT_DROPDOWN = gradio.Dropdown(
                            label='等待排队的任务（不选直接点退回 = 全部退回表格）',
                            choices=init_q_choices,
                            value=[],
                            multiselect=True,
                            scale=3,
                            min_width=180
                        )
                        RELEASE_QUEUE_BUTTON = gradio.Button('↩️ 退回排队', size='sm', scale=1, min_width=90)

                with gradio.Tabs(elem_classes=['ff-sub-tabs']):
                    with gradio.Tab('📋 本机记录'):
                        LOCAL_QUEUE_DATAFRAME = gradio.Dataframe(
                            headers=['任务编号', '状态', '进度', '预设', '实时详情', '成品'],
                            datatype=['str', 'str', 'str', 'str', 'str', 'markdown'],
                            value=_build_local_queue_table(),
                            interactive=False,
                            wrap=True
                        )

                    with gradio.Tab('📊 全表总览'):
                        ALL_ROWS_DATAFRAME = gradio.Dataframe(
                            headers=['行号', '任务编号', '状态', '工位', '预设', '成品', '备注'],
                            datatype=['number', 'str', 'str', 'str', 'str', 'markdown', 'str'],
                            value=_build_all_rows_table(init_rows),
                            interactive=False,
                            wrap=True
                        )

                    with gradio.Tab('🛠️ 日志维护'):
                        with gradio.Row():
                            RECOVER_OWNED_BUTTON = gradio.Button('🔄 恢复本机中断任务', size='sm', min_width=120)
                            AUTO_POLL_NOW_BUTTON = gradio.Button('⚡ 立即补单巡检', size='sm', min_width=110)
                        WORKER_LOG_TEXTBOX = gradio.Textbox(
                            label='运行日志（表格端仅写入“已领取制作中”与最终结果）',
                            value=_build_logs_text(),
                            lines=7,
                            interactive=False
                        )


def _full_sheet_ui_tuple(include_failed: bool = False) -> Tuple[Any, ...]:
    with WORKER_MANAGER._lock:
        rows = list(WORKER_MANAGER.cached_sheet_rows)
        rev = WORKER_MANAGER.snapshot_revision
        ui_ver = WORKER_MANAGER.ui_state_version
        banner = WORKER_MANAGER.last_banner
    claim_table, claim_choices = _build_claimable_view(rows, include_failed=include_failed)
    has_claimable = len(claim_choices) > 0
    all_table = _build_all_rows_table(rows)
    curr_html, is_running = _build_current_task_state()
    q_choices, q_sig = _build_queued_choices()
    backup_vis, backup_html = _build_backup_alert_state()
    return (
        _build_dashboard_html(),
        _build_banner_html(banner),
        gradio.update(visible=backup_vis),
        backup_html,
        gradio.update(value=_build_empty_claimable_html(has_claimable), visible=not has_claimable),
        gradio.update(visible=has_claimable),
        gradio.update(choices=claim_choices, value=[]),
        claim_table,
        all_table,
        curr_html,
        gradio.update(visible=is_running),
        gradio.update(visible=bool(q_choices)),
        gradio.update(choices=q_choices, value=[]),
        _build_local_queue_table(),
        _build_logs_text(),
        rev,
        q_sig,
        ui_ver
    )


def on_upload_secret_file(secret_file: Optional[str]) -> Tuple[Any, ...]:
    if not secret_file:
        return tuple(gradio.skip() for _ in range(7))
    try:
        mode, msg = WORKER_MANAGER.import_uploaded_secret_file(secret_file)
        if mode == 'imported_token':
            email = WORKER_MANAGER.oauth.read_token_data().get('authorized_email') or '已导入凭证'
            msg = WORKER_MANAGER._try_autoconnect_after_auth(email)
        gradio.Info(msg)
    except Exception as exc:
        err = str(exc)
        WORKER_MANAGER.set_banner(err)
        gradio.Warning(err)

    return (
        WORKER_MANAGER.get_auth_status_text(),
        gradio.update(visible=WORKER_MANAGER.auth_in_progress),
        _build_auth_link_html(),
        _build_banner_html(WORKER_MANAGER.last_banner),
        _build_logs_text(),
        gradio.update(value=None, label=_secret_file_label()),
        _auth_sync_signature()
    )


def on_authorize(
    worker_name: str,
    spreadsheet_url: str,
    sheet_title: Optional[str],
    drive_folder_url: str,
    secret_file: Optional[str]
) -> Tuple[Any, ...]:
    try:
        WORKER_MANAGER.update_config(
            worker_name,
            spreadsheet_url,
            sheet_title or '',
            drive_folder_url,
            uploaded_secret_file=secret_file
        )
        auth_url, msg = WORKER_MANAGER.start_google_auth_session(
            uploaded_secret_file=None,
            open_browser=True
        )
        if auth_url:
            gradio.Info('已发起 Google 登录！请在弹出的网页点击“允许”（若未自动弹出，请点第 2 步下方的蓝色按钮打开）。')
        else:
            gradio.Info(msg)
    except Exception as exc:
        err = str(exc)
        WORKER_MANAGER.set_banner(err)
        gradio.Warning(err)

    return (
        WORKER_MANAGER.get_auth_status_text(),
        gradio.update(visible=WORKER_MANAGER.auth_in_progress),
        _build_auth_link_html(),
        gradio.update(value=''),
        _build_banner_html(WORKER_MANAGER.last_banner),
        _build_logs_text(),
        gradio.update(value=None, label=_secret_file_label()),
        _auth_sync_signature()
    )


def on_manual_auth_submit(
    raw_code_or_url: str,
    worker_name: str,
    spreadsheet_url: str,
    sheet_title: Optional[str],
    drive_folder_url: str,
    include_failed: bool
) -> Tuple[Any, ...]:
    try:
        WORKER_MANAGER.update_config(
            worker_name,
            spreadsheet_url,
            sheet_title or '',
            drive_folder_url,
            uploaded_secret_file=None
        )
        msg = WORKER_MANAGER.complete_google_auth_manual(raw_code_or_url)
        gradio.Info(msg)
        code_box_upd: Any = gradio.update(value='')
    except Exception as exc:
        err = str(exc)
        WORKER_MANAGER.set_banner(err)
        gradio.Warning(err)
        code_box_upd = gradio.skip()

    with WORKER_MANAGER._lock:
        connected_ok = bool(WORKER_MANAGER.spreadsheet_title) and not WORKER_MANAGER.last_connect_failed
        titles = list(WORKER_MANAGER.available_sheets)
        sel_sheet = WORKER_MANAGER.config.sheet_title

    sheet_dd_upd = gradio.update(choices=titles, value=sel_sheet or None) if connected_ok else gradio.skip()
    accordion_upd = gradio.Accordion(open=not connected_ok)

    return (
        WORKER_MANAGER.get_auth_status_text(),
        gradio.update(visible=WORKER_MANAGER.auth_in_progress),
        _build_auth_link_html(),
        code_box_upd,
        sheet_dd_upd,
        accordion_upd,
        _auth_sync_signature(),
        *_full_sheet_ui_tuple(include_failed=bool(include_failed))
    )


def on_logout_auth(include_failed: bool) -> Tuple[Any, ...]:
    try:
        msg = WORKER_MANAGER.logout_google_account()
        gradio.Info(msg)
    except Exception as exc:
        err = str(exc)
        WORKER_MANAGER.set_banner(err)
        gradio.Warning(err)

    return (
        WORKER_MANAGER.get_auth_status_text(),
        gradio.update(visible=False),
        '',
        gradio.update(value=''),
        gradio.update(value=False),
        gradio.Accordion(open=True),
        _auth_sync_signature(),
        *_full_sheet_ui_tuple(include_failed=bool(include_failed))
    )


def on_save_and_connect(
    worker_name: str,
    spreadsheet_url: str,
    sheet_title: Optional[str],
    drive_folder_url: str,
    secret_file: Optional[str],
    include_failed: bool
) -> Tuple[Any, ...]:
    clean_worker = (worker_name or '').strip()
    clean_url = (spreadsheet_url or '').strip()

    # Always persist any non-empty user inputs & uploaded JSON first so nothing typed is ever lost
    try:
        WORKER_MANAGER.update_config(
            clean_worker,
            clean_url,
            sheet_title or '',
            drive_folder_url,
            uploaded_secret_file=secret_file
        )
    except Exception as exc:
        err = str(exc)
        WORKER_MANAGER.set_banner(err)
        gradio.Warning(err)
        return (
            WORKER_MANAGER.get_auth_status_text(),
            gradio.update(visible=WORKER_MANAGER.auth_in_progress),
            _build_auth_link_html(),
            gradio.skip(),
            gradio.update(value=None, label=_secret_file_label()),
            gradio.Accordion(open=True),
            _auth_sync_signature(),
            *_full_sheet_ui_tuple(include_failed=bool(include_failed))
        )

    if not clean_worker:
        msg = '请先填写第 1 步“本机工位名称”（例如：Alex、Worker-01），保存后才能连接读取表格。'
        WORKER_MANAGER.set_banner(msg)
        gradio.Warning(msg)
        return (
            WORKER_MANAGER.get_auth_status_text(),
            gradio.update(visible=WORKER_MANAGER.auth_in_progress),
            _build_auth_link_html(),
            gradio.skip(),
            gradio.update(value=None, label=_secret_file_label()),
            gradio.Accordion(open=True),
            _auth_sync_signature(),
            *_full_sheet_ui_tuple(include_failed=bool(include_failed))
        )

    try:
        parse_spreadsheet_ref(clean_url)
    except ValueError as exc:
        err = str(exc)
        WORKER_MANAGER.set_banner(err)
        gradio.Warning(err)
        return (
            WORKER_MANAGER.get_auth_status_text(),
            gradio.update(visible=WORKER_MANAGER.auth_in_progress),
            _build_auth_link_html(),
            gradio.skip(),
            gradio.update(value=None, label=_secret_file_label()),
            gradio.Accordion(open=True),
            _auth_sync_signature(),
            *_full_sheet_ui_tuple(include_failed=bool(include_failed))
        )

    # Smooth Step 2 bridge: if user hasn't finished Google OAuth yet, help them instead of throwing an error!
    if not WORKER_MANAGER.oauth.has_valid_refresh_token():
        if WORKER_MANAGER.has_client_secret_file():
            try:
                if not WORKER_MANAGER.auth_in_progress:
                    WORKER_MANAGER.start_google_auth_session(uploaded_secret_file=None, open_browser=True)
                msg = '工位名称与表格链接已保存！已为您打开 Google 登录页，网页点击“允许”后将自动连接表格（若未弹出请点第 2 步蓝色按钮）。'
                WORKER_MANAGER.set_banner(msg)
                gradio.Info(msg)
            except Exception as exc:
                err = str(exc)
                WORKER_MANAGER.set_banner(err)
                gradio.Warning(err)
        else:
            msg = '工位名称与云表格链接已保存！请继续完成【第 2 步】：上传客户端 JSON 文件并点击“🔑 登录 Google”。'
            WORKER_MANAGER.set_banner(msg)
            gradio.Warning(msg)

        return (
            WORKER_MANAGER.get_auth_status_text(),
            gradio.update(visible=WORKER_MANAGER.auth_in_progress),
            _build_auth_link_html(),
            gradio.skip(),
            gradio.update(value=None, label=_secret_file_label()),
            gradio.Accordion(open=True),
            _auth_sync_signature(),
            *_full_sheet_ui_tuple(include_failed=bool(include_failed))
        )

    try:
        doc_title, titles, selected_sheet = WORKER_MANAGER.test_and_load_sheets()
        with WORKER_MANAGER._lock:
            active_cnt = WORKER_MANAGER.active_task_count()
        if active_cnt == 0:
            WORKER_MANAGER.set_banner(f'已成功连接表格《{doc_title}》的工作表“{selected_sheet}”。')
        gradio.Info(f'连接成功：《{doc_title} / {selected_sheet}》')
        sheet_dd_upd: Any = gradio.update(choices=titles, value=selected_sheet)
        accordion_upd = gradio.Accordion(open=False)
    except Exception as exc:
        err = f'连接云表格未成功：{exc}'
        with WORKER_MANAGER._lock:
            WORKER_MANAGER.last_connect_failed = True
        WORKER_MANAGER.set_banner(err)
        gradio.Warning(err)
        sheet_dd_upd = gradio.skip()
        accordion_upd = gradio.Accordion(open=True)

    return (
        WORKER_MANAGER.get_auth_status_text(),
        gradio.update(visible=WORKER_MANAGER.auth_in_progress),
        _build_auth_link_html(),
        sheet_dd_upd,
        gradio.update(value=None, label=_secret_file_label()),
        accordion_upd,
        _auth_sync_signature(),
        *_full_sheet_ui_tuple(include_failed=bool(include_failed))
    )


def on_refresh_sheet(include_failed: bool) -> Tuple[Any, ...]:
    if not _is_connection_ready():
        prompt = _missing_setup_prompt()
        WORKER_MANAGER.set_banner(prompt)
        gradio.Warning(prompt)
        return (gradio.Accordion(open=True), *_full_sheet_ui_tuple(include_failed=bool(include_failed)))
    try:
        rows = WORKER_MANAGER.refresh_sheet_tasks()
        claim_cnt = sum(1 for r in rows if r.is_claimable)
        WORKER_MANAGER.set_banner(f'表格刷新成功：当前共有 {claim_cnt} 条待领取需求。')
    except Exception as exc:
        err = f'刷新表格失败：{exc}'
        WORKER_MANAGER.set_banner(err)
        gradio.Warning(err)
    return (gradio.skip(), *_full_sheet_ui_tuple(include_failed=bool(include_failed)))


def on_recover_owned(include_failed: bool) -> Tuple[Any, ...]:
    if not _is_connection_ready():
        prompt = _missing_setup_prompt()
        WORKER_MANAGER.set_banner(prompt)
        gradio.Warning(prompt)
        return (gradio.Accordion(open=True), *_full_sheet_ui_tuple(include_failed=bool(include_failed)))
    try:
        WORKER_MANAGER.refresh_sheet_tasks()
        _, msg = WORKER_MANAGER.recover_owned_in_progress_tasks(quiet_if_empty=False)
        gradio.Info(msg)
    except Exception as exc:
        err = f'恢复任务失败：{exc}'
        WORKER_MANAGER.set_banner(err)
        gradio.Warning(err)
    return (gradio.skip(), *_full_sheet_ui_tuple(include_failed=bool(include_failed)))


def on_toggle_show_failed(include_failed: bool) -> Tuple[Any, Any, Any, Any]:
    with WORKER_MANAGER._lock:
        rows = list(WORKER_MANAGER.cached_sheet_rows)
    claim_table, claim_choices = _build_claimable_view(rows, include_failed=bool(include_failed))
    has_claimable = len(claim_choices) > 0
    return (
        gradio.update(value=_build_empty_claimable_html(has_claimable), visible=not has_claimable),
        gradio.update(visible=has_claimable),
        gradio.update(choices=claim_choices, value=[]),
        claim_table,
    )


def on_select_all(include_failed: bool) -> Any:
    with WORKER_MANAGER._lock:
        rows = list(WORKER_MANAGER.cached_sheet_rows)
    _, choices = _build_claimable_view(rows, include_failed=bool(include_failed))
    return gradio.update(value=[val for _, val in choices])


def on_clear_selection() -> Any:
    return gradio.update(value=[])


def on_claim_selected(selected_tokens: List[str], include_failed: bool) -> Tuple[Any, ...]:
    if not _is_connection_ready():
        prompt = _missing_setup_prompt()
        WORKER_MANAGER.set_banner(prompt)
        gradio.Warning(prompt)
        return (gradio.Accordion(open=True), *_full_sheet_ui_tuple(include_failed=bool(include_failed)))
    try:
        added_cnt, conflicts = WORKER_MANAGER.claim_manual_selection(
            selected_tokens,
            allow_retry_failed=bool(include_failed)
        )
        if conflicts:
            gradio.Warning('部分需求已被他人领取：' + '；'.join(conflicts))
        elif added_cnt > 0:
            gradio.Info(f'成功领取 {added_cnt} 条需求，已加入本机制作队列！')
    except Exception as exc:
        err = str(exc)
        WORKER_MANAGER.set_banner(err)
        gradio.Warning(err)
    return (gradio.skip(), *_full_sheet_ui_tuple(include_failed=bool(include_failed)))


def on_toggle_auto_claim(enabled: bool) -> Tuple[Any, Any, str, str, str, bool]:
    if enabled and not _is_connection_ready():
        prompt = _missing_setup_prompt()
        WORKER_MANAGER.set_banner(prompt)
        gradio.Warning(prompt)
        return (
            gradio.update(value=False),
            gradio.Accordion(open=True),
            _build_dashboard_html(),
            _build_banner_html(prompt),
            _build_logs_text(),
            False
        )
    try:
        msg = WORKER_MANAGER.set_auto_claim(enabled)
        cur_auto = WORKER_MANAGER.auto_claim_enabled
        return (
            gradio.update(value=cur_auto),
            gradio.skip(),
            _build_dashboard_html(),
            _build_banner_html(msg),
            _build_logs_text(),
            cur_auto
        )
    except Exception as exc:
        err = str(exc)
        WORKER_MANAGER.set_banner(err)
        gradio.Warning(err)
        return (
            gradio.update(value=False),
            gradio.Accordion(open=True),
            _build_dashboard_html(),
            _build_banner_html(err),
            _build_logs_text(),
            False
        )


def on_poll_auto_now(include_failed: bool) -> Tuple[Any, ...]:
    if not _is_connection_ready():
        prompt = _missing_setup_prompt()
        WORKER_MANAGER.set_banner(prompt)
        gradio.Warning(prompt)
        return (gradio.Accordion(open=True), *_full_sheet_ui_tuple(include_failed=bool(include_failed)))
    try:
        msg = WORKER_MANAGER.trigger_auto_claim_once()
        if '异常' in msg or '跳过' in msg:
            gradio.Info(msg)
    except Exception as exc:
        err = str(exc)
        WORKER_MANAGER.set_banner(err)
        gradio.Warning(err)
    return (gradio.skip(), *_full_sheet_ui_tuple(include_failed=bool(include_failed)))


def on_abort_current() -> Tuple[str, str]:
    try:
        msg = WORKER_MANAGER.abort_current_task(return_to_sheet=True)
        gradio.Warning(msg)
    except Exception as exc:
        msg = f'中止当前任务异常：{exc}'
        WORKER_MANAGER.set_banner(msg)
        gradio.Warning(msg)
    return _build_banner_html(msg), _build_logs_text()


def on_release_queued(selected_queue_ids: Optional[List[str]], include_failed: bool) -> Tuple[Any, ...]:
    try:
        msg = WORKER_MANAGER.release_queued_tasks(selected_queue_ids)
        gradio.Info(msg)
    except Exception as exc:
        err = str(exc)
        WORKER_MANAGER.set_banner(err)
        gradio.Warning(err)
    return _full_sheet_ui_tuple(include_failed=bool(include_failed))


def on_retry_backup_upload(include_failed: bool) -> Tuple[Any, ...]:
    try:
        msg = WORKER_MANAGER.retry_saved_uploads()
        gradio.Info(msg)
    except Exception as exc:
        err = str(exc)
        WORKER_MANAGER.set_banner(err)
        gradio.Warning(err)
    return _full_sheet_ui_tuple(include_failed=bool(include_failed))


def on_open_local_outputs() -> Tuple[str, str]:
    try:
        msg = WORKER_MANAGER.open_local_outputs_folder()
        gradio.Info(msg)
        return _build_banner_html(msg), _build_logs_text()
    except Exception as exc:
        err = f'打开本地成品目录失败：{exc}'
        WORKER_MANAGER.set_banner(err)
        gradio.Warning(err)
        return _build_banner_html(err), _build_logs_text()


def on_timer_tick(
    seen_rev: int,
    seen_q_sig: str,
    seen_ui_ver: int,
    seen_auto_enabled: bool,
    seen_auth_sig: str,
    include_failed: bool
) -> Tuple[Any, ...]:
    """
    Lightweight 2s local UI refresh.
    - Returns pure `gradio.skip()` across all widgets when idle and `ui_state_version` is unchanged!
    - Syncs Step 2 OAuth completion & automatic sheet connection without overwriting user inputs!
    """
    with WORKER_MANAGER._lock:
        current_rev = WORKER_MANAGER.snapshot_revision
        current_ui_ver = WORKER_MANAGER.ui_state_version
        current_auto = WORKER_MANAGER.auto_claim_enabled
        rows = list(WORKER_MANAGER.cached_sheet_rows)
        banner = WORKER_MANAGER.last_banner
        in_prog = WORKER_MANAGER.auth_in_progress
        doc_title = WORKER_MANAGER.spreadsheet_title
        avail_sheets = list(WORKER_MANAGER.available_sheets)
        sel_sheet = WORKER_MANAGER.config.sheet_title
        conn_failed = WORKER_MANAGER.last_connect_failed

    auto_checkbox_update = gradio.update(value=current_auto) if current_auto != seen_auto_enabled else gradio.skip()
    current_auth_sig = _auth_sync_signature()

    if current_auth_sig != seen_auth_sig:
        auth_status_upd: Any = WORKER_MANAGER.get_auth_status_text()
        auth_group_upd: Any = gradio.update(visible=in_prog)
        auth_link_upd: Any = _build_auth_link_html()
        if doc_title and not conn_failed:
            choices_list = avail_sheets or ([sel_sheet] if sel_sheet else [])
            sheet_dd_upd: Any = gradio.update(choices=choices_list, value=sel_sheet or None)
            accordion_upd: Any = gradio.Accordion(open=False)
        else:
            sheet_dd_upd = gradio.skip()
            accordion_upd = gradio.skip()
    else:
        auth_status_upd = gradio.skip()
        auth_group_upd = gradio.skip()
        auth_link_upd = gradio.skip()
        sheet_dd_upd = gradio.skip()
        accordion_upd = gradio.skip()

    # Fast path when completely idle (no state, log, progress, auth, or sheet change since last tick)
    if current_ui_ver == seen_ui_ver and current_rev == seen_rev and current_auth_sig == seen_auth_sig:
        summary_update = _build_dashboard_html() if current_auto else gradio.skip()
        return (
            summary_update,
            gradio.skip(),
            gradio.skip(),
            gradio.skip(),
            gradio.skip(),
            gradio.skip(),
            gradio.skip(),
            gradio.skip(),
            gradio.skip(),
            gradio.skip(),
            gradio.skip(),
            gradio.skip(),
            gradio.skip(),
            gradio.skip(),
            gradio.skip(),
            seen_rev,
            seen_q_sig,
            seen_ui_ver,
            auto_checkbox_update,
            current_auto,
            auth_status_upd,
            auth_group_upd,
            auth_link_upd,
            sheet_dd_upd,
            accordion_upd,
            current_auth_sig
        )

    curr_html, is_running = _build_current_task_state()
    summary_html = _build_dashboard_html()
    banner_html = _build_banner_html(banner)
    backup_vis, backup_html = _build_backup_alert_state()
    queue_table = _build_local_queue_table()
    logs_txt = _build_logs_text()
    q_choices, q_sig = _build_queued_choices()
    q_group_update = gradio.update(visible=bool(q_choices))
    q_dropdown_update = gradio.update(choices=q_choices, value=[]) if q_sig != seen_q_sig else gradio.skip()

    if current_rev != seen_rev:
        claim_table, claim_choices = _build_claimable_view(rows, include_failed=bool(include_failed))
        has_claimable = len(claim_choices) > 0
        all_table = _build_all_rows_table(rows)
        return (
            summary_html,
            banner_html,
            gradio.update(visible=backup_vis),
            backup_html,
            gradio.update(value=_build_empty_claimable_html(has_claimable), visible=not has_claimable),
            gradio.update(visible=has_claimable),
            gradio.update(choices=claim_choices, value=[]),
            claim_table,
            all_table,
            curr_html,
            gradio.update(visible=is_running),
            q_group_update,
            q_dropdown_update,
            queue_table,
            logs_txt,
            current_rev,
            q_sig,
            current_ui_ver,
            auto_checkbox_update,
            current_auto,
            auth_status_upd,
            auth_group_upd,
            auth_link_upd,
            sheet_dd_upd,
            accordion_upd,
            current_auth_sig
        )

    return (
        summary_html,
        banner_html,
        gradio.update(visible=backup_vis),
        backup_html,
        gradio.skip(),
        gradio.skip(),
        gradio.skip(),
        gradio.skip(),
        gradio.skip(),
        curr_html,
        gradio.update(visible=is_running),
        q_group_update,
        q_dropdown_update,
        queue_table,
        logs_txt,
        seen_rev,
        q_sig,
        current_ui_ver,
        auto_checkbox_update,
        current_auto,
        auth_status_upd,
        auth_group_upd,
        auth_link_upd,
        sheet_dd_upd,
        accordion_upd,
        current_auth_sig
    )


def listen() -> None:
    full_outputs = [
        SUMMARY_HTML,
        BANNER_HTML,
        BACKUP_ALERT_GROUP,
        BACKUP_ALERT_HTML,
        CLAIMABLE_EMPTY_HTML,
        CLAIMABLE_ACTION_COL,
        CLAIMABLE_CHECKBOX_GROUP,
        CLAIMABLE_DATAFRAME,
        ALL_ROWS_DATAFRAME,
        CURRENT_TASK_HTML,
        ABORT_CURRENT_BUTTON,
        QUEUE_GROUP,
        QUEUED_SELECT_DROPDOWN,
        LOCAL_QUEUE_DATAFRAME,
        WORKER_LOG_TEXTBOX,
        SEEN_REVISION_STATE,
        SEEN_QUEUE_SIG_STATE,
        SEEN_UI_VERSION_STATE
    ]

    CLIENT_SECRET_FILE.upload(
        on_upload_secret_file,
        inputs=CLIENT_SECRET_FILE,
        outputs=[
            AUTH_STATUS_TEXTBOX,
            AUTH_PENDING_GROUP,
            AUTH_LINK_HTML,
            BANNER_HTML,
            WORKER_LOG_TEXTBOX,
            CLIENT_SECRET_FILE,
            SEEN_AUTH_SIG_STATE
        ],
        concurrency_id='sheet_ops',
        concurrency_limit=1,
        show_progress='minimal'
    )

    AUTHORIZE_BUTTON.click(
        on_authorize,
        inputs=[WORKER_NAME_TEXTBOX, SPREADSHEET_URL_TEXTBOX, SHEET_TITLE_DROPDOWN, DRIVE_FOLDER_TEXTBOX, CLIENT_SECRET_FILE],
        outputs=[
            AUTH_STATUS_TEXTBOX,
            AUTH_PENDING_GROUP,
            AUTH_LINK_HTML,
            MANUAL_AUTH_CODE_TEXTBOX,
            BANNER_HTML,
            WORKER_LOG_TEXTBOX,
            CLIENT_SECRET_FILE,
            SEEN_AUTH_SIG_STATE
        ],
        concurrency_id='sheet_ops',
        concurrency_limit=1,
        show_progress='minimal'
    )

    MANUAL_AUTH_SUBMIT_BUTTON.click(
        on_manual_auth_submit,
        inputs=[
            MANUAL_AUTH_CODE_TEXTBOX,
            WORKER_NAME_TEXTBOX,
            SPREADSHEET_URL_TEXTBOX,
            SHEET_TITLE_DROPDOWN,
            DRIVE_FOLDER_TEXTBOX,
            SHOW_FAILED_CHECKBOX
        ],
        outputs=[
            AUTH_STATUS_TEXTBOX,
            AUTH_PENDING_GROUP,
            AUTH_LINK_HTML,
            MANUAL_AUTH_CODE_TEXTBOX,
            SHEET_TITLE_DROPDOWN,
            CONFIG_ACCORDION,
            SEEN_AUTH_SIG_STATE,
            *full_outputs
        ],
        concurrency_id='sheet_ops',
        concurrency_limit=1,
        show_progress='minimal'
    )

    LOGOUT_AUTH_BUTTON.click(
        on_logout_auth,
        inputs=SHOW_FAILED_CHECKBOX,
        outputs=[
            AUTH_STATUS_TEXTBOX,
            AUTH_PENDING_GROUP,
            AUTH_LINK_HTML,
            MANUAL_AUTH_CODE_TEXTBOX,
            AUTO_CLAIM_CHECKBOX,
            CONFIG_ACCORDION,
            SEEN_AUTH_SIG_STATE,
            *full_outputs
        ],
        concurrency_id='sheet_ops',
        concurrency_limit=1,
        show_progress='minimal'
    )

    SAVE_CONNECT_BUTTON.click(
        on_save_and_connect,
        inputs=[
            WORKER_NAME_TEXTBOX,
            SPREADSHEET_URL_TEXTBOX,
            SHEET_TITLE_DROPDOWN,
            DRIVE_FOLDER_TEXTBOX,
            CLIENT_SECRET_FILE,
            SHOW_FAILED_CHECKBOX
        ],
        outputs=[
            AUTH_STATUS_TEXTBOX,
            AUTH_PENDING_GROUP,
            AUTH_LINK_HTML,
            SHEET_TITLE_DROPDOWN,
            CLIENT_SECRET_FILE,
            CONFIG_ACCORDION,
            SEEN_AUTH_SIG_STATE,
            *full_outputs
        ],
        concurrency_id='sheet_ops',
        concurrency_limit=1,
        show_progress='minimal'
    )

    REFRESH_BUTTON.click(
        on_refresh_sheet,
        inputs=SHOW_FAILED_CHECKBOX,
        outputs=[CONFIG_ACCORDION, *full_outputs],
        concurrency_id='sheet_ops',
        concurrency_limit=1,
        show_progress='minimal'
    )

    RECOVER_OWNED_BUTTON.click(
        on_recover_owned,
        inputs=SHOW_FAILED_CHECKBOX,
        outputs=[CONFIG_ACCORDION, *full_outputs],
        concurrency_id='sheet_ops',
        concurrency_limit=1,
        show_progress='minimal'
    )

    SHOW_FAILED_CHECKBOX.change(
        on_toggle_show_failed,
        inputs=SHOW_FAILED_CHECKBOX,
        outputs=[CLAIMABLE_EMPTY_HTML, CLAIMABLE_ACTION_COL, CLAIMABLE_CHECKBOX_GROUP, CLAIMABLE_DATAFRAME],
        show_progress='hidden'
    )

    SELECT_ALL_BUTTON.click(on_select_all, inputs=SHOW_FAILED_CHECKBOX, outputs=CLAIMABLE_CHECKBOX_GROUP, show_progress='hidden')
    CLEAR_SELECT_BUTTON.click(on_clear_selection, outputs=CLAIMABLE_CHECKBOX_GROUP, show_progress='hidden')

    CLAIM_SELECTED_BUTTON.click(
        on_claim_selected,
        inputs=[CLAIMABLE_CHECKBOX_GROUP, SHOW_FAILED_CHECKBOX],
        outputs=[CONFIG_ACCORDION, *full_outputs],
        concurrency_id='sheet_ops',
        concurrency_limit=1,
        show_progress='minimal'
    )

    AUTO_CLAIM_CHECKBOX.input(
        on_toggle_auto_claim,
        inputs=AUTO_CLAIM_CHECKBOX,
        outputs=[AUTO_CLAIM_CHECKBOX, CONFIG_ACCORDION, SUMMARY_HTML, BANNER_HTML, WORKER_LOG_TEXTBOX, SEEN_AUTO_ENABLED_STATE],
        show_progress='minimal'
    )

    AUTO_POLL_NOW_BUTTON.click(
        on_poll_auto_now,
        inputs=SHOW_FAILED_CHECKBOX,
        outputs=[CONFIG_ACCORDION, *full_outputs],
        concurrency_id='sheet_ops',
        concurrency_limit=1,
        show_progress='minimal'
    )

    ABORT_CURRENT_BUTTON.click(
        on_abort_current,
        outputs=[BANNER_HTML, WORKER_LOG_TEXTBOX],
        show_progress='minimal'
    )

    RELEASE_QUEUE_BUTTON.click(
        on_release_queued,
        inputs=[QUEUED_SELECT_DROPDOWN, SHOW_FAILED_CHECKBOX],
        outputs=full_outputs,
        concurrency_id='sheet_ops',
        concurrency_limit=1,
        show_progress='minimal'
    )

    RETRY_BACKUP_UPLOAD_BUTTON.click(
        on_retry_backup_upload,
        inputs=SHOW_FAILED_CHECKBOX,
        outputs=full_outputs,
        concurrency_id='sheet_ops',
        concurrency_limit=1,
        show_progress='minimal'
    )

    OPEN_LOCAL_OUTPUTS_BUTTON.click(
        on_open_local_outputs,
        outputs=[BANNER_HTML, WORKER_LOG_TEXTBOX],
        show_progress='minimal'
    )

    LIVE_TIMER.tick(
        on_timer_tick,
        inputs=[
            SEEN_REVISION_STATE,
            SEEN_QUEUE_SIG_STATE,
            SEEN_UI_VERSION_STATE,
            SEEN_AUTO_ENABLED_STATE,
            SEEN_AUTH_SIG_STATE,
            SHOW_FAILED_CHECKBOX
        ],
        outputs=[
            *full_outputs,
            AUTO_CLAIM_CHECKBOX,
            SEEN_AUTO_ENABLED_STATE,
            AUTH_STATUS_TEXTBOX,
            AUTH_PENDING_GROUP,
            AUTH_LINK_HTML,
            SHEET_TITLE_DROPDOWN,
            CONFIG_ACCORDION,
            SEEN_AUTH_SIG_STATE
        ],
        show_progress='hidden'
    )
