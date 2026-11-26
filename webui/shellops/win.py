# -*- coding: utf-8 -*-
"""Windows 窗口操作：把刚打开的窗口带到最前面。

为什么需要它：本程序在后台运行（pythonw），不是当前的前台程序。
Windows 为了防“抢焦点”，默认不允许后台程序切换前台窗口，
所以用记事本/资源管理器打开文件时，窗口往往开在后面，
用户看不到，以为“没打开”。这里用几种标准办法把窗口提到最前：

  1. ShellExecuteEx(SEE_MASK_NOCLOSEPROCESS) 拿到新进程，等它的主窗口出现；
  2. 置顶再取消置顶 + BringWindowToTop；
  3. AttachThreadInput 附加到前台线程后 SetForegroundWindow（绕过前台锁）；
  4. 以上都不行时，模拟按一下 Alt 键（系统认为有用户输入，就会允许切换）。
"""
import ctypes
import os
import subprocess
import threading
import time
from ctypes import wintypes as wt

IS_WIN = os.name == "nt"

SW_RESTORE = 9
SW_SHOWNORMAL = 1
HWND_TOPMOST = ctypes.c_void_p(-1)
HWND_NOTOPMOST = ctypes.c_void_p(-2)
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_SHOWWINDOW = 0x0040
GW_OWNER = 4
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
VK_MENU = 0x12
KEYEVENTF_KEYUP = 0x0002
ASFW_ANY = 0xFFFFFFFF          # AllowSetForegroundWindow 用的“允许任何进程”
SEE_MASK_NOCLOSEPROCESS = 0x00000040
SEE_MASK_NOASYNC = 0x00000100
SEE_MASK_FLAG_NO_UI = 0x00000400
ERROR_NO_ASSOCIATION = 1155
ERROR_CANCELLED = 1223

if IS_WIN:
    try:
        _user32 = ctypes.WinDLL("user32", use_last_error=True)
        _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        _user32.GetForegroundWindow.restype = wt.HWND
        _user32.GetWindow.argtypes = [wt.HWND, wt.UINT]
        _user32.GetWindow.restype = wt.HWND
        _user32.IsWindowVisible.argtypes = [wt.HWND]
        _user32.IsIconic.argtypes = [wt.HWND]
        _user32.GetWindowTextLengthW.argtypes = [wt.HWND]
        _user32.SetForegroundWindow.argtypes = [wt.HWND]
        _user32.SetForegroundWindow.restype = wt.BOOL
        _user32.BringWindowToTop.argtypes = [wt.HWND]
        _user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
        _user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wt.UINT]
        _user32.keybd_event.argtypes = [wt.BYTE, wt.BYTE, wt.DWORD, ctypes.c_void_p]
        _user32.AllowSetForegroundWindow.argtypes = [wt.DWORD]
        _user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
        _kernel32.GetCurrentThreadId.restype = wt.DWORD
        _kernel32.GetProcessId.argtypes = [wt.HANDLE]
        _kernel32.GetProcessId.restype = wt.DWORD
        _kernel32.CloseHandle.argtypes = [wt.HANDLE]
        _user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
        _user32.GetWindowThreadProcessId.restype = wt.DWORD
        _user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
        _user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
        _get_style = getattr(_user32, "GetWindowLongPtrW", _user32.GetWindowLongW)
        _get_style.argtypes = [wt.HWND, ctypes.c_int]
        _get_style.restype = ctypes.c_ssize_t
        _EnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    except Exception:            # 声明失败也不能让程序起不来（功能退化成“窗口不置前”）
        try:
            from core.diag import log_exc
            log_exc("窗口 API 初始化失败")
        except Exception:
            pass


def _pid_of_window(hwnd):
    pid = wt.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _title(hwnd):
    n = _user32.GetWindowTextLengthW(hwnd)
    if not n:
        return ""
    buf = ctypes.create_unicode_buffer(n + 2)
    _user32.GetWindowTextW(hwnd, buf, n + 2)
    return buf.value


