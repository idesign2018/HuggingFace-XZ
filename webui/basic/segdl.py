# -*- coding: utf-8 -*-
"""
多线程分段下载（“常规”页的大文件用它，由 basic/worker.py 调用，在下载子进程中运行）

为什么需要它：huggingface_hub 下载一个文件只用 1 个连接。这个连接一旦卡住（镜像站 / CDN 不再发数据），
整个文件就停在原地，速度 0 B/s。这里把大文件切成很多小块，同时开多个连接下载：
  - 每个连接 20 秒收不到数据就自动断开重连，从断点继续，不影响其他连接
  - 遇到 429（限流）/ 5xx 自动等一会儿再试；下载地址过期（CDN 签名约 1 小时）自动重新获取
  - 已完成的分块记在 “文件名.hfdpart.json” 里，停止后“继续下载”只补没下完的部分
  - 服务器不支持分段（没有 Range）时抛出 Unsupported，由调用方改用 huggingface_hub 下载

开始下载不用等：
  - 临时文件 .hfdpart 建成“稀疏文件”：大小一步定好，但不往硬盘写 0（以前先把整个文件写满 0，
    20 GB 的文件要白等 100 秒，还多写一遍硬盘）。没写到的地方不占硬盘空间。
  - 硬盘不支持稀疏文件（exFAT / FAT32 / 部分网络盘）时改成“边下边长”：文件随下载慢慢变大，速度一样。
  - 开始前检查剩余空间，不够就直接提示，不会下到一半才报错。
  - 准备阶段（探测）也能随时停止，不用等它结束。

Token 和代理：
  - 先按 use_token 决定带不带 Token（国内镜像一律不带；官方源只有已知是受限 / 私有仓库才带），
    服务器回 401 / 403 再带上 Token 重试一次；Token 只发给仓库站点，跳转到下载服务器后不再发送
  - 官方源整条跳转链走代理，国内镜像整条直连（core/net.py）
"""
import os
import json
import time
import shutil
import ctypes
import threading
import http.client
import urllib.error
import urllib.request
from collections import deque
from urllib.parse import urljoin, urlparse

from core import net
from core.common import make_headers, NoRedirect, human_size

SEG_MIN_SIZE = 32 * 1024 * 1024     # 小于这个大小的文件仍用 huggingface_hub（单连接已经够快）
MAX_THREADS = 16                    # 每个文件最多同时开的连接数（和 aria2 官方版上限一致）
CHUNK_MIN, CHUNK_MAX = 4 * 1024 * 1024, 16 * 1024 * 1024
BLOCK = 256 * 1024                  # 每次读取的大小（也是“停止”检查的间隔）
READ_TIMEOUT = 20                   # 一个连接这么多秒收不到数据，就断开重连
PROBE_TIMEOUT = 10                  # 开始前那一次探测：这么多秒没有回应就算这次失败（会再试几次）
MAX_TRIES = 12                      # 同一个分块连续失败这么多次，才算下载失败
RATE_LIMIT_TRIES = 40               # 429（连接受限）不算网络故障：多等一会儿、多试几次，别把文件判失败
PROBE_TRIES = 4                     # 开始前“探一下支不支持分段”最多试几次
SPACE_MARGIN = 256 * 1024 * 1024    # 检查剩余空间时多留的余量
PART, STATE = ".hfdpart", ".hfdpart.json"
REDIRECTS = (301, 302, 303, 307, 308)
EXPIRED = (401, 403, 404, 410)      # 下载中途出现这些：多半是 CDN 地址过期，重新获取一次
AUTH = (401, 403)                   # 服务器要求登录：带上 Token 重试
FSCTL_SET_SPARSE = 0x000900C4
FILE_END_OF_FILE_INFO = 6           # SetFileInformationByHandle 的 FileEndOfFileInfo


class Unsupported(Exception):
    """服务器不支持分段下载 → 改用单连接下载"""


class Cancelled(Exception):
    """用户停止了这个文件（准备阶段也能马上停）"""


# ============================================================
# 临时文件：稀疏文件 / 边下边长 / 剩余空间
# ============================================================
_K32 = None


