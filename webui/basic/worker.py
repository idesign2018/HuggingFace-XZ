# -*- coding: utf-8 -*-
"""
下载引擎（在下载子进程中运行），“常规”页的所有下载都用它：
  - 常规页填仓库 ID 下载：先获取文件列表，再逐个文件下载
  - 批量页“用常规下载”、列表右键“继续下载 / 重新下载”：直接下载给定的文件

功能：
  - 同时下载 N 个文件；每个文件单独的进度（每 0.4 秒汇总发送一次）
  - 下载过程中可以追加文件（控制消息 add），可以停止单个文件（控制消息 stop）
  - 停止的文件保留已下载的部分（.incomplete），下次“继续下载”时自动续传
  - 自定义保存路径：先下载到目标文件夹中的 .hfd_staging，完成后移动到目标位置
"""
import os
import re
import time
import queue
import shutil
import fnmatch
import inspect
import threading
import urllib.error
from collections import deque

from core.common import (ENDPOINTS, human_size, fmt_duration, build_resolve_url, mirror_redirect_target,
                         sanitize_component, fetch_repo_files, explain_http_error)
from basic import segdl

DONE, SIZE, STATE, BAR, T0, T1, ERR = range(7)
IDX = {"done": DONE, "size": SIZE, "state": STATE, "bar": BAR, "t0": T0, "t1": T1, "err": ERR}
REPORT_INTERVAL = 0.4
STAGING = ".hfd_staging"


class StopFile(Exception):
    """用户停止了这个文件"""


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _http_hint(e):
    """把常见的 HTTP 错误码翻译成人话"""
    return {
        401: "401 未授权：这个仓库需要 HF Token（私有 / 受限仓库）",
        403: "403 拒绝访问：Token 没有权限，或还没在网页上同意该仓库的使用协议",
        404: "404 未找到：仓库里没有这个文件，或仓库 ID 写错了",
        429: "429 请求过多：被限流了，过一会儿再试，或把“每文件连接”调小",
    }.get(getattr(e, "code", None), f"HTTP {getattr(e, 'code', None)}：{e}")


def _status_code(e):
    """从 urllib / huggingface_hub 的异常里取 HTTP 状态码"""
    resp = getattr(e, "response", None)
    return getattr(resp, "status_code", None) or getattr(e, "code", None)


def run_engine(p, q, stop_evt, ctrl):
    def log(s):
        q.put(("log", s + "\n"))

    eng = Engine(p, q, stop_evt, ctrl, log)
    log("hf_xet 高性能模式：" + ("开启" if p.get("high_perf") else "关闭") + f"；同时下载 {p['workers']} 个文件"
        + (f"，每个文件最多 {eng.file_threads} 个连接（大文件分段多线程下载）" if eng.file_threads > 1
           else "，每个文件 1 个连接"))
    if p.get("repo"):
        jobs = list_repo_jobs(p, log, q)
        if not jobs:
            return
        q.put(("jobs", jobs))
    else:
        jobs = p["jobs"]
        n_repo = len(set(j["repo"] for j in jobs))
        total = sum(j.get("size") or 0 for j in jobs)
        log(f"\n--- 开始任务：{len(jobs)} 个文件（{n_repo} 个仓库），合计 {human_size(total)}"
            f"（HF {human_size(total, 1000)}） ---")
    q.put(("ack", 0, eng.add(jobs)))
    eng.run()


def _single_file(pattern):
    """pattern 是不是“一个明确的文件名”（没有通配符，也没有空格 / 逗号分隔的多个条件）。
    是就返回这个路径：这时不必拉整个仓库的文件列表。"""
    t = (pattern or "").strip().strip('"').strip()
    if not t or any(c in t for c in "*?["):
        return None
    if re.search(r"[\s,;，；、|]", t):
        return None
    return t.lstrip("/")


