# -*- coding: utf-8 -*-
"""
常规下载的历史记录：保存在程序目录的 hf_download_history.json，关闭程序后再打开仍然在。

  - 每个文件一条记录，用“本地保存路径”区分（同一个文件再次下载会更新原记录，不会重复）
  - 不保存 Token（重新下载/继续下载时使用“常规”页当前填写的 Token）
  - 最多保留 MAX_ROWS 条，超出时删除最早的已结束记录
"""
import os
import json
import time
import threading

MAX_ROWS = 5000
KEEP = ("id", "repo", "repo_type", "revision", "hf", "size", "label", "local", "local_dir", "direct", "use_mirror",
        "use_token", "source", "state", "done", "bar", "t0", "t1", "err", "added")
FINAL = ("完成", "已是最新", "失败", "已停止", "已中断", "已取消")


class History:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.dirty = False
        self.last_save = 0.0

    def load(self):
        """读取历史；上次没结束的文件（下载中/等待中）标记为“已中断”"""
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return []
        except Exception:
            try:        # 文件损坏时改名保存，避免覆盖掉
                os.replace(self.path, self.path + f".bad-{int(time.time())}")
            except Exception:
                pass
            return []
        rows = []
        for r in data.get("rows", []) if isinstance(data, dict) else []:
            if not isinstance(r, dict) or not r.get("id") or not r.get("hf"):
                continue
            if r.get("state") not in FINAL:
                r["state"] = "已中断"
            rows.append(r)
        return rows

    def save(self, rows, force=False):
        """rows：按显示顺序的记录列表。force=False 时最多每 3 秒写一次"""
        now = time.time()
        with self.lock:
            if not force and (not self.dirty or now - self.last_save < 3):
                return
            self.dirty = False
            self.last_save = now
            out = [{k: r.get(k) for k in KEEP} for r in rows]
            if len(out) > MAX_ROWS:
                ended = [i for i, r in enumerate(out) if r["state"] in FINAL]
                drop = set(ended[:len(out) - MAX_ROWS])
                out = [r for i, r in enumerate(out) if i not in drop]
            try:
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({"version": 1, "rows": out}, f, ensure_ascii=False)
                os.replace(tmp, self.path)
            except Exception:
                self.dirty = True

    def touch(self):
        with self.lock:
            self.dirty = True