def _k32():
    global _K32
    if _K32 is None:
        from ctypes import wintypes as w
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.DeviceIoControl.argtypes = [w.HANDLE, w.DWORD, ctypes.c_void_p, w.DWORD, ctypes.c_void_p, w.DWORD,
                                      ctypes.POINTER(w.DWORD), ctypes.c_void_p]
        k.DeviceIoControl.restype = w.BOOL
        k.SetFileInformationByHandle.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        k.SetFileInformationByHandle.restype = w.BOOL
        k.GetCompressedFileSizeW.argtypes = [w.LPCWSTR, ctypes.POINTER(w.DWORD)]
        k.GetCompressedFileSizeW.restype = w.DWORD
        _K32 = k
    return _K32


def _set_sparse(fileobj, on=True):
    import msvcrt
    from ctypes import wintypes as w
    h = msvcrt.get_osfhandle(fileobj.fileno())
    flag, ret = ctypes.c_ubyte(1 if on else 0), w.DWORD(0)
    return bool(_k32().DeviceIoControl(h, FSCTL_SET_SPARSE, ctypes.byref(flag), 1, None, 0, ctypes.byref(ret), None))


def _set_size(fileobj, total):
    """直接改文件大小（不像 Python 的 truncate 那样在 Windows 上把扩出来的部分写满 0）"""
    import msvcrt
    h = msvcrt.get_osfhandle(fileobj.fileno())
    eof = ctypes.c_longlong(total)
    return bool(_k32().SetFileInformationByHandle(h, FILE_END_OF_FILE_INFO, ctypes.byref(eof), ctypes.sizeof(eof)))


def _create_part(part, total):
    """建临时文件：能建稀疏文件就一步定好大小（不写 0，瞬间完成）；不能就留空，边下边长。
    返回 "sparse" / "grow" """
    with open(part, "wb") as f:
        if os.name != "nt":
            f.truncate(total)               # Linux / macOS 的 truncate 本来就是稀疏的
            return "sparse"
        try:
            if _set_sparse(f) and _set_size(f, total):
                return "sparse"
        except Exception:
            pass
    return "grow"


def _finish_part(part):
    """下载完成：去掉稀疏标记（此时每个字节都写过了，去掉后就是普通文件）。失败不影响使用"""
    if os.name != "nt":
        return
    try:
        with open(part, "r+b") as f:
            _set_sparse(f, False)
    except Exception:
        pass


def _allocated(path):
    """文件实际占用的硬盘空间（稀疏文件没写到的地方不占空间）"""
    try:
        if os.name == "nt":
            high = ctypes.c_ulong(0)
            ctypes.set_last_error(0)
            low = _k32().GetCompressedFileSizeW(path, ctypes.byref(high))
            if low == 0xFFFFFFFF and ctypes.get_last_error():
                raise OSError("GetCompressedFileSizeW")
            return (high.value << 32) + low
        return os.stat(path).st_blocks * 512
    except Exception:
        try:
            return os.path.getsize(path)
        except OSError:
            return 0


def _check_space(part, total, label, fresh):
    """剩余空间不够就直接报错（不要下到一半才失败）。已经写进临时文件的部分不用再算"""
    folder = os.path.dirname(os.path.abspath(part))
    try:
        free = shutil.disk_usage(folder).free
    except OSError:
        return                                  # 查不到（比如某些网络盘）：不拦
    used = _allocated(part) if os.path.isfile(part) else 0
    if fresh:                                   # 要重建临时文件：旧的会被清空，它占的空间也会还回来
        free, need = free + used, total
    else:
        need = max(0, total - used)
    if free < need + SPACE_MARGIN:
        drive = os.path.splitdrive(folder)[0] or folder
        raise RuntimeError(f"硬盘空间不够：{label} 还需要约 {human_size(need)}，{drive} 只剩 {human_size(free)}。"
                           f"请清理空间或换一个保存位置。")


def _disk_full(e):
    return getattr(e, "winerror", None) in (39, 112) or getattr(e, "errno", None) == 28


