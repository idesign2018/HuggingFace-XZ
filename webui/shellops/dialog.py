# -*- coding: utf-8 -*-
"""
Windows 原生的“选择文件夹 / 打开文件 / 另存为”对话框。

网页本身拿不到本地完整路径，所以由 Python 弹出系统对话框。
每次在一个很小的独立进程里弹出（约 0.1 秒），不影响主程序和下载；同一时间只允许一个对话框。
"""
import os
import sys
import json
import threading
import subprocess

_lock = threading.Lock()
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 在独立进程中运行的脚本：读取 JSON 参数，弹出对话框，输出 JSON 结果
_SCRIPT = r'''
import sys, json, os
try:
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(1)      # 高分屏下对话框不发虚
except Exception:
    pass
import tkinter as tk
from tkinter import filedialog
a = json.loads(sys.stdin.read() or "{}")
root = tk.Tk()
root.withdraw()
root.attributes("-topmost", True)
root.update()
kind = a.get("kind")
init = a.get("initialdir") or ""
if init and not os.path.isdir(init):
    init = os.path.dirname(init) if os.path.isdir(os.path.dirname(init)) else ""
kw = {"parent": root, "title": a.get("title") or ""}
if init:
    kw["initialdir"] = init
ft = [tuple(x) for x in (a.get("filetypes") or [])]
if kind == "dir":
    r = filedialog.askdirectory(mustexist=False, **kw)
elif kind == "open":
    r = filedialog.askopenfilename(filetypes=ft or [("所有文件", "*.*")], **kw)
elif kind == "open_multi":
    r = list(filedialog.askopenfilenames(filetypes=ft or [("所有文件", "*.*")], **kw))
else:
    r = filedialog.asksaveasfilename(filetypes=ft or [("所有文件", "*.*")], defaultextension=a.get("ext") or "",
                                     initialfile=a.get("initialfile") or "", confirmoverwrite=a.get("overwrite", True), **kw)
root.destroy()
if isinstance(r, (list, tuple)):
    r = [os.path.normpath(x) for x in r]
elif r:
    r = os.path.normpath(r)
sys.stdout.write(json.dumps({"result": r or ""}, ensure_ascii=False))
'''


def _python_exe():
    exe = sys.executable or "python"
    # 用 pythonw 运行，不会闪出黑色窗口
    d, name = os.path.split(exe)
    if name.lower() == "python.exe" and os.path.isfile(os.path.join(d, "pythonw.exe")):
        return os.path.join(d, "pythonw.exe")
    return exe


def ask(args):
    """
    args: {"kind": "dir" | "open" | "open_multi" | "save", "title": ..., "initialdir": ...,
           "filetypes": [["CSV 文件", "*.csv"], ...], "initialfile": ..., "ext": ".csv"}
    返回 {"result": 路径 / 路径列表 / ""}
    """
    if not _lock.acquire(blocking=False):
        return {"result": "", "info": "已经打开了一个选择窗口，请先完成它。"}
    try:
        p = subprocess.run([_python_exe(), "-X", "utf8", "-c", _SCRIPT],
                           input=json.dumps(args or {}, ensure_ascii=False).encode("utf-8"),
                           capture_output=True, timeout=3600, creationflags=CREATE_NO_WINDOW)
        out = p.stdout.decode("utf-8", errors="replace").strip()
        if not out:
            err = p.stderr.decode("utf-8", errors="replace").strip()
            return {"result": "", "error": f"无法打开选择窗口：{err[-300:]}" if err else ""}
        return json.loads(out)
    except Exception as e:
        return {"result": "", "error": f"无法打开选择窗口：{e}"}
    finally:
        _lock.release()
