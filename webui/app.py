# -*- coding: utf-8 -*-
"""
Hugging Face 模型极速下载器 —— 网页界面版（启动入口）

启动方式：双击上一级目录中的“启动.bat”（或旧的“下载(网页版).bat”），也可以运行  pythonw app.py

窗口优先级：
  1. 已安装 pywebview 时：使用独立的原生窗口（WebView2 内核）
  2. 否则：使用 Edge 的“应用模式”窗口（没有地址栏和标签页，看起来就是一个独立程序）
  3. 都没有时：用默认浏览器打开

关于“保持在线”：
  · 窗口开着的时候后台永远不会因为“太久没联系”而退出（浏览器会把后台标签页的
    定时器降频到每分钟一次，以前 150 秒就退出的做法会让人看到“无法连接到后台”）。
  · 关窗口时，网页会先发一个“关闭通知”，后台等几秒确认不是刷新页面后才退出。
  · Edge 窗口退出也会让后台退出。
  · 只有当使用普通浏览器、而且网页被强行关掉（连关闭通知都发不出来）时，
    才会在 6 小时完全没有请求后兜底退出，避免留下看不见的进程。

出问题看哪里：程序目录下的“启动日志.log”记录了每一步，启动失败的原因也在里面；
也可以双击“诊断启动.bat”，它会把完整的报错显示出来。

参数：
  --browser     强制用默认浏览器打开
  --port N      指定端口（默认自动选择）
  --check       只做自检（检查模块能否导入、配置能否读取），不打开窗口
"""
import os
import sys
import time
import platform
import shutil
import subprocess
import multiprocessing as mp

APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from core.diag import log, log_exc, show_error       # noqa: E402  （要先加好 sys.path）

IDLE_EXIT = 6 * 3600        # 兜底：普通浏览器模式下，这么久一个请求都没有才退出（正常关窗口不用等）
PROC_GONE_IDLE = 300        # Edge 窗口进程已退出、但网页还在联系时的兜底时间（页面被降频也不会误退）
BYE_GRACE = 6               # 收到“窗口已关闭”通知后，再等这么多秒（刷新页面时网页会重新连上）
WINDOW_SIZE = (1440, 920)   # 默认窗口大小（宽, 高）；屏幕放不下时自动缩小。表格列宽会随窗口自适应


class _HubWriter(object):
    """pythonw 运行时没有控制台：把意外的错误输出转到网页日志里"""
    def __init__(self, hub):
        self.hub = hub

    def write(self, s):
        if s and s.strip():
            self.hub.log_raw("basic", s if s.endswith("\n") else s + "\n")

    def flush(self):
        pass


def _window_size():
    """按屏幕大小决定窗口大小（不会超出屏幕）"""
    w, h = WINDOW_SIZE
    try:
        import ctypes
        user32 = ctypes.windll.user32
        sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        w, h = min(w, int(sw * 0.92)), min(h, int(sh * 0.88))
    except Exception:
        pass
    return w, h


def _find_edge():
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        if base:
            p = os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe")
            if os.path.isfile(p):
                return p
    return shutil.which("msedge") or ""


def _open_edge_app(url):
    edge = _find_edge()
    if not edge:
        return None
    w, h = _window_size()
    profile = os.path.join(os.environ.get("LOCALAPPDATA") or APP_DIR, "HFDownloaderWeb", "edge-profile")
    os.makedirs(profile, exist_ok=True)
    cmd = [edge, f"--app={url}", f"--user-data-dir={profile}", f"--window-size={w},{h}",
           "--no-first-run", "--no-default-browser-check", "--disable-sync", "--lang=zh-CN",
           # 下面这些参数让窗口在后台/最小化时也照常工作：
           #   · 不降频定时器 → 与后台的联系不会断
           #   · 不允许标签页进入“睡眠” → 页面不会被冻结
           "--disable-background-timer-throttling", "--disable-backgrounding-occluded-windows",
           "--disable-renderer-backgrounding",
           "--disable-features=Translate,msEdgeSidebarV2,SleepingTabs,msSleepingTabs,msEdgeSleepingTabs"]
    try:
        return subprocess.Popen(cmd)
    except Exception:
        log_exc("打开 Edge 应用窗口失败")
        return None


def _run_pywebview(url, app):
    """使用 pywebview 原生窗口（阻塞直到窗口关闭）。没有安装 pywebview 时返回 False"""
    try:
        import webview
    except Exception:
        return False
    w, h = _window_size()

    def on_closing():
        if app.busy():
            # 有任务在进行时，关闭前再确认一次
            return bool(win.create_confirmation_dialog("确认退出", "还有下载任务正在进行，退出会中止它们。确定退出吗？"))
        return True

    app.window_mode = "pywebview 原生窗口"
    log("使用 pywebview 原生窗口")
    win = webview.create_window("Hugging Face 模型极速下载器", url, width=w, height=h, min_size=(960, 680))
    try:
        win.events.closing += on_closing
    except Exception:
        pass
    webview.start()
    return True