def windows():
    """所有可见的顶层窗口：{句柄: (进程ID, 标题)}"""
    if not IS_WIN:
        return {}
    out = {}

    def cb(hwnd, _):
        if not _user32.IsWindowVisible(hwnd):
            return True
        if _user32.GetWindow(hwnd, GW_OWNER):           # 有父窗口的是对话框，跳过（另有专门处理）
            return True
        if _user32.GetWindowTextLengthW(hwnd) == 0:
            return True
        if _get_style(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
            return True
        out[hwnd] = (_pid_of_window(hwnd), _title(hwnd))
        return True

    try:
        _user32.EnumWindows(_EnumProc(cb), 0)
    except Exception:
        return out
    return out


def activate(hwnd):
    """把窗口提到最前（并恢复最小化的窗口）"""
    if not IS_WIN or not hwnd:
        return False
    try:
        if _user32.IsIconic(hwnd):
            _user32.ShowWindow(hwnd, SW_RESTORE)
        if _user32.GetForegroundWindow() == hwnd:
            return True
        # ① 置顶再取消置顶：这条不受前台锁限制
        _user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        _user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        _user32.BringWindowToTop(hwnd)
        if _user32.GetForegroundWindow() == hwnd:
            return True
        # ② 附加到前台线程的输入队列，再切换（标准绕过后台限制的做法）
        fg = _user32.GetForegroundWindow()
        fg_tid = _user32.GetWindowThreadProcessId(fg, None) if fg else 0
        cur_tid = _kernel32.GetCurrentThreadId()
        attached = False
        if fg_tid and fg_tid != cur_tid:
            attached = bool(_user32.AttachThreadInput(cur_tid, fg_tid, True))
        try:
            _user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                _user32.AttachThreadInput(cur_tid, fg_tid, False)
        if _user32.GetForegroundWindow() == hwnd:
            return True
        # ③ 最后手段：模拟按一下 Alt 键
        _user32.AllowSetForegroundWindow(ASFW_ANY)
        _user32.keybd_event(VK_MENU, 0, 0, None)
        _user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, None)
        time.sleep(0.02)
        _user32.SetForegroundWindow(hwnd)
        _user32.BringWindowToTop(hwnd)
        return _user32.GetForegroundWindow() == hwnd
    except Exception:
        return False


def _pick(before, pid=0, hint=""):
    """在“打开之前记录的窗口”之外，挑出最像目标的那一个"""
    now = windows()
    new = [(h, p, t) for h, (p, t) in now.items() if h not in before]
    if not new:
        return 0
    if pid:
        same = [x for x in new if x[1] == pid]
        if same:
            new = same
    if hint:
        hit = [x for x in new if hint.lower() in (x[2] or "").lower()]
        if hit:
            return hit[0][0]
    return new[0][0]


def bring_forward(before, pid=0, hint="", timeout=3.0):
    """后台线程：等新窗口出现并把它提到最前"""
    if not IS_WIN:
        return
    deadline = time.time() + max(0.2, timeout)
    while time.time() < deadline:
        hwnd = _pick(before, pid, hint)
        if hwnd:
            time.sleep(0.08)            # 再等一下，让窗口画完第一帧
            if activate(hwnd):
                return
        time.sleep(0.1)
    if pid:
        # 没有出现新窗口：程序可能是复用已经打开的窗口，把它叫到最前
        for h, (p, t) in windows().items():
            if p == pid and activate(h):
                return


def _later(fn, *a, **kw):
    threading.Thread(target=fn, args=a, kwargs=kw, daemon=True).start()


