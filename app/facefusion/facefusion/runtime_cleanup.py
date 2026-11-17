"""Clean only owned disposable workspaces, never user media, models or credentials.

Markers protect work owned by a live process (including headless children).
Windows hard-kills cannot run finally: the next startup reclaims dead owners.
"""
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import time

from facefusion.userdata_paths import get_userdata_dir, get_package_root

MARKER='.facefusion-temporary.json'


def read_owner(path):
    try:
        owner=json.loads(Path(path).read_text(encoding='utf-8'))
        return owner if isinstance(owner,dict) else None
    except (OSError,ValueError):
        return None


def write_owner(path, owner):
    path=Path(path)
    handle,temporary=tempfile.mkstemp(prefix='.owner-',dir=path.parent)
    try:
        with os.fdopen(handle,'w',encoding='utf-8') as stream:
            json.dump(owner,stream)
        os.replace(temporary,path)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)


def mark_workspace(directory):
    path=Path(directory)
    path.mkdir(parents=True,exist_ok=True)
    owner=read_owner(path/MARKER)
    if owner:
        if owner.get('pid')!=os.getpid() and not owner.get('finished') and pid_alive(owner.get('pid')):
            raise OSError('另一个任务仍在使用临时目录：' + str(path))
    write_owner(path/MARKER, {'pid':os.getpid(),'package':str(get_package_root()),'created':time.time()})


def finish_workspace(directory):
    try:
        marker=Path(directory)/MARKER
        owner=read_owner(marker)
        if owner and owner.get('pid')==os.getpid():
            owner['finished']=True
            write_owner(marker,owner)
    except (OSError,ValueError):pass


def pid_alive(pid):
    if not isinstance(pid,int) or pid<=0:
        return False
    if os.name=='nt':
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=kernel.OpenProcess(0x1000,False,pid)
        if not handle:
            return ctypes.get_last_error()==5  # Access denied means preserve.
        try:
            code=wintypes.DWORD()
            return not kernel.GetExitCodeProcess(handle,ctypes.byref(code)) or code.value==259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid,0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def safe_remove(directory, owned_root):
    """Delete a direct workspace child only after resolving its absolute boundary."""
    path=Path(directory);root=Path(owned_root).resolve()
    try:
        if path.is_symlink() or path.resolve().parent!=root:
            return False
        if not path.exists():
            return True
        marker=path/MARKER
        owner=read_owner(marker)
        if owner:
            if owner.get('pid')!=os.getpid() and not owner.get('finished') and pid_alive(owner.get('pid')):
                return False
        def writable(function, name, error):
            os.chmod(name,stat.S_IWRITE)
            function(name)
        for attempt in range(3):
            try:
                if path.is_dir():
                    shutil.rmtree(path,onerror=writable)
                else:
                    path.unlink()
                return True
            except OSError:
                if attempt<2:time.sleep(.1)
        return False
    except (OSError,ValueError):
        return False


def sweep_workspaces(startup=False):
    data=get_userdata_dir()
    roots=[data/'temp/facefusion',data/'temp/manual_preflight']
    removed=0
    for root in roots:
        if not root.is_dir():continue
        for child in list(root.iterdir()):
            if not child.is_dir():continue
            try:
                marker=child/MARKER
                owner=read_owner(marker)
                if owner:
                    if not owner.get('finished') and pid_alive(owner.get('pid')):continue
                    if Path(owner.get('package','')).resolve()!=get_package_root().resolve():continue
                elif not startup or child.stat().st_mtime>time.time()-24*3600:
                    continue
                removed+=bool(safe_remove(child,root))
            except (OSError,ValueError,TypeError):
                continue
    # System Temp may be shared with other FaceFusion installs. Only our marked
    # workspaces are eligible there; unknown legacy directories are preserved.
    system_root=Path(tempfile.gettempdir())/'facefusion'
    if system_root.resolve() not in [p.resolve() for p in roots] and system_root.is_dir():
        for child in list(system_root.iterdir()):
            try:
                owner=read_owner(child/MARKER)
                if owner and Path(owner.get('package','')).resolve()==get_package_root().resolve() and not pid_alive(owner.get('pid')):
                    removed+=bool(safe_remove(child,system_root))
            except (OSError,ValueError,TypeError):pass
    return removed


if __name__=='__main__':
    count=sweep_workspaces(startup=True)
    if count:print('已自动回收 %s 个过期临时任务目录' % count)
