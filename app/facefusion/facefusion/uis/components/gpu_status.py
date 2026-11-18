"""Non-blocking GPU status panels for manual processing and automatic jobs."""
import html
import math

import gradio

from facefusion.gpu_status import GPU_STATUS

PANELS = {}
TIMER = None


def memory(value):
    if value is None or not math.isfinite(value) or value < 0:
        return '暂不可读取'
    return '%.2f GiB' % (value / 1024)


def format_status(snapshot):
    device = snapshot.get('device') or {}
    active = snapshot.get('active', False)
    task = snapshot.get('task_id')
    workers = snapshot.get('workers') or {}
    tier = snapshot.get('tier') or ('准备选档' if active else '等待任务')
    status = snapshot.get('status') or '任务开始后显示实际档位与观测峰值'
    total = snapshot.get('total_mib') or device.get('total_mib') or 0
    policy = '6GB策略：空闲≥3200MiB优先中档，最高中档' if 5120 < total <= 6144 else ''
    concurrency = ' · '.join('%s%s' % (title, workers[key]) for key,title in [
        ('swap','换脸'),('restore','恢复'),('enhance','增强'),('temple','太阳穴')] if key in workers)
    current = (memory(snapshot.get('current_mib')) if snapshot.get('pid') else '—（GPU阶段未开始）') if active else ('—（已结束）' if task else '—')
    peak = memory(snapshot.get('peak_mib')) if task else '—'
    title = html.escape(str(task)) if task else '暂无处理任务'
    card = html.escape(str(device.get('name') or '正在读取显卡信息'))
    note = '按处理进程及编码子进程统计；整卡占用含其他程序，仅统计专用显存。'
    if active and snapshot.get('pid') and snapshot.get('current_mib') is None:
        note = '任务进程显存暂不可读取，整卡数据仅供参考；不会把整卡占用当作任务占用。'
    if snapshot.get('sample_age') is not None and snapshot['sample_age'] > 5:
        note += ' 数据正在更新。'
    return (
        '<div style="border:1px solid var(--border-color-primary,#ddd);border-radius:10px;padding:12px;margin-top:10px">'
        '<div style="display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap">'
        '<strong>显存与并发档位</strong><span>' + html.escape(str(status)) + '</span></div>'
        '<div style="margin:5px 0;overflow-wrap:anywhere">' + title + '</div>'
        '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px">'
        '<div>当前档位<br><strong>' + html.escape(str(tier)) + '</strong></div>'
        '<div>任务进程显存<br><strong>' + current + '</strong></div>'
        '<div>本次观测峰值<br><strong>' + peak + '</strong></div>'
        '<div>整卡已用 / 总显存<br><strong>' + memory(device.get('used_mib')) + ' / ' + memory(device.get('total_mib')) + '</strong></div>'
        '<div>整卡空闲显存<br><strong>' + memory(device.get('free_mib')) + '</strong></div></div>'
        '<div style="margin-top:7px">' + html.escape(concurrency) + '</div>' +
        ('<div>' + html.escape(policy) + '</div>' if policy else '') +
        '<div style="font-size:12px;opacity:.75;margin-top:5px">' + card + '<br>' + html.escape(note) + '</div></div>'
    )


def render(source):
    global TIMER
    if source == 'manual':
        PANELS.clear()
        TIMER = None
    PANELS[source] = gradio.HTML(value=format_status(GPU_STATUS.snapshot(source, request=False)), padding=False)
    if TIMER is None:
        TIMER = gradio.Timer(2)


def refresh():
    return tuple(format_status(GPU_STATUS.snapshot(source)) for source in PANELS)


def listen():
    if TIMER is not None and PANELS:
        TIMER.tick(refresh, outputs=list(PANELS.values()), show_progress='hidden', queue=False)