# ============================================================
# 下载地址
# ============================================================
class _Source:
    """下载地址：resolve 地址 → 跟随跳转得到真正的文件地址（只对仓库站点发送 Token）"""

    def __init__(self, url, token, use_token=False, log=None):
        self.url, self.token = url, token or None
        self.send_token = bool(self.token and use_token)
        self.log = log or (lambda s: None)
        self.hub = {urlparse(url).netloc.lower(), "huggingface.co", "hf.co"}
        self.lock = threading.Lock()
        self.final, self.gen = None, 0
        self.opener = net.opener_for(url, NoRedirect)       # 官方源走代理，国内镜像直连

    def open(self, url, rng, timeout=READ_TIMEOUT):
        cur = url
        for _ in range(8):
            tok = self.token if (self.send_token and urlparse(cur).netloc.lower() in self.hub) else None
            h = make_headers(tok)
            h.update({"Range": rng, "Accept-Encoding": "identity"})
            try:
                return self.opener.open(urllib.request.Request(cur, headers=h), timeout=timeout), cur
            except urllib.error.HTTPError as e:
                loc = e.headers.get("Location") if e.code in REDIRECTS else None
                if not loc:
                    raise
                e.close()
                cur = urljoin(cur, loc)
        raise RuntimeError("跳转次数过多")

    def _open_auth(self, url, rng, timeout):
        """先按现在的方式请求；服务器回 401 / 403、还没带过 Token、手上又有 Token 时，带上 Token 再试一次"""
        try:
            return self.open(url, rng, timeout)
        except urllib.error.HTTPError as e:
            code = e.code
            if code not in AUTH or self.send_token or not self.token:
                raise
            e.close()
        with self.lock:
            self.send_token = True
        self.log(f"服务器要求登录（HTTP {code}），已带上 HF Token 重试"
                 + ("（Token 会经过 hf-mirror.com，由镜像站转交官方验证）" if net.is_mirror(self.url) else ""))
        return self.open(url, rng, timeout)

    def _probe_once(self):
        resp, final = self._open_auth(self.url, "bytes=0-0", PROBE_TIMEOUT)
        with resp:
            if resp.status != 206:
                raise Unsupported("服务器不支持分段下载")
            try:
                total = int((resp.headers.get("Content-Range") or "").rsplit("/", 1)[1])
            except Exception:
                raise Unsupported("服务器没有返回文件大小")
            resp.read()
        with self.lock:
            self.final = final
            self.gen += 1
        return total

    def probe(self, should_stop=None):
        """确认支持分段下载，返回文件总大小（连不上 / 超时就等一会儿重试；用户停止时马上退出）"""
        stopped = should_stop or (lambda: False)
        err = None
        for i in range(PROBE_TRIES):
            if stopped():
                raise Cancelled()
            wait = min(8.0, 0.6 * 2 ** i)   # 网络打嗝：0.6 / 1.2 / 2.4 秒后再试
            try:
                return self._probe_once()
            except urllib.error.HTTPError as e:
                e.close()
                if e.code != 429 and e.code < 500:
                    raise                   # 404 / 401 这类不用重试，直接让调用方看到原因
                err = e                     # 429 被限流 / 5xx 服务器打嗝：等一会儿再试
                if e.code == 429:
                    wait = min(8.0, 3.0 * (i + 1))      # 被限流时要等得久一点
            except (OSError, http.client.HTTPException) as e:
                err = e
            if i == PROBE_TRIES - 1:
                break
            end = time.time() + wait
            while time.time() < end:        # 等待期间也能马上停
                if stopped():
                    raise Cancelled()
                time.sleep(0.1)
        raise err

    def current(self):
        with self.lock:
            return self.final or self.url, self.gen

    def refresh(self, gen):
        """地址过期：重新获取（几个连接同时发现时只获取一次）"""
        with self.lock:
            if gen != self.gen:
                return
        self.probe()