def _wait_window_closed(app, proc):
    """窗口还开着就一直等；关掉后立刻返回"""
    app.last_seen = time.time()
    while True:
        time.sleep(1)
        now = time.time()
        idle = now - app.last_seen
        bye = app.bye_at
        if bye and now - bye >= BYE_GRACE and app.last_seen <= bye:
            return                              # 网页发来关闭通知，且没有重新连上（不是刷新）
        if proc is not None:
            # 窗口进程在：永远不因“闲置”退出（最小化、后台标签页降频都不影响）
            if proc.poll() is None:
                continue
            # 进程已退出（可能只是 Edge 把网址交给了已在运行的实例）：
            # 只要网页还在联系就一直留着，兜底时间也放得很宽
            if idle > PROC_GONE_IDLE:
                return
            continue
        if idle > IDLE_EXIT:
            return                              # 普通浏览器：长时间完全没有请求，兜底退出


def _self_check():
    """自检：模块能不能导入、配置能不能读、本地服务能不能起（不打开窗口）"""
    out = ["Python  %s  %s" % (sys.version.split()[0], platform.machine()), "程序目录  " + APP_DIR]
    try:
        from core.server import App, start_server, STATIC_DIR, CONFIG_PATH
        out.append("导入模块  正常")
        out.append("配置文件  " + CONFIG_PATH)
        out.append("网页文件  " + os.path.join(STATIC_DIR, "index.html"))
        app = App()
        out.append("读取配置  正常（%d 条历史记录）" % len(app.basic.rows))
        out.append("下载工具  IDM: %s | aria2: %s" % (app.batch.s["idm_path"] or "未找到",
                                                    app.batch.s["aria2_path"] or "未找到"))
        httpd, port = start_server(app, 0)
        out.append("本地服务  正常（端口 %d）" % port)
        httpd.shutdown()
        out.append("全部正常，可以启动。")
    except Exception as e:
        out.append("出错了：" + repr(e))
        log_exc("自检失败")
        raise
    finally:
        log("自检结果：\n" + "\n".join(out))
    return "\n".join(out)


def main():
    if "--check" in sys.argv:
        text = _self_check()
        try:
            print(text)
        except Exception:
            pass
        return

    use_browser = "--browser" in sys.argv
    port = 0
    if "--port" in sys.argv:
        try:
            port = int(sys.argv[sys.argv.index("--port") + 1])
        except Exception:
            port = 0

    log("=" * 64)
    log("启动：Python %s / %s" % (sys.version.split()[0], platform.machine()))
    log("解释器：" + (sys.executable or "未知"))
    try:
        import importlib.util
        ok = importlib.util.find_spec("huggingface_hub") is not None
        log("huggingface_hub：" + ("已安装" if ok else "没找到（下载会失败，请用 Anaconda 环境里的 python）"))
    except Exception:
        pass
    log("参数：%s" % (" ".join(sys.argv[1:]) or "（无）"))

    from core.server import App, start_server
    from core.common import LOOPBACK
    app = App()
    log("读取配置完成")
    if sys.stdout is None or sys.stderr is None or not sys.stdout.isatty():
        sys.stdout = sys.stderr = _HubWriter(app.hub)
    httpd, port = start_server(app, port)
    url = f"http://{LOOPBACK}:{port}/"
    log("本地服务已启动：" + url)

    try:
        if not use_browser and _run_pywebview(url, app):
            return
        proc = None if use_browser else _open_edge_app(url)
        if proc is not None:
            app.window_mode = "Edge 应用窗口"
            log("已打开 Edge 应用窗口")
        else:
            app.window_mode = "默认浏览器"
            log("没有可用的 Edge 应用窗口，改用默认浏览器")
            import webbrowser
            webbrowser.open(url)
        _wait_window_closed(app, proc)
        log("窗口已关闭，退出")
    finally:
        app.shutdown()
        httpd.shutdown()


if __name__ == "__main__":
    mp.freeze_support()
    try:
        main()
    except BaseException as e:          # pythonw 下没有控制台，出错必须让人看得见
        log_exc("启动失败")
        show_error("Hugging Face 模型极速下载器 —— 启动失败",
                   "程序没能启动：\n\n%s\n\n详细信息已写到：\n%s\n\n"
                   "可以双击“诊断启动.bat”，它会列出完整的报错。"
                   % (e, os.path.join(os.path.dirname(APP_DIR), "启动日志.log")))
        raise
