# -*- coding: utf-8 -*-
"""
常规下载（网页版）—— 主进程部分：启动/控制下载子进程、整理进度、保存历史记录。
真正的下载在子进程中进行（basic/worker.py），不会和界面争抢 Python 的全局锁（GIL），界面不卡。

  - 列表 = 历史记录：所有下载过的文件都保留（保存在 hf_download_history.json），可以手动删除
  - 右键菜单：打开 / 打开方式 / 打开文件夹 / 继续下载 / 重新下载 / 停止 / 从列表移除 / 属性
  - 下载过程中“继续 / 重新下载”别的文件：直接加入正在运行的任务，不用等
"""
import os
import sys
import time
import queue
import threading
import multiprocessing as mp
from collections import deque

from core.history import History
from shellops import files as shell
from basic.segdl import MAX_THREADS

SPEED_WINDOW = 3.0          # 速度 = 最近这么多秒的实际下载量（实时速度，不是全程平均）
STOP_GRACE = 4.0            # “停止下载”后等这么多秒还没停下，就重启下载进程
STALL_WARN = 60             # 正在下载的文件这么多秒没收到数据，提醒一次
DEFAULT_WORKERS = 4
DEFAULT_THREADS = MAX_THREADS   # 每个文件的连接数：默认用最大值
DONE, SIZE, STATE, BAR, T0, T1, ERR = range(7)


def calc_speed(samples, now, value):
    """根据最近 SPEED_WINDOW 秒的采样计算速度（字节/秒）"""
    samples.append((now, value))
    while len(samples) > 2 and now - samples[0][0] > SPEED_WINDOW:
        samples.popleft()
    t0, v0 = samples[0]
    if now - t0 < 0.8:
        return None
    return max(0.0, (value - v0) / (now - t0))


class _QueueWriter(object):
    def __init__(self, q):
        self.q = q

    def write(self, s):
        if s:
            self.q.put(("log", s))

    def flush(self):
        pass


def _child_main(p, q, stop_evt, ctrl):
    """下载子进程入口"""
    t_boot = time.time()
    sys.stdout = sys.stderr = _QueueWriter(q)
    if p.get("high_perf"):                  # 必须在导入 huggingface_hub / hf_xet 之前设置
        os.environ["HF_XET_HIGH_PERFORMANCE"] = "1"
    else:
        os.environ.pop("HF_XET_HIGH_PERFORMANCE", None)
    os.environ.pop("HF_ENDPOINT", None)
    try:
        from core import net                    # 必须在导入 huggingface_hub / hf_xet 之前：它们读环境变量里的代理
        q.put(("log", "下载源：" + net.apply_env(not p.get("use_mirror", True)) + "\n"))
        from basic.worker import run_engine
        q.put(("log", f"下载进程已启动（{time.time() - t_boot:.1f} 秒）\n"))
        run_engine(p, q, stop_evt, ctrl)
    except Exception as e:
        q.put(("log", f"\n❌ 发生错误: {e}\n"))
        q.put(("notice", "error", "错误", f"下载失败:\n{e}"))
    finally:
        q.put(("end",))


