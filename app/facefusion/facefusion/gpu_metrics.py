"""Read dedicated GPU process memory without importing CUDA or allocating VRAM."""
import csv
import ctypes as C
from ctypes import wintypes as W
import io
import os
import re
import subprocess


def read_devices():
    try:
        output = subprocess.check_output([
            'nvidia-smi', '--query-gpu=index,name,memory.total,memory.used,memory.free',
            '--format=csv,noheader,nounits'
        ], timeout=3, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).decode(errors='replace')
        return {int(row[0]): dict(name=row[1].strip(), total_mib=float(row[2]), used_mib=float(row[3]), free_mib=float(row[4]))
                for row in csv.reader(io.StringIO(output)) if len(row) == 5}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}


def process_family(root_pid):
    if os.name != 'nt':
        return {root_pid} if root_pid == os.getpid() else set()
    class Entry(C.Structure):
        _fields_ = [('dwSize', W.DWORD), ('cntUsage', W.DWORD), ('pid', W.DWORD),
                    ('heap', C.c_size_t), ('module', W.DWORD), ('threads', W.DWORD),
                    ('parent', W.DWORD), ('priority', W.LONG), ('flags', W.DWORD), ('exe', W.WCHAR * 260)]
    lib = C.WinDLL('kernel32', use_last_error=True)
    lib.CreateToolhelp32Snapshot.argtypes = [W.DWORD, W.DWORD]
    lib.CreateToolhelp32Snapshot.restype = W.HANDLE
    lib.Process32FirstW.argtypes = [W.HANDLE, C.POINTER(Entry)]
    lib.Process32NextW.argtypes = [W.HANDLE, C.POINTER(Entry)]
    lib.CloseHandle.argtypes = [W.HANDLE]
    handle = lib.CreateToolhelp32Snapshot(2, 0)
    if handle == C.c_void_p(-1).value:
        return set()
    parents = {}
    try:
        entry = Entry(); entry.dwSize = C.sizeof(entry)
        more = lib.Process32FirstW(handle, C.byref(entry))
        while more:
            parents[int(entry.pid)] = int(entry.parent)
            more = lib.Process32NextW(handle, C.byref(entry))
    finally:
        lib.CloseHandle(handle)
    if root_pid not in parents:
        return set()
    result = {root_pid}
    while True:
        children = {pid for pid, parent in parents.items() if parent in result}
        if children.issubset(result):
            return result
        result.update(children)


def single_nvidia_adapter():
    """Map the one NVIDIA adapter to a WDDM LUID; refuse ambiguous multi-GPU data."""
    if os.name != 'nt':
        return None
    class Guid(C.Structure):
        _fields_ = [('a', W.DWORD), ('b', W.WORD), ('c', W.WORD), ('d', C.c_ubyte * 8)]
    class Luid(C.Structure):
        _fields_ = [('low', W.DWORD), ('high', W.LONG)]
    class Desc(C.Structure):
        _fields_ = [('name', W.WCHAR * 128), ('vendor', W.UINT), ('device', W.UINT),
                    ('subsystem', W.UINT), ('revision', W.UINT), ('video', C.c_size_t),
                    ('system', C.c_size_t), ('shared', C.c_size_t), ('luid', Luid), ('flags', W.UINT)]
    def method(pointer, index, result, *args):
        table = C.cast(pointer, C.POINTER(C.POINTER(C.c_void_p))).contents
        return C.WINFUNCTYPE(result, C.c_void_p, *args)(table[index])
    library = C.WinDLL('dxgi'); factory = C.c_void_p()
    iid = Guid(0x770aae78, 0xf26f, 0x4dba, (C.c_ubyte * 8)(0xa8,0x29,0x25,0x3c,0x83,0xd1,0xb3,0x87))
    create = library.CreateDXGIFactory1
    create.argtypes = [C.POINTER(Guid), C.POINTER(C.c_void_p)]
    if create(C.byref(iid), C.byref(factory)):
        return None
    found = []
    try:
        for index in range(16):
            adapter = C.c_void_p()
            if method(factory, 12, W.LONG, W.UINT, C.POINTER(C.c_void_p))(factory, index, C.byref(adapter)):
                break
            try:
                desc = Desc()
                if not method(adapter, 10, W.LONG, C.POINTER(Desc))(adapter, C.byref(desc)) and desc.vendor == 0x10de:
                    found.append('luid_0x%08x_0x%08x_phys_0' % (desc.luid.high & 0xffffffff, desc.luid.low))
            finally:
                method(adapter, 2, W.ULONG)(adapter)
    finally:
        method(factory, 2, W.ULONG)(factory)
    return found[0] if len(found) == 1 else None


