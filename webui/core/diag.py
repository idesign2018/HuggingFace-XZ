# -*- coding: utf-8 -*-
"""启动诊断日志。

pythonw 没有控制台，启动失败时什么提示都没有（双击后像没反应一样），
所以把关键步骤和错误都写到程序目录下的 启动日志.log，出问题时一看就知道断在哪。
"""
import os
import time
import traceback

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # webui
PROGRAM_DIR = os.path.dirname(APP_DIR)                                  # 程序目录（和配置文件放一起）
LOG_PATH = os.path.join(PROGRAM_DIR, "启动日志.log")
MAX_SIZE = 200 * 1024


def log(text):
    """追加一行日志（出错时静默忽略，绝不影响程序本身）"""
    try:
        if os.path.isfile(LOG_PATH) and os.path.getsize(LOG_PATH) > MAX_SIZE:
            try:
                os.replace(LOG_PATH, LOG_PATH + ".old")
            except Exception:
                pass
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("[%m-%d %H:%M:%S] ") + str(text).rstrip() + "\n")
    except Exception:
        pass


def log_exc(prefix="出错"):
    log(prefix + "：\n" + traceback.format_exc())


def show_error(title, text):
    """弹一个系统消息框：pythonw 下也能看到（没有界面也不用怕，失败就算了）"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, str(text), str(title), 0x10)
    except Exception:
        pass