class BasicManager:
    def __init__(self, hub, history_path=None):
        self.hub = hub
        self.lock = threading.RLock()
        self.proc = self.stop_evt = self.ctrl = None
        self.params = None                  # 当前下载进程的公共参数（同时下载数等）
        self.running = False
        self.run_id = 0
        self.rev = 0
        self.struct = 0                     # 行的增删/顺序变化
        self.rows, self.order = {}, []
        self.t_start = self.t_end = None
        self.run_ids = set()                # 本次运行的文件（用于统计）
        self._total_samples, self._file_samples, self._speed = deque(), {}, None
        self.source = ""
        self.make_jobs = None               # 行 → 下载任务（带 Token），由 App 设置；重启下载进程时用
        self._stall = {}                    # 卡住检测：文件 → [上次的已下载字节, 从什么时候开始没变, 是否已提醒]
        self.listeners = []                 # 文件状态变化时通知（批量页同步状态）
        self._ack_seq = 0
        self._acks = {}
        self.history = History(history_path) if history_path else None
        if self.history:
            for r in self.history.load():
                r.update(speed=None, rev=0)
                self.rows[r["id"]] = r
                self.order.append(r["id"])

    def log(self, s):
        self.hub.log("basic", s)

    # ---------------- 加入下载 ----------------
    def start(self, p, source=""):
        """常规页：填仓库 ID 下载（先在子进程中获取文件列表）"""
        if self.running:
            return {"error": "当前已有下载任务在进行，请等它完成或先停止。\n（也可以在列表中右键继续/重新下载单个文件，会自动加入当前任务）"}
        self.source = source or f"常规下载：{p['repo']}"
        return self._spawn(dict(p, jobs=None))

    def enqueue(self, jobs, p, source=""):
        """加入下载（批量页发送、右键继续/重新下载）。正在下载时加入当前任务，否则启动新的下载进程"""
        if not jobs:
            return {"info": "没有需要下载的文件。"}
        if self.running and self.ctrl is not None:
            jobs = self._same_source(jobs)
        with self.lock:
            for j in jobs:
                self._upsert(j)
            self.struct += 1
        if self.running and self.ctrl is not None:
            r = self._send_add(jobs)
            if r is not None:
                if source:
                    self.source = source
                return {"ok": True, "added": len(r), "joined": True}
            for _ in range(50):            # 下载进程正在退出：等它结束后重新启动
                if not self.running:
                    break
                time.sleep(0.1)
            if self.running:
                return {"error": "下载进程正在结束，请稍后再试。"}
        self.source = source or self.source
        return self._spawn(dict(p, repo="", jobs=jobs))

    def _same_source(self, jobs):
        """加入正在运行的下载任务：沿用这个任务的下载源（中途切换的下载源从下一次开始下载时生效）"""
        run_mirror = (self.params or {}).get("use_mirror")
        if run_mirror is None or all(bool(j.get("use_mirror")) == bool(run_mirror) for j in jobs):
            return jobs
        self.log("提示：当前下载任务用的是" + ("国内镜像" if run_mirror else "官方源")
                 + "，新加入的文件也先用它；切换后的下载源从下一次开始下载时生效。")
        return [dict(j, use_mirror=bool(run_mirror)) for j in jobs]

    def _send_add(self, jobs):
        with self.lock:
            self._ack_seq += 1
            seq = self._ack_seq
            ev = threading.Event()
            self._acks[seq] = [ev, None]
        try:
            self.ctrl.put(("add", seq, jobs))
        except Exception:
            return None
        ev.wait(5)
        with self.lock:
            return self._acks.pop(seq, [None, None])[1]

    def _upsert(self, j):
        """新建或更新一行（同一个本地文件只有一行）"""
        k = j["id"]
        old = self.rows.get(k)
        r = {kk: v for kk, v in j.items() if kk not in ("token", "force")}
        r.update(state="等待中", done=0, bar=False, t0=None, t1=None, err="", speed=None, rev=0,
                 added=old.get("added") if old else time.time())
        if old is None:
            self.order.append(k)
        self.rows[k] = r
        self._bump(r)
        self.run_ids.add(k)
        if self.history:
            self.history.touch()

    def _spawn(self, p):
        with self.lock:
            if self.running:
                return {"error": "当前已有下载任务在进行。"}
            self.running = True
            self.run_id += 1
            self.t_start, self.t_end = time.time(), None
            if p.get("repo"):
                self.run_ids = set()
            self._total_samples, self._file_samples, self._speed = deque(), {}, None
            self.rev += 1
        p = dict(p, run=self.run_id)
        self.params = {k: v for k, v in p.items() if k not in ("jobs", "repo", "pattern")}
        ctx = mp.get_context("spawn")
        q = ctx.Queue()
        self.stop_evt, self.ctrl = ctx.Event(), ctx.Queue()
        self.proc = ctx.Process(target=_child_main, args=(p, q, self.stop_evt, self.ctrl), daemon=True)
        try:
            self.proc.start()
        except Exception as e:
            with self.lock:
                self.running = False
                for k in list(self.run_ids):
                    r = self.rows.get(k)
                    if r and r["state"] == "等待中":
                        r["state"], r["err"] = "失败", str(e)
                        self._bump(r)
            return {"error": f"无法启动下载进程：{e}"}
        threading.Thread(target=self._reader, args=(self.proc, q), daemon=True).start()
        return {"ok": True}

    # ---------------- 停止 ----------------
    def stop(self, force=False):
        if not self.running:
            return {"info": "当前没有进行中的下载。"}
        if force:
            if self.proc is not None and self.proc.is_alive():
                self.proc.terminate()
            self.log("⏹ 已立即中止：下载进程已结束（未完成的文件可右键“继续下载”，会续传）。")
            return {"ok": True}
        if self.stop_evt is not None and not self.stop_evt.is_set():
            self.stop_evt.set()
            self.log("⏹ 已请求停止：不再开始新的文件；正在下载的文件会继续下完。需要马上结束请点“立即中止”。")
        return {"ok": True}

    def stop_files(self, ids, immediate=False):
        """停止选中的文件（正在下载的会中断，已下载的部分保留）
        immediate=True：不等待，马上结束下载进程（卡在连接里的文件也能停掉），其余文件自动继续"""
        with self.lock:
            ids = [k for k in ids if k in self.rows and self.rows[k]["state"] in ("下载中", "等待中")]
        if not ids:
            return {"info": "选中的文件没有在下载或等待中。"}
        if self.running and self.ctrl is not None:
            self.ctrl.put(("stop", ids))
            grace = 0.0 if immediate else STOP_GRACE
            threading.Thread(target=self._ensure_stopped, args=(ids, self.run_id, grace), daemon=True).start()
        return {"ok": True}

    def _ensure_stopped(self, ids, run_id, grace=STOP_GRACE):
        """
        正在下载的文件要等“收到下一块数据”才能中断；卡在连接里、一直收不到数据的文件中断不了。
        等 grace 秒还没停下 → 结束下载进程，其余正在下载/等待的文件自动重新开始（续传，不会从头下）。
        """
        deadline = time.time() + grace
        while True:
            time.sleep(0.3)
            with self.lock:
                if self.run_id != run_id or not self.running:
                    return
                stuck = [k for k in ids if k in self.rows and self.rows[k]["state"] == "下载中"]
                if not stuck:
                    return
                if time.time() < deadline:
                    continue
                others = [dict(self.rows[k]) for k in self.run_ids if k in self.rows and k not in ids
                          and self.rows[k]["state"] in ("下载中", "等待中")]
                params, proc = dict(self.params or {}), self.proc
                break
        self.log(f"⏹ {len(stuck)} 个文件卡在网络请求里、收不到数据，无法正常中断，已重启下载进程"
                 + (f"；其余 {len(others)} 个文件自动继续（续传）。" if others else "。"))
        if proc is not None and proc.is_alive():
            proc.terminate()
        for _ in range(100):
            if not self.running:
                break
            time.sleep(0.1)
        with self.lock:
            for k in ids:
                r = self.rows.get(k)
                if r and r["state"] in ("已中断", "下载中", "等待中"):
                    r["state"], r["speed"] = "已停止", None
                    self._bump(r)
            self.rev += 1
        self._notify(ids)
        if others and self.make_jobs is not None and not self.running:
            jobs = self.make_jobs(others)
            if jobs:
                self.enqueue(jobs, params, "")
            else:
                self.log("其余文件需要 HF Token 才能继续，请填写 Token 后右键“继续下载”。")

    def shutdown(self):
        if self.proc is not None and self.proc.is_alive():
            self.proc.terminate()
        self.save_history(force=True)

    # ---------------- 接收子进程消息 ----------------
    def _reader(self, proc, q):
        last_tick = 0.0
        ended = False
        while not ended:
            try:
                msg = q.get(timeout=0.5)
            except queue.Empty:
                msg = None
            except (EOFError, OSError):
                break
            if msg is not None:
                ended = self._handle(msg)
            now = time.time()
            if now - last_tick >= 0.5:
                last_tick = now
                self._tick(now)
            if msg is None and not proc.is_alive():
                break
        changed = []
        with self.lock:
            self.running = False
            self.t_end = time.time()
            for k, r in self.rows.items():
                if r["state"] in ("下载中", "等待中"):
                    r["state"] = "已中断" if r["state"] == "下载中" else "已停止"
                    r["speed"] = None
                    self._bump(r)
                    changed.append(k)
            for seq, a in list(self._acks.items()):        # 还在等确认的“加入”：让它重新启动进程
                a[0].set()
            self.rev += 1
        self._notify(changed)
        self.save_history(force=True)

    def _bump(self, r):
        self.rev += 1
        r["rev"] = self.rev

    def _handle(self, msg):
        kind = msg[0]
        if kind == "log":
            self.hub.log_raw("basic", msg[1])
        elif kind == "notice":
            self.hub.notice("basic", msg[1], msg[2], msg[3])
        elif kind == "jobs":                                # 常规页：子进程获取到的文件列表
            with self.lock:
                for j in msg[1]:
                    self._upsert(j)
                self.struct += 1
        elif kind == "ack":
            with self.lock:
                a = self._acks.get(msg[1])
                if a:
                    a[1] = msg[2]
                    a[0].set()
        elif kind == "upd":
            changed = []
            with self.lock:
                for k, v in msg[1].items():
                    r = self.rows.get(k)
                    if r is None:
                        continue
                    prev = r["state"]
                    if prev != v[STATE]:
                        changed.append(k)
                    r.update(done=v[DONE], size=v[SIZE], state=v[STATE], bar=v[BAR], t0=v[T0], t1=v[T1], err=v[ERR])
                    if r["state"] != "下载中" or prev != "下载中":
                        self._file_samples.pop(k, None)      # 重新开始时丢掉上一次的采样（速度要实时）
                        if r["state"] != "下载中":
                            r["speed"] = None
                    self._bump(r)
            if self.history:
                self.history.touch()
            self._notify(changed)
        elif kind == "end":
            return True
        return False

    def _notify(self, ids):
        if not ids:
            return
        with self.lock:
            states = {k: self.rows[k]["state"] for k in ids if k in self.rows}
        for fn in self.listeners:
            try:
                fn(states)
            except Exception as e:
                self.log(f"[提示] 同步状态失败：{e}")

    def _tick(self, now):
        with self.lock:
            rows = [self.rows[k] for k in self.run_ids if k in self.rows]
            transferred = sum(r["done"] for r in rows if r["bar"])
            self._speed = calc_speed(self._total_samples, now, transferred)
            stalled = self._check_stall(rows, now)
            for r in rows:
                if r["state"] == "下载中" and r["bar"]:
                    sp = calc_speed(self._file_samples.setdefault(r["id"], deque()), now, r["done"])
                    if sp != r["speed"]:
                        r["speed"] = sp
                        self._bump(r)
            self.rev += 1
        for r in stalled:
            name = r.get("label") or r.get("hf") or ""
            msg = (f"{name} 已经 {STALL_WARN} 秒没有收到数据，下载连接被卡住了。\n"
                   f"大文件会自动断开重连、从断点续传；如果一直不动，可以右键“停止下载”再“继续下载”，"
                   f"或换一个下载源（国内镜像 / 官方）后重试。")
            self.log("⚠ " + msg.replace("\n", ""))
            self.hub.notice("basic", "warn", "下载没有进展", msg)
        self.save_history()

    def _check_stall(self, rows, now):
        """返回刚刚超过 STALL_WARN 秒没有进展的文件（每个文件只提醒一次，有进展后重新计时）"""
        out = []
        for r in rows:
            k = r["id"]
            if r["state"] != "下载中" or not r["bar"]:      # 还在“准备中”（没开始传数据）不算卡住
                self._stall.pop(k, None)
                continue
            st = self._stall.get(k)
            if st is None or st[0] != r["done"]:
                self._stall[k] = [r["done"], now, False]
            elif not st[2] and now - st[1] >= STALL_WARN:
                st[2] = True
                out.append(r)
        return out

    def save_history(self, force=False):
        if not self.history:
            return
        with self.lock:
            rows = [self.rows[k] for k in self.order if k in self.rows]
        self.history.save(rows, force=force)

    # ---------------- 列表操作（右键菜单） ----------------
    def get_rows(self, ids):
        with self.lock:
            return [dict(self.rows[k]) for k in ids if k in self.rows]

    def remove(self, ids, delete_files=False):
        with self.lock:
            busy = [k for k in ids if k in self.rows and self.rows[k]["state"] in ("下载中", "等待中")]
            if busy:
                return {"info": f"有 {len(busy)} 个文件正在下载或等待中，请先“停止”再移除。"}
            n_del = 0
            for k in ids:
                r = self.rows.pop(k, None)
                if r is None:
                    continue
                self.run_ids.discard(k)
                if delete_files:
                    n_del += shell.delete_local(r["local"])
            self.order = [k for k in self.order if k in self.rows]
            self.struct += 1
            self.rev += 1
        if self.history:
            self.history.touch()
        self.save_history(force=True)
        return {"ok": True, "deleted": n_del}

    def clear(self, mode):
        """清除历史：done=只清除已完成的；all=清除全部（正在下载/等待中的保留）"""
        with self.lock:
            keep = ("下载中", "等待中") if mode == "all" else ("下载中", "等待中", "失败", "已停止", "已中断", "已取消")
            ids = [k for k in self.order if self.rows[k]["state"] not in keep]
        if not ids:
            return {"info": "没有可以清除的记录。"}
        r = self.remove(ids)
        r["removed"] = len(ids)
        return r

    # ---------------- 给网页的数据 ----------------
    def snapshot(self, struct, since):
        with self.lock:
            full = struct != self.struct
            ks = self.order if full else [k for k in self.order if self.rows[k]["rev"] > since]
            out = []
            for k in ks:
                r = self.rows[k]
                cost = (r["t1"] - r["t0"]) if (r.get("t0") and r.get("t1")) else 0
                avg = (r["size"] or 0) / cost if (r["state"] == "完成" and cost > 0) else None
                out.append([k, r.get("label") or r["hf"], r["size"], r["done"], r["state"], r["bar"], r["speed"], avg,
                            r.get("err") or "", r["repo"], r["local"], r.get("t1") or r.get("added")])
            return {"struct": self.struct, "rev": self.rev, "full": full, "rows": out, "running": self.running,
                    "total": self._totals(), "source": self.source if self.running or self.t_end else ""}

    def _totals(self):
        rows = [self.rows[k] for k in self.run_ids if k in self.rows]
        now = self.t_end or time.time()
        total = sum(r["size"] or 0 for r in rows)
        done = sum(min(r["done"], r["size"]) if r["size"] else r["done"] for r in rows)
        transferred = sum(r["done"] for r in rows if r["bar"])
        states = [r["state"] for r in rows]
        elapsed = (now - self.t_start) if self.t_start else 0
        speed = self._speed if self.running else None
        eta = (total - done) / speed if (speed and elapsed >= SPEED_WINDOW and transferred > 0) else None
        return {"n_all": len(states), "n_ok": states.count("完成") + states.count("已是最新"),
                "n_active": states.count("下载中"), "n_fail": states.count("失败"), "total": total, "done": done,
                "speed": speed, "avg": transferred / elapsed if elapsed > 0 else 0, "elapsed": elapsed, "eta": eta,
                "started": self.t_start is not None and bool(rows),
                "final": not self.running and self.t_start is not None, "history": len(self.rows)}


def make_params(cfg, body):
    """把网页提交的参数整理成子进程需要的格式"""
    try:
        workers = max(1, min(16, int(body.get("workers") or DEFAULT_WORKERS)))
    except Exception:
        workers = DEFAULT_WORKERS
    try:
        threads = max(1, min(MAX_THREADS, int(body.get("threads") or DEFAULT_THREADS)))
    except Exception:
        threads = DEFAULT_THREADS
    return {
        "repo": (body.get("repo") or "").strip(),
        "path": (body.get("path") or "").strip() or os.getcwd(),
        "pattern": (body.get("pattern") or "").strip(),
        "token": (cfg.get("token") or "").strip(),
        "use_mirror": bool(body.get("use_mirror", True)),
        "no_symlinks": bool(body.get("no_symlinks", True)),
        "workers": workers,
        "file_threads": threads,
        "high_perf": bool(body.get("high_perf", False)),
    }
