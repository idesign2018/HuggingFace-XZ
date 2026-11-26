# -*- coding: utf-8 -*-
"""本地网页服务：给网页界面提供数据接口。

  GET  /                → 网页界面
  GET  /static/<文件>    → 网页用到的 css / js
  POST /api/<名称>       → 接口（JSON 进，JSON 出）

只监听本机地址，每个请求都要带随机密钥（防止别的网页调用本地接口）。
网页每 0.5 秒调用一次 /api/poll，一次取回：新日志、新提示、两个页面的变化。
"""
import os
import json
import time
import secrets
import threading
import urllib.parse
import webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

from core.common import FILTER_PRESETS, IDM_DEFAULT_HINT, LOOPBACK
from core import diag, version, net
from core.about import AboutApiMixin, CONTENT_DIR, CONTENT_KINDS
from core.hub import LogHub
from batch.aria2 import (ARIA2_FIELDS, ARIA2_DEFAULTS, PROXY_MODES, FILE_ALLOC_CHOICES,
                         preview_opts, describe_proxy)
from batch.filter import legacy_to_query
from batch.manager import BatchManager
from basic.manager import BasicManager, DEFAULT_WORKERS, DEFAULT_THREADS
from basic.segdl import MAX_THREADS
from basic.api import BasicApiMixin
from batch.api import BatchApiMixin
from shellops import dialog as hfd_dialog

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # webui 目录（主程序所在目录）
STATIC_DIR = os.path.join(APP_DIR, "static")
PROGRAM_DIR = os.path.dirname(APP_DIR)              # 配置文件和旧版程序放在一起，可以共用
CONFIG_PATH = os.path.join(PROGRAM_DIR, "hf_downloader_config.json")
HISTORY_PATH = os.path.join(PROGRAM_DIR, "hf_download_history.json")   # 常规下载的历史记录

MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
        ".ico": "image/x-icon", ".woff2": "font/woff2"}