def list_repo_jobs(p, log, q):
    """常规页：获取仓库文件列表 → 下载任务（每个文件直接下载到“保存路径 + HF 原路径”）

    只填了一个明确的文件名时，不拉整个仓库的文件列表：直接按这个名字生成任务，文件大小在
    开始下载时探一次就得到。仓库文件多、镜像站又慢的时候，列表接口动不动十几秒甚至一分钟，
    这一步省掉，点“开始下载”基本立刻就开始传。
    """
    repo, path, pattern, token = p["repo"], p["path"], p["pattern"], p["token"]
    endpoint = ENDPOINTS["mirror"] if p["use_mirror"] else ENDPOINTS["official"]
    log(f"\n--- 开始任务: {repo} ---")
    log(f"下载源: {endpoint}\n下载目录: {path}")
    if token:
        log("已填写 HF Token：先不带 Token 请求，服务器要求登录（401 / 403）时才带上")

    one = _single_file(pattern)
    if one:
        local = os.path.normpath(os.path.join(path, *one.split("/")))
        log(f"只下载这一个文件：{one}（跳过“获取整个仓库的文件列表”，大小在开始时探一次就得到）")
        return [{"id": os.path.normcase(local), "repo": repo, "repo_type": "model", "revision": "main", "hf": one,
                 "size": None, "label": one, "local": local, "local_dir": path, "direct": True,
                 "use_mirror": p["use_mirror"], "token": token, "use_token": False,
                 "source": f"常规下载：{repo}"}]

    log("正在获取文件列表...")
    t_list = time.time()
    try:
        try:
            info, listed = fetch_repo_files(endpoint, "model", repo, "main", None, log=log)     # 先不带 Token
        except urllib.error.HTTPError as e:
            if e.code not in (401, 403, 404) or not token:
                raise
            log(f"未带 Token 访问失败（HTTP {e.code}），正在带上 Token 重试…"
                + ("（Token 会经过 hf-mirror.com）" if p["use_mirror"] else ""))
            info, listed = fetch_repo_files(endpoint, "model", repo, "main", token, log=log)
    except Exception as e:
        msg = explain_http_error(e) if getattr(e, "code", None) else f"获取文件列表失败：{e}"
        log(f"❌ {msg}")
        q.put(("notice", "error", "获取文件列表失败", msg))
        return None
    files = [(f["path"], f["size"]) for f in listed if f.get("path")]
    log(f"文件列表获取完成：{len(files)} 个文件，用时 {time.time() - t_list:.1f} 秒")
    if pattern:
        log(f"文件过滤: 仅下载 {pattern}")
        files = [f for f in files if fnmatch.fnmatch(f[0], pattern)]
    if not files:
        log("❌ 没有符合条件的文件。")
        q.put(("notice", "warn", "没有文件", "没有符合条件的文件，请检查仓库ID或文件过滤条件。"))
        return None
    total = sum(s or 0 for _, s in files)
    log(f"共 {len(files)} 个文件，合计 {human_size(total)}（HF {human_size(total, 1000)}）")
    revision = info.get("sha") or "main"
    needs = bool(info.get("gated")) or bool(info.get("private"))    # 受限 / 私有：官方源一开始就带 Token
    out = []
    for f, s in files:
        local = os.path.normpath(os.path.join(path, *f.split("/")))
        out.append({"id": os.path.normcase(local), "repo": repo, "repo_type": "model", "revision": revision, "hf": f,
                    "size": s, "label": f, "local": local, "local_dir": path, "direct": True,
                    "use_mirror": p["use_mirror"], "token": token, "use_token": needs,
                    "source": f"常规下载：{repo}"})
    return out


