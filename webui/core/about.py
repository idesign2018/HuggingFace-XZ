# -*- coding: utf-8 -*-
"""接口：关于软件 / 关于作者。

“关于作者”和“关于软件”里的图文内容不放代码里，而是放程序目录下的“关于”文件夹，
直接用记事本或任何网页编辑器改就能生效（图片也放这个文件夹）：

    程序目录\\关于\\关于作者.html      ← 作者信息、联系方式、二维码……
    程序目录\\关于\\关于软件.html      ← 版本说明、致谢、免责声明……

本文件只负责：给“关于软件”提供版本号、运行环境、各处路径这些通用信息。
"""
import os
import sys
import time
import platform
import datetime
import importlib.util

from core import version
from core.diag import LOG_PATH, PROGRAM_DIR
from shellops import files as shell

CONTENT_DIR = os.path.join(PROGRAM_DIR, "关于")
ABOUT_AUTHOR = "关于作者.html"
ABOUT_APP = "关于软件.html"
CONTENT_KINDS = {".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".txt": "text/plain; charset=utf-8", ".json": "application/json; charset=utf-8",
                 ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg",
                 ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
                 ".ico": "image/x-icon", ".woff2": "font/woff2"}


def _pkg_version(name):
    """已安装的包的版本号（不导入它，免得拖慢启动）；没装返回空"""
    try:
        if importlib.util.find_spec(name) is None:
            return ""
        from importlib.metadata import version as _v
        return _v(name)
    except Exception:
        return ""


def content_file(name):
    """“关于”文件夹里的文件（带安全检查，防止跑到别的目录去）"""
    full = os.path.normpath(os.path.join(CONTENT_DIR, name))
    if not (full == CONTENT_DIR or full.startswith(CONTENT_DIR + os.sep)):
        return ""
    return full


def _file_info(name):
    full = content_file(name)
    if not full or not os.path.isfile(full):
        return {"name": name, "path": full or os.path.join(CONTENT_DIR, name), "exists": False,
                "size": 0, "mtime": 0}
    st = os.stat(full)
    return {"name": name, "path": full, "exists": True, "size": st.st_size, "mtime": int(st.st_mtime)}


class AboutApiMixin:
    # ---------------- 关于软件 ----------------
    def api_about(self, b):
        bs = self.basic_settings()
        return {
            "name": version.APP_NAME,
            "sub": version.APP_SUB,
            "version": version.VERSION,
            "build": version.BUILD_DATE,
            "author": version.AUTHOR,
            "copyright": version.COPYRIGHT,
            "homepage": version.HOMEPAGE,
            "license": version.LICENSE,
            "window": getattr(self, "window_mode", "") or "—",
            "python": "%s (%s)" % (sys.version.split()[0], platform.machine()),
            "system": "%s %s" % (platform.system(), platform.release()),
            "uptime": int(time.time() - getattr(self, "started_at", time.time())),
            "today": datetime.date.today().strftime("%Y-%m-%d"),
            "stats": {"history": len(self.basic.rows), "repos": len(self.batch.repos)},
            "deps": {
                "huggingface_hub": _pkg_version("huggingface_hub") or "未安装",
                "hf_xet": _pkg_version("hf_xet") or "未安装",
                "pywebview": _pkg_version("pywebview") or "未安装",
                "IDM": bs.get("idm_path") or self.batch.s.get("idm_path") or "未找到",
                "aria2": self.batch.s.get("aria2_path") or "未找到",
            },
            "paths": {
                "程序目录": PROGRAM_DIR,
                "配置文件": os.path.join(PROGRAM_DIR, "hf_downloader_config.json"),
                "历史记录": os.path.join(PROGRAM_DIR, "hf_download_history.json"),
                "启动日志": LOG_PATH,
                "关于内容": CONTENT_DIR,
                "下载保存位置": bs.get("path") or "",
            },
            "content": {"dir": CONTENT_DIR, "author": _file_info(ABOUT_AUTHOR), "app": _file_info(ABOUT_APP)},
        }

    def api_open_content(self, b):
        """打开“关于”文件夹，方便直接改里面的图文"""
        os.makedirs(CONTENT_DIR, exist_ok=True)
        return shell.open_dir(CONTENT_DIR)