class App(BasicApiMixin, BatchApiMixin, AboutApiMixin):
    """程序主体：配置 + 两个下载功能 + 接口分发"""

    def __init__(self, history_path=None):
        self.key = secrets.token_urlsafe(24)
        self.hub = LogHub()
        self.cfg_lock = threading.Lock()
        self.cfg = self._read_config()
        self.basic = BasicManager(self.hub, history_path or HISTORY_PATH)
        self.batch = BatchManager(self.hub, lambda: (self.cfg.get("token") or "").strip(), self.save_config)
        self.batch.load_config(self.cfg)
        self.batch.s["source"] = self.source()      # 下载源是全局设置（左侧导航栏“源”按钮），两个页面共用
        self.basic.listeners.append(self.batch.on_basic_states)     # 常规下载的状态同步到批量清单
        self.basic.make_jobs = lambda rows: self._jobs_from_rows(rows, False)[0]   # 重启下载进程时续传其余文件
        self.started_at = time.time()
        self.last_seen = time.time()
        self.bye_at = 0.0
        self._page_seen = False     # 网页第一次连上来时写一行日志（排查“打不开”用）            # 网页发出“窗口已关闭”通知的时间（刷新页面会在几秒内重新连上）

    # ---------------- 配置 ----------------
    def _read_config(self):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except Exception as e:
            self.hub.log("basic", f"配置文件读取失败: {e}")
            return {}

    def save_config(self):
        """先读出旧配置再合并，避免覆盖掉旧版程序或其他页面的设置"""
        with self.cfg_lock:
            data = self._read_config()
            data.update({k: v for k, v in self.cfg.items() if not k.startswith("idm_")})
            data.update(self.batch.dump_config())
            try:
                tmp = CONFIG_PATH + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(tmp, CONFIG_PATH)
            except Exception as e:
                self.hub.log("basic", f"配置保存失败: {e}")

    def basic_settings(self):
        c = self.cfg
        return {"token": c.get("token", ""), "repo": c.get("repo_id", ""), "path": c.get("local_dir") or os.getcwd(),
                "use_mirror": self.use_mirror(), "no_symlinks": c.get("no_symlinks", True),
                "workers": c.get("web_basic_workers", DEFAULT_WORKERS), "high_perf": c.get("basic_high_perf", False),
                "threads": c.get("web_basic_threads", DEFAULT_THREADS), "pattern": c.get("allow_patterns", "")}

    def set_basic_settings(self, b):
        c = self.cfg
        mapping = {"token": "token", "repo": "repo_id", "path": "local_dir",
                   "no_symlinks": "no_symlinks", "workers": "web_basic_workers", "high_perf": "basic_high_perf",
                   "threads": "web_basic_threads", "pattern": "allow_patterns"}
        for k, ck in mapping.items():
            if k in b:
                v = b[k]
                c[ck] = v.strip() if isinstance(v, str) else v
        try:
            c["web_basic_workers"] = max(1, min(16, int(c.get("web_basic_workers") or DEFAULT_WORKERS)))
        except Exception:
            c["web_basic_workers"] = DEFAULT_WORKERS
        try:
            c["web_basic_threads"] = max(1, min(MAX_THREADS, int(c.get("web_basic_threads") or DEFAULT_THREADS)))
        except Exception:
            c["web_basic_threads"] = DEFAULT_THREADS

    # ---------------- 下载源（全局：国内镜像 / 官方） ----------------
    def source(self):
        s = self.cfg.get("source")
        if s in ("mirror", "official"):
            return s
        return "mirror" if self.cfg.get("use_mirror", True) else "official"     # 旧配置：沿用常规页的镜像开关

    def use_mirror(self):
        return self.source() == "mirror"

    def api_set_source(self, b):
        """左侧导航栏“源”按钮：切换下载源（两个页面同时生效）。旧版配置里的两个开关也一起改，保持一致"""
        src = b.get("source")
        if src not in ("mirror", "official"):
            return {"error": "未知的下载源"}
        self.cfg["source"] = src
        self.cfg["use_mirror"] = src == "mirror"
        self.batch.update_settings({"settings": {"source": src}})
        self.save_config()
        text = net.describe(src)
        name = "国内镜像 hf-mirror.com" if src == "mirror" else "官方 huggingface.co"
        for ch in ("basic", "batch"):
            self.hub.log(ch, f"下载源已切换为：{name}。{text}")
        r = {"ok": True, "source": src, "text": text}
        if self.basic.running:
            r["note"] = "正在进行的常规下载任务继续用原来的下载源；下一次开始下载时用新的下载源。"
        return r

    # ---------------- 接口 ----------------
    def api(self, name, b):
        fn = getattr(self, "api_" + name, None)
        if fn is None:
            return {"error": f"未知接口：{name}"}
        return fn(b)

    def api_init(self, b):
        if not self._page_seen:
            self._page_seen = True
            diag.log("网页已连接（界面加载成功）")
        presets = {k: legacy_to_query({"ext": v}) for k, v in FILTER_PRESETS.items()}    # 常用类型 → 筛选框写法
        return {"about": {"name": version.APP_NAME, "version": version.VERSION, "build": version.BUILD_DATE,
                          "author": version.AUTHOR},
                "basic": self.basic_settings(), "batch": self.batch.settings(), "presets": presets,
                "source": self.source(),
                "aria2_fields": ARIA2_FIELDS, "aria2_defaults": ARIA2_DEFAULTS, "proxy_modes": PROXY_MODES,
                "file_alloc": FILE_ALLOC_CHOICES, "idm_hint": IDM_DEFAULT_HINT, "config_path": CONFIG_PATH}

    def api_poll(self, b):
        seq, logs, notes = self.hub.since(int(b.get("seq") or 0))
        bb = b.get("basic") or {}
        tb = b.get("batch") or {}
        bstruct = bb.get("struct")
        return {"seq": seq, "logs": logs, "notices": notes,
                "basic": self.basic.snapshot(-1 if bstruct is None else int(bstruct), int(bb.get("since") or 0)),
                "batch": self.batch.snapshot(int(tb.get("rev") or -1), int(tb.get("struct") or -1))}

    # 其他
    def api_dialog(self, b):
        return hfd_dialog.ask(b)

    def api_open_url(self, b):
        url = str(b.get("url") or "")
        if url.startswith(("https://", "http://")):
            webbrowser.open(url)
        return {"ok": True}

    def api_bye(self, b):
        """网页在关闭窗口前发来：几秒内没有重新连上（刷新）就退出程序"""
        self.bye_at = time.time()
        return {"ok": True}

    def busy(self):
        return self.basic.running or self.batch.busy

    def shutdown(self):
        self.basic.shutdown()
        self.batch.shutdown()


