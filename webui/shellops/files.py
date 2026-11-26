# -*- coding: utf-8 -*-
"""右键菜单用到的系统操作：打开、打开方式…、打开文件夹（并选中文件）、删除本地文件。

打开后的窗口会由 shellops.win 带到最前面（否则后台程序打开的窗口会藏在后面）。
"""
import os

from shellops import win


def _nearest_dir(path):
    d = os.path.dirname(path)
    while d and not os.path.isdir(d):
        parent = os.path.dirname(d)
        if parent == d:
            return ""
        d = parent
    return d


def open_file(path):
    if not path or not os.path.isfile(path):
        return {"info": "文件还不存在（还没下载完成，或已被移动/删除）。"}
    ok, err = win.open_path(path)
    if not ok:
        if not err:                     # 用户在“打开方式”里点了取消
            return {"ok": True}
        return {"error": f"无法打开：{err}"}
    return {"ok": True}


def open_with(path):
    """Windows 的“打开方式”选择窗口"""
    if not path or not os.path.isfile(path):
        return {"info": "文件还不存在（还没下载完成，或已被移动/删除）。"}
    ok, err = win.open_with_dialog(path)
    if not ok:
        return {"error": f"无法打开“打开方式”窗口：{err}"}
    return {"ok": True}


def open_folder(path):
    """打开所在文件夹；文件存在时在资源管理器中选中它"""
    if path and os.path.isfile(path):
        ok, err = win.open_folder(os.path.dirname(path), select=path)
        if not ok:
            return {"error": f"无法打开文件夹：{err}"}
        return {"ok": True}
    d = _nearest_dir(path)
    if d:
        ok, err = win.open_folder(d)
        if not ok:
            return {"error": f"无法打开文件夹：{err}"}
        same = os.path.normcase(d) == os.path.normcase(os.path.dirname(path or ""))
        return {"ok": True, "info": "" if same else f"保存位置还不存在，已打开最近的上级文件夹：\n{d}"}
    return {"info": "文件夹还不存在（下载时会自动创建）。"}


def open_dir(path, info=""):
    """打开一个文件夹（批量页的“打开”按钮用）"""
    if path and os.path.isdir(path):
        ok, err = win.open_folder(path)
        if not ok:
            return {"error": f"无法打开文件夹：{err}"}
        return {"ok": True, "info": info}
    return {"info": info or "文件夹还不存在（下载时会自动创建）。"}


def delete_local(path):
    """删除本地文件和同名的未完成部分（含分段下载的临时文件）；返回删除的数量"""
    n = 0
    for p in (path, path + ".incomplete", path + ".part", path + ".hfdpart", path + ".hfdpart.json"):
        try:
            if os.path.isfile(p):
                os.remove(p)
                n += 1
        except OSError:
            pass
    return n
