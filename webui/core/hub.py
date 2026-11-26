# -*- coding: utf-8 -*-
"""日志与提示中心。

后台线程往里写，网页每 0.5 秒轮询一次，按序号只取新的内容。
提示（notice）只做“通知”，网页会显示成右下角的小卡片，不打断操作。
"""
import threading
import time

LOG_KEEP = 4000         # 每个页面最多保留的日志条数
NOTICE_KEEP = 50        # 最多保留的提示条数


class LogHub:
    def __init__(self):
        self.lock = threading.Lock()
        self.seq = 0
        self.logs = {"basic": [], "batch": []}
        self.notices = []

    # ---------------- 日志 ----------------
    def log_raw(self, channel, text):
        if not text:
            return
        with self.lock:
            self.seq += 1
            buf = self.logs.setdefault(channel, [])
            buf.append((self.seq, text))
            if len(buf) > LOG_KEEP:
                del buf[:len(buf) - LOG_KEEP]

    def log(self, channel, text):
        self.log_raw(channel, str(text) + "\n")

    # ---------------- 提示 ----------------
    def notice(self, channel, level, title, msg):
        """level: ok / info / warn / error。网页显示成不打断操作的小卡片。"""
        with self.lock:
            self.seq += 1
            self.notices.append((self.seq, channel, level, title, msg, time.time()))
            del self.notices[:-NOTICE_KEEP]

    # ---------------- 给网页 ----------------
    def since(self, seq):
        with self.lock:
            out = {ch: "".join(t for s, t in buf if s > seq) for ch, buf in self.logs.items()}
            notes = [list(n) for n in self.notices if n[0] > seq]
            return self.seq, out, notes