class DedicatedCounter:
    def __init__(self, suffix):
        class Union(C.Union):
            _fields_ = [('large', C.c_longlong), ('double', C.c_double), ('long', W.LONG)]
        class Value(C.Structure):
            _anonymous_ = ('value',)
            _fields_ = [('status', W.DWORD), ('value', Union)]
        class Item(C.Structure):
            _fields_ = [('name', W.LPWSTR), ('value', Value)]
        self.Item = Item; self.suffix = suffix.lower(); self.query = W.HANDLE(); self.counter = W.HANDLE()
        self.lib = C.WinDLL('pdh')
        self.lib.PdhOpenQueryW.argtypes = [W.LPCWSTR, C.c_size_t, C.POINTER(W.HANDLE)]
        self.lib.PdhAddEnglishCounterW.argtypes = [W.HANDLE, W.LPCWSTR, C.c_size_t, C.POINTER(W.HANDLE)]
        self.lib.PdhCollectQueryData.argtypes = [W.HANDLE]
        self.lib.PdhGetFormattedCounterArrayW.argtypes = [W.HANDLE,W.DWORD,C.POINTER(W.DWORD),C.POINTER(W.DWORD),C.c_void_p]
        self.lib.PdhCloseQuery.argtypes = [W.HANDLE]
        self.check(self.lib.PdhOpenQueryW(None, 0, C.byref(self.query)))
        try:
            self.check(self.lib.PdhAddEnglishCounterW(self.query, '\\GPU Process Memory(*)\\Dedicated Usage', 0, C.byref(self.counter)))
        except BaseException:
            self.close()
            raise
    def check(self, status):
        if status:
            raise OSError('GPU counter error ' + hex(status & 0xffffffff))
    def sample(self):
        self.check(self.lib.PdhCollectQueryData(self.query))
        size, count = W.DWORD(), W.DWORD()
        status = self.lib.PdhGetFormattedCounterArrayW(self.counter, 0x400, C.byref(size), C.byref(count), None) & 0xffffffff
        if status not in (0, 0x800007d2):
            self.check(status)
        if not size.value:
            return {}
        buffer = C.create_string_buffer(size.value)
        self.check(self.lib.PdhGetFormattedCounterArrayW(self.counter, 0x400, C.byref(size), C.byref(count), buffer))
        items = C.cast(buffer, C.POINTER(self.Item)); result = {}
        for index in range(count.value):
            item = items[index]; match = re.match(r'pid_(\d+)_', item.name)
            if match and item.name.lower().endswith(self.suffix) and item.value.status in (0, 1):
                pid = int(match.group(1)); result[pid] = result.get(pid, 0) + int(item.value.large)
        return result
    def close(self):
        if self.query:
            self.lib.PdhCloseQuery(self.query); self.query = W.HANDLE()


class Collector:
    def __init__(self):
        self.counter = None
        try:
            suffix = single_nvidia_adapter()
            if suffix:
                self.counter = DedicatedCounter(suffix)
        except (OSError, AttributeError, ValueError):
            pass
    def read(self, tasks):
        devices = read_devices(); amounts = None
        if self.counter:
            try:
                amounts = self.counter.sample()
            except (OSError, ValueError):
                amounts = None
        memory = {}
        for key, task in tasks.items():
            family = process_family(task['pid']) if task.get('pid') else set()
            memory[key] = sum(amounts.get(pid, 0) for pid in family) / 1024**2 if amounts is not None and family else None
        return devices, memory
    def close(self):
        if self.counter:
            self.counter.close()