def make_handler(app):
    index_cache = {}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):       # 不在控制台打印每个请求
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _key_ok(self, path, query):
            if self.headers.get("X-HFD-Key") == app.key:
                return True
            # 关闭窗口时用 sendBeacon 发送，无法带自定义请求头：只对“关闭通知”放行，密钥放在地址里
            if path == "/api/bye":
                q = urllib.parse.parse_qs(query)
                return (q.get("k") or [""])[0] == app.key
            return False

        def do_GET(self):
            path, _, query = self.path.partition("?")
            if path in ("/", "/index.html"):
                if "html" not in index_cache:
                    with open(os.path.join(STATIC_DIR, "index.html"), "r", encoding="utf-8") as f:
                        index_cache["html"] = f.read()
                html = index_cache["html"].replace("__HFD_KEY__", app.key).replace("__HFD_VER__", str(int(time.time())))
                return self._send(200, html, MIME[".html"])
            if path.startswith("/static/"):
                name = path[len("/static/"):]
                if not name or ".." in name or "\\" in name or ":" in name or name.startswith("/"):
                    return self._send(404, "not found", "text/plain")
                full = os.path.join(STATIC_DIR, name)
                if os.path.isfile(full):
                    with open(full, "rb") as f:
                        data = f.read()
                    return self._send(200, data, MIME.get(os.path.splitext(full)[1].lower(), "application/octet-stream"))
            if path.startswith("/content/"):
                name = urllib.parse.unquote(path[len("/content/"):])
                full = os.path.normpath(os.path.join(CONTENT_DIR, name))
                if (".." in name or not full.startswith(CONTENT_DIR + os.sep) or not os.path.isfile(full)):
                    return self._send(404, "not found", "text/plain")
                ctype = CONTENT_KINDS.get(os.path.splitext(full)[1].lower())
                if not ctype:
                    return self._send(404, "not found", "text/plain")
                with open(full, "rb") as f:
                    return self._send(200, f.read(), ctype)
            if path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            return self._send(404, "not found", "text/plain")

        def do_POST(self):
            path, _, query = self.path.partition("?")
            if not path.startswith("/api/"):
                return self._send(404, json.dumps({"error": "not found"}))
            if not self._key_ok(path, query):
                return self._send(403, json.dumps({"error": "forbidden"}))
            try:
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n).decode("utf-8") if n else ""
                body = json.loads(raw) if raw.strip().startswith("{") else {}
            except Exception:
                return self._send(400, json.dumps({"error": "bad request"}))
            app.last_seen = time.time()
            try:
                result = app.api(path[len("/api/"):], body)
            except Exception as e:
                diag.log_exc("接口 %s 出错" % path)
                result = {"error": f"程序出错：{e}"}
            self._send(200, json.dumps(result, ensure_ascii=False))

    return Handler


def start_server(app, port=0):
    """启动服务（后台线程），返回 (server, 实际端口)"""
    httpd = ThreadingHTTPServer((LOOPBACK, port), make_handler(app))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.25}, daemon=True).start()
    return httpd, httpd.server_address[1]