class Engine:
    def __init__(self, p, q, stop_evt, ctrl, log):
        self.p, self.q, self.stop_evt, self.ctrl, self.log = p, q, stop_evt, ctrl, log
        self._hub_mod = None        # huggingface_hub 导入要 2~4 秒，只有真要单连接下载时才导入
        try:
            self.file_threads = max(1, min(segdl.MAX_THREADS, int(p.get("file_threads") or 1)))
        except Exception:
            self.file_threads = 1
        self.cv = threading.Condition()
        self.jobs, self.st = {}, {}
        self.pending, self.active, self.stop_ids, self.dirty = deque(), set(), set(), set()
        self.endpoints, self.ep_lock = {}, threading.Lock()
        self.staging = set()
        self.closing = False
        self.finished = threading.Event()
        self.counter = 0

    # ---------------- 任务管理（任何线程都可以调用） ----------------
    def add(self, jobs):
        """加入下载队列；返回实际加入的文件 id（正在下载的会跳过）；引擎正在退出时返回 None（主进程会重新启动下载进程）"""
        ids = []
        with self.cv:
            if self.closing:
                return None
            for j in jobs:
                k = j["id"]
                if k in self.active or k in self.pending:
                    continue
                self.jobs[k] = j
                self.st[k] = [0, j.get("size"), "等待中", False, None, None, ""]
                self.stop_ids.discard(k)
                self.pending.append(k)
                self.dirty.add(k)
                ids.append(k)
            self.cv.notify_all()
        return ids

    def stop(self, ids):
        with self.cv:
            for k in ids:
                if k in self.active:
                    self.stop_ids.add(k)            # 下一次收到数据时中断
                elif k in self.pending:
                    self.pending.remove(k)
                    self._set(k, state="已停止")

    def _set(self, k, **kw):
        row = self.st[k]
        for name, v in kw.items():
            row[IDX[name]] = v
        self.dirty.add(k)

    # ---------------- 线程 ----------------
    def _reporter(self):
        while True:
            stop = self.finished.wait(REPORT_INTERVAL)
            with self.cv:
                batch = {k: list(self.st[k]) for k in self.dirty}
                self.dirty.clear()
            if batch:
                self.q.put(("upd", batch))
            if stop:
                return

    def _control(self):
        while not self.finished.is_set():
            try:
                msg = self.ctrl.get(timeout=0.3)
            except queue.Empty:
                continue
            except (EOFError, OSError):
                return
            if msg[0] == "add":                  # ("add", 序号, 任务列表)
                self.q.put(("ack", msg[1], self.add(msg[2])))
            elif msg[0] == "stop":
                self.stop(msg[1])

    def _worker(self):
        while True:
            with self.cv:
                while not self.pending and not self.closing:
                    self.cv.wait(0.5)
                if not self.pending:
                    return
                k = self.pending.popleft()
                self.active.add(k)
                self._set(k, state="下载中", t0=time.time(), err="")
            try:
                self._download(k)
            finally:
                with self.cv:
                    self.active.discard(k)
                    self.stop_ids.discard(k)
                    self.cv.notify_all()

    def run(self):
        workers = [threading.Thread(target=self._worker, daemon=True) for _ in range(self.p["workers"])]
        helpers = [threading.Thread(target=self._reporter, daemon=True), threading.Thread(target=self._control, daemon=True)]
        for t in workers + helpers:
            t.start()
        t_start = time.time()
        while True:
            with self.cv:
                if self.stop_evt.is_set() and self.pending:      # “停止”：不再开始新的文件
                    for k in list(self.pending):
                        self._set(k, state="已停止")
                    self.pending.clear()
                idle = not self.pending and not self.active
            if not idle:
                time.sleep(0.3)
                continue
            time.sleep(0.6)             # 再等一下，接收可能刚发来的“继续/追加”请求
            with self.cv:
                if not self.pending and not self.active:
                    self.closing = True
                    self.cv.notify_all()
                    break
        for t in workers:
            t.join()
        self.finished.set()
        for t in helpers:
            t.join(3)
        self._cleanup_staging()
        self._summary(time.time() - t_start)

    # ---------------- 下载一个文件 ----------------
    def _hub(self):
        """按需导入 huggingface_hub（导入比较慢，分段下载用不到它，就不必等这几秒）"""
        if self._hub_mod is None:
            t = time.time()
            from huggingface_hub import hf_hub_download
            self._hub_mod = (hf_hub_download, inspect.signature(hf_hub_download).parameters)
            self.log(f"已加载 huggingface_hub（{time.time() - t:.1f} 秒）")
        return self._hub_mod

    def _endpoint(self, j):
        """文件从哪个源下载。这里不做任何网络检查 —— 分段下载自己会跟随跳转，
        省掉每个仓库一次白等的请求（镜像站慢的时候这一步就要好几秒）"""
        return ENDPOINTS["mirror"] if j.get("use_mirror") else ENDPOINTS["official"]

    def _hub_endpoint(self, j):
        """huggingface_hub 不支持“镜像站把下载转到官方”，用它下载前才需要确认（每个仓库只查一次）"""
        ep = self._endpoint(j)
        if ep == ENDPOINTS["official"]:
            return ep
        spec = (j["repo"], j["repo_type"], j["revision"])
        with self.ep_lock:
            if spec not in self.endpoints:
                target = mirror_redirect_target(build_resolve_url(ep, spec[1], spec[0], spec[2], j["hf"]))
                if target:
                    self.log(f"⚠ {spec[0]}：镜像站目前会把下载转到官方 {target}，这个仓库已自动改用官方源下载。")
                self.endpoints[spec] = ENDPOINTS["official"] if target else ep
            return self.endpoints[spec]

    def _progress_class(self, k):
        eng = self

        class _FileProgress:
            """交给 hf_hub_download 的进度接收器：只累加字节数；文件被停止时中断下载"""
            def __init__(self, *args, **kwargs):
                with eng.cv:
                    row = eng.st[k]
                    row[BAR] = True
                    if not row[SIZE] and kwargs.get("total"):
                        row[SIZE] = kwargs.get("total")
                    row[DONE] = kwargs.get("initial") or 0
                    eng.dirty.add(k)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def update(self, n=1):
                if k in eng.stop_ids:
                    raise StopFile()
                with eng.cv:
                    eng.st[k][DONE] += n or 0
                    eng.dirty.add(k)

            def close(self):
                pass

            def refresh(self, *args, **kwargs):
                pass

            def set_description(self, *args, **kwargs):
                pass
        return _FileProgress

    def _download(self, k):
        j = self.jobs[k]
        label = j.get("label") or j["hf"]
        target = j["local"]
        try:
            ep = self._endpoint(j)
            if j["direct"]:
                local_dir, dest = j["local_dir"], target
            else:       # 每个仓库单独一个临时文件夹，避免不同仓库的同名文件互相覆盖
                stage = os.path.join(os.path.dirname(target), STAGING)
                local_dir = os.path.join(stage, sanitize_component(j["repo"].replace("/", "_"))[0])
                with self.cv:
                    self.staging.add(stage)
                dest = os.path.join(local_dir, *j["hf"].split("/"))
            if k in self.stop_ids:
                raise StopFile()
            path = dest if self._by_segments(k, j, ep, dest, label) else self._by_hub(k, j, local_dir)
            if not j["direct"]:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                os.replace(path, target)
            with self.cv:
                row = self.st[k]
                row[T1] = time.time()
                row[STATE] = "完成" if row[BAR] else "已是最新"
                if row[SIZE]:
                    row[DONE] = row[SIZE]
                self.dirty.add(k)
                self.counter += 1
                n, size, bar, cost = self.counter, row[SIZE], row[BAR], row[T1] - (row[T0] or row[T1])
            if bar:
                avg = (size or 0) / cost if cost > 0 else 0
                self.log(f"✅ [{n}] 完成：{label}（{human_size(size)}，用时 {fmt_duration(cost)}，平均 {human_size(avg)}/s）")
        except Exception as e:
            with self.cv:
                stopped = isinstance(e, StopFile) or k in self.stop_ids
                if stopped:
                    self._set(k, state="已停止", t1=time.time())
                else:
                    self._set(k, state="失败", t1=time.time(), err=str(e))
            if stopped:
                self.log(f"⏹ 已停止：{label}（已下载的部分会保留，“继续下载”时续传）")
            else:
                self.log(f"❌ 失败：{label}：{e}")

    # ---------------- 两种下载方式 ----------------
    def _by_segments(self, k, j, ep, dest, label):
        """
        大文件：多线程分段下载（basic/segdl.py）。把文件切成很多小块同时下，
        每个连接单独计时（从断点重连续传），一条连接卡住不会拖住整个文件。
        返回 True = 这个文件已经处理完（完成 / 已是最新）；False = 交给 huggingface_hub 单连接下载。
        """
        if self.file_threads <= 1:
            return False                        # 只要 1 个连接：huggingface_hub 就够了
        size = j.get("size") or 0
        if size and size < segdl.SEG_MIN_SIZE:
            return False                        # 小文件单连接就够快，不必分段
        force = bool(j.get("force"))
        url = build_resolve_url(ep, j["repo_type"], j["repo"], j["revision"], j["hf"])
        stop = lambda: k in self.stop_ids       # noqa: E731  准备阶段也能马上停
        use_tok = bool(j.get("use_token")) and not j.get("use_mirror")      # 国内镜像一律先不带 Token
        prepared = None
        try:
            if not size:
                # 大小未知（只填了一个文件名，没拉仓库列表）：先探一次。
                # 这一次请求同时拿到文件大小、真正的下载地址、以及支不支持分段，后面直接复用
                t_probe = time.time()
                prepared = segdl.prepare(url, j.get("token"), use_tok, self.log, stop)
                size = prepared[1]
                with self.cv:
                    self.st[k][SIZE] = size
                    self.dirty.add(k)
                self.log(f"⇣ {label}：{human_size(size)}（探测用时 {time.time() - t_probe:.1f} 秒）")
                if size < segdl.SEG_MIN_SIZE:
                    return False                # 小文件单连接就够快，不必分段
            if stop():
                raise StopFile()
            if not force and os.path.isfile(dest) and os.path.getsize(dest) == size:
                return True                     # 已是最新
            if prepared is None:
                self.log(f"⇣ {label}：{human_size(size)}，{self.file_threads} 个连接分段下载")
            segdl.download(url, j.get("token"), dest, self.file_threads, self._progress_class(k), force=force,
                           log=self.log, label=label, prepared=prepared, use_token=use_tok, should_stop=stop)
        except segdl.Cancelled:
            raise StopFile()
        except segdl.Unsupported as e:
            self._drop_seg_files(dest)
            self.log(f"提示：{label} 的下载源不支持分段下载（{e}），改用单连接下载。")
            return False
        except urllib.error.HTTPError as e:
            raise RuntimeError(_http_hint(e))
        except urllib.error.URLError as e:
            raise RuntimeError(f"无法连接下载源：{e.reason}")
        return True

    def _drop_seg_files(self, dest):
        """分段的临时文件没用了，别留给下次误判"""
        for p in (dest + segdl.PART, dest + segdl.STATE, dest + segdl.STATE + ".tmp"):
            _remove(p)

    def _by_hub(self, k, j, local_dir):
        """单连接下载（huggingface_hub 官方实现）：小文件、或者下载源不支持分段时用"""
        dl, supported = self._hub()
        kwargs = {"repo_id": j["repo"], "filename": j["hf"], "revision": j["revision"], "repo_type": j["repo_type"],
                  "local_dir": local_dir, "endpoint": self._hub_endpoint(j), "tqdm_class": self._progress_class(k)}
        token = j.get("token") or None
        # 国内镜像一律先不带；官方源只有已知是受限 / 私有仓库才带（False = 明确不带，也不读本机登录的 Token）
        first = token if (token and j.get("use_token") and not j.get("use_mirror")) else False
        kwargs["token"] = first
        if j.get("force"):
            kwargs["force_download"] = True
        if "local_dir_use_symlinks" in supported:           # 旧版参数：需要移动的文件必须是真实文件
            kwargs["local_dir_use_symlinks"] = (not self.p["no_symlinks"]) if j["direct"] else False
        if "resume_download" in supported:
            kwargs["resume_download"] = True
        kwargs = {a: v for a, v in kwargs.items() if a in supported}
        try:
            return dl(**kwargs)
        except Exception as e:
            code = _status_code(e)
            if code not in (401, 403) or first or not token or "token" not in kwargs:
                raise
            self.log(f"{j.get('label') or j['hf']}：服务器要求登录（HTTP {code}），已带上 HF Token 重试"
                     + ("（Token 会经过 hf-mirror.com）" if j.get("use_mirror") else ""))
            kwargs["token"] = token
            return dl(**kwargs)

    # ---------------- 结束 ----------------
    def _cleanup_staging(self):
        """删除临时文件夹；里面还有没下完的文件（.incomplete，用于续传）时保留"""
        for d in self.staging:
            if not os.path.isdir(d):
                continue
            partial = any(f.endswith((".incomplete", segdl.PART)) for _, _, fs in os.walk(d) for f in fs)
            if partial:
                self.log(f"提示：{d} 中保留了未下完的部分（继续下载时会续传）。")
            else:
                shutil.rmtree(d, ignore_errors=True)

    def _summary(self, cost):
        states = [r[STATE] for r in self.st.values()]
        n_new, n_skip = states.count("完成"), states.count("已是最新")
        n_fail, n_stop = states.count("失败"), states.count("已停止")
        self.log(f"--- 任务结束：新下载 {n_new} 个，已是最新（跳过）{n_skip} 个，失败 {n_fail} 个，"
                 f"已停止 {n_stop} 个；总用时 {fmt_duration(cost)} ---")
        if n_fail:
            self.q.put(("notice", "warn", "部分文件失败",
                        f"有 {n_fail} 个文件下载失败（原因见日志）。\n在列表中选中它们，右键 →“继续下载”即可重试（已下载的部分会续传）。"))
        elif n_stop:
            self.q.put(("notice", "info", "已停止", f"有 {n_stop} 个文件已停止。\n需要时在列表中右键 →“继续下载”。"))
        elif states:
            self.log("\n✅ 下载完成！")
            done = n_new + n_skip
            self.q.put(("notice", "ok", "下载完成",
                        f"{done} 个文件已就绪" + (f"（其中 {n_skip} 个本地已是最新）" if n_skip else "") +
                        f"，用时 {fmt_duration(cost)}。"))