def _load_state(state_path, part, total):
    """读取上次的进度：返回 (分块大小, 已完成的分块, {分块序号: 这块已下的字节})；
    对不上时返回 (None, 空, 空)，从头开始。
    “边下边长”的临时文件比总大小小：超出文件末尾的进度不算（那部分没真正写进去）"""
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            st = json.load(f)
        size = os.path.getsize(part) if os.path.isfile(part) else -1
        if st.get("size") != total or not 0 <= size <= total:
            raise ValueError
        chunk = int(st["chunk"])
        if chunk <= 0:
            raise ValueError
        clen = lambda i: min(total, (i + 1) * chunk) - i * chunk      # noqa: E731
        done = set()
        for x in st.get("done") or []:
            i = int(x)
            if i * chunk + clen(i) <= size:
                done.add(i)
        filled = {}
        for x, v in (st.get("filled") or {}).items():
            i = int(x)
            if i in done:
                continue
            v = min(int(v), clen(i), max(0, size - i * chunk))
            if v > 0:
                filled[i] = v
        return chunk, done, filled
    except Exception:
        pass
    return None, set(), {}


def prepare(url, token, use_token=False, log=None, should_stop=None):
    """开始前探一次：一次请求同时拿到文件总大小、真正的下载地址、以及服务器支不支持分段下载。
    返回 (源, 总大小)；交给 download(prepared=...) 复用，同一件事不用做两遍。"""
    src = _Source(url, token, use_token, log)
    if should_stop is None:
        return src, src.probe()
    # 探测放到后台线程：用户点停止时马上返回，不用等正在进行的那次连接超时（它在后台自己结束）
    box = {}

    def run():
        try:
            box["v"] = src.probe(should_stop)
        except BaseException as e:
            box["e"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    while t.is_alive():
        if should_stop():
            raise Cancelled()
        t.join(0.1)
    if "e" in box:
        raise box["e"]
    return src, box["v"]


def download(url, token, dest, threads, bar_factory, force=False, log=None, label="", prepared=None,
             use_token=False, should_stop=None):
    """
    下载到 dest。返回 "done"（下载完成）或 "skip"（本地已有同样大小的文件）。
    bar_factory(total=, initial=) 返回进度接收器，它的 update(n) 在用户停止时会抛出异常。
    prepared：已经 prepare() 过的 (源, 总大小)，有就直接用，不再探一次。
    should_stop()：返回 True 时尽快停下（抛出 Cancelled）。
    """
    log = log or (lambda s: None)
    stopped = should_stop or (lambda: False)
    if prepared is None:
        src, total = prepare(url, token, use_token, log, should_stop)
    else:
        src, total = prepared
    if stopped():
        raise Cancelled()
    if not force and os.path.isfile(dest) and os.path.getsize(dest) == total:
        return "skip"
    part, state_path = dest + PART, dest + STATE
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    chunk, done, filled = (None, set(), {}) if force else _load_state(state_path, part, total)
    if not chunk:
        chunk = max(CHUNK_MIN, min(CHUNK_MAX, total // (max(1, threads) * 4) or CHUNK_MIN))
        done, filled = set(), {}
        _check_space(part, total, label, fresh=True)
        if _create_part(part, total) == "grow":
            log(f"提示：{label} 所在的硬盘不支持稀疏文件，改为边下边长（速度不受影响）")
    else:
        _check_space(part, total, label, fresh=False)
    n = (total + chunk - 1) // chunk
    clen = lambda i: min(total, (i + 1) * chunk) - i * chunk      # noqa: E731
    have = sum(clen(i) for i in done) + sum(min(v, clen(i)) for i, v in filled.items() if i not in done)
    bar = bar_factory(total=total, initial=have)
    todo = deque(i for i in range(n) if i not in done)
    if done or filled:
        log(f"↻ 续传：{label}（已完成 {len(done)}/{n} 块，接着上次的进度继续）")

    lock, abort, errors = threading.Lock(), threading.Event(), []
    info = {"saved": 0.0, "retries": 0, "logged": 0.0}

    def save(now=False):
        """把进度写进状态文件（调用时必须已持有 lock）。每秒最多写一次"""
        t = time.time()
        if not now and t - info["saved"] < 1.0:
            return
        info["saved"] = t
        tmp = state_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"size": total, "chunk": chunk, "done": sorted(done), "filled": filled}, f)
            os.replace(tmp, state_path)
        except OSError:
            pass                                # 磁盘忙 / 被杀毒软件占用：下一次再写

    def retried(err):
        with lock:
            info["retries"] += 1
            if time.time() - info["logged"] < 30:
                return
            info["logged"] = time.time()
            n_retry = info["retries"]
        log(f"⟳ {label}：有连接中断或超时，已自动重连（累计 {n_retry} 次）：{err}")

    def fetch(i, f):
        base = i * chunk
        pos, end, tries = base + min(filled.get(i, 0), clen(i)), base + clen(i) - 1, 0
        while pos <= end:
            url_now, gen = src.current()
            limit, wait_max = MAX_TRIES, 30.0
            try:
                resp, _ = src.open(url_now, f"bytes={pos}-{end}")
                with resp:
                    if resp.status != 206:
                        raise Unsupported("服务器中途不再支持分段下载")
                    if not (resp.headers.get("Content-Range") or "").startswith(f"bytes {pos}-"):
                        raise OSError("服务器返回的分段位置不对")
                    while pos <= end:
                        if abort.is_set():
                            return False
                        buf = resp.read(min(BLOCK, end - pos + 1))
                        if not buf:
                            raise OSError("连接提前结束")
                        f.seek(pos)
                        f.write(buf)
                        pos += len(buf)
                        tries = 0
                        f.flush()                  # 先落到磁盘，再把“已下到这里”写进状态文件
                        with lock:
                            filled[i] = pos - base
                            save()
                        bar.update(len(buf))       # 用户停止时这里会抛出异常
                return True
            except urllib.error.HTTPError as e:
                code = e.code
                e.close()
                if code in EXPIRED:
                    try:
                        src.refresh(gen)
                    except Exception:
                        pass
                elif code != 429 and code < 500:
                    raise
                if code == 429:                 # 被限流（连接开得多时常见）：等久一点、多试几次
                    limit, wait_max = RATE_LIMIT_TRIES, 60.0
                err = f"HTTP {code}"
            except (OSError, http.client.HTTPException) as e:
                if _disk_full(e):
                    raise RuntimeError("硬盘空间已满，下载已暂停。清理空间后右键“继续下载”即可续传。")
                err = str(e) or type(e).__name__
            tries += 1
            if tries > limit:
                raise RuntimeError(f"网络多次中断，同一分块已重试 {limit} 次仍失败（{err}）")
            retried(err)
            if abort.wait(min(wait_max, 1.5 ** tries)):
                return False
        return True

    def worker():
        try:
            with open(part, "r+b") as f:
                while not abort.is_set():
                    with lock:
                        if not todo:
                            return
                        i = todo.popleft()
                    if not fetch(i, f):
                        return
                    f.flush()                       # 先把这一块写到磁盘，再记“已完成”（中途被结束也不会记错）
                    with lock:
                        done.add(i)
                        filled.pop(i, None)
                        save()
        except BaseException as e:
            with lock:
                errors.append(e)
            abort.set()

    ts = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, min(threads, len(todo))))]
    for t in ts:
        t.start()
    while any(t.is_alive() for t in ts):
        if abort.is_set():                  # 出错或被停止：最多再等 1.5 秒（卡住的连接不必等它超时）
            deadline = time.time() + 1.5
            for t in ts:
                t.join(max(0.0, deadline - time.time()))
            break
        time.sleep(0.2)
    with lock:
        save(now=True)
    if errors:
        raise errors[0]
    if len(done) != n:
        raise RuntimeError("下载没有完成（部分分块缺失），请重试")
    if os.path.getsize(part) != total:      # 边下边长：最后一块写完时大小就对了；万一不对，别当成完成
        raise RuntimeError("临时文件大小和服务器上的不一致，请右键“重新下载”")
    _finish_part(part)
    os.replace(part, dest)
    try:
        os.remove(state_path)
    except OSError:
        pass
    return "done"