def _shell_execute(path, verb="open"):
    """ShellExecuteEx：启动文件并拿到进程句柄，返回 (进程ID, 错误信息)"""
    if not IS_WIN:
        try:
            os.startfile(path)
            return 0, ""
        except OSError as e:
            return 0, str(e)
    try:
        sei = _ShellExecuteInfo()
        sei.cbSize = ctypes.sizeof(sei)
        sei.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC | SEE_MASK_FLAG_NO_UI
        sei.lpVerb = verb
        sei.lpFile = path
        sei.nShow = SW_SHOWNORMAL
        # 允许被打开的程序自己切到前台（它才是用户真正想看的窗口）
        try:
            _user32.AllowSetForegroundWindow(ASFW_ANY)
        except Exception:
            pass
        if not _shell32.ShellExecuteExW(ctypes.byref(sei)):
            err = ctypes.get_last_error()
            if err == ERROR_NO_ASSOCIATION:
                return 0, "这个文件没有关联的打开程序。\n可以右键 →“打开方式…”选择用哪个程序打开。"
            if err == ERROR_CANCELLED:
                return 0, ""
            return 0, "Windows 没能打开它（错误码 %s）。" % err
        pid = 0
        handle = sei.hProcess
        if handle:
            pid = int(_kernel32.GetProcessId(handle) or 0)
            _kernel32.CloseHandle(handle)
        return pid, ""
    except Exception as e:
        return 0, str(e)


class _ShellExecuteInfo(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wt.HWND),
                ("lpVerb", wt.LPCWSTR), ("lpFile", wt.LPCWSTR), ("lpParameters", wt.LPCWSTR),
                ("lpDirectory", wt.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wt.HINSTANCE),
                ("lpIDList", ctypes.c_void_p), ("lpClass", wt.LPCWSTR), ("hkeyClass", wt.HKEY),
                ("dwHotKey", wt.DWORD), ("hIcon", wt.HANDLE), ("hProcess", wt.HANDLE)]


if IS_WIN:
    _shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_ShellExecuteInfo)]
    _shell32.ShellExecuteExW.restype = wt.BOOL


def class_of(hwnd):
    """窗口类名（资源管理器的窗口是 CabinetWClass）"""
    if not IS_WIN:
        return ""
    buf = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


# ---------------- 对外：打开文件 / 文件夹 / 打开方式 ----------------
def open_path(path, verb="open", hint=""):
    """打开文件（等同双击）。返回 (ok, 错误信息)；窗口在后台被带到最前"""
    before = windows()
    pid, err = _shell_execute(path, verb)
    if err:
        return False, err
    _later(bring_forward, before, pid, hint, 3.0)
    return True, ""


def open_folder(path, select=""):
    """打开文件夹（可选中其中的文件）。返回 (ok, 错误信息)"""
    folder = os.path.normpath(select or path)
    before = windows()
    try:
        if select:
            subprocess.Popen(["explorer.exe", "/select,", folder])
        else:
            os.startfile(folder)
    except Exception as e:
        return False, str(e)
    base = os.path.basename(os.path.dirname(folder) if select else folder.rstrip("\\/")) or folder
    # 同一个文件夹已经有窗口时，explorer 会复用旧窗口：找不到新窗口就按标题找
    _later(_bring_folder, before, base, 3.0)
    return True, ""


def _bring_folder(before, hint, timeout):
    deadline = time.time() + max(0.3, timeout)
    while time.time() < deadline:
        hwnd = _pick(before, 0, hint)
        if hwnd and activate(hwnd):
            return
        time.sleep(0.1)
    if hint:                                    # 同一个文件夹已经有窗口：只在资源管理器窗口里按标题找
        for h, (p, t) in windows().items():
            if class_of(h) in ("CabinetWClass", "ExploreWClass") and hint.lower() in (t or "").lower():
                if activate(h):
                    return


def open_with_dialog(path):
    """Windows 的“打开方式”选择窗口。返回 (ok, 错误信息)"""
    before = windows()
    try:
        proc = subprocess.Popen(["rundll32.exe", "shell32.dll,OpenAs_RunDLL", os.path.normpath(path)])
    except Exception as e:
        return False, str(e)
    _later(bring_forward, before, proc.pid, "", 3.0)
    return True, ""
