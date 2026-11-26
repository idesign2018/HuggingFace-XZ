# -*- coding: utf-8 -*-
"""接口：常规下载，以及“常规”页列表的右键菜单（打开、打开方式、打开文件夹、继续/重新下载、停止、移除、属性）。

由 core.server.App 继承使用。
"""
import os

from core.common import size_both
from basic.manager import make_params
from shellops import files as shell


class BasicApiMixin:
    # ---------------- 常规下载 ----------------
    def _engine_params(self, use_mirror=None):
        bs = self.basic_settings()
        return make_params(self.cfg, {"repo": "", "path": bs["path"], "workers": bs["workers"],
                                      "threads": bs["threads"],
                                      "use_mirror": bs["use_mirror"] if use_mirror is None else use_mirror,
                                      "no_symlinks": bs["no_symlinks"], "high_perf": bs["high_perf"]})

    def api_basic_save(self, b):
        self.set_basic_settings(b)
        self.save_config()
        return {"ok": True}

    def api_basic_start(self, b):
        self.set_basic_settings(b)
        self.save_config()
        p = make_params(self.cfg, dict(b, use_mirror=self.use_mirror()))     # 下载源：左侧导航栏“源”按钮
        if not p["repo"]:
            return {"error": "请输入仓库ID！"}
        return self.basic.start(p)

    def api_basic_stop(self, b):
        return self.basic.stop(force=bool(b.get("force")))

    # ---------------- 右键菜单：常规页列表 ----------------
    def _jobs_from_rows(self, rows, force):
        token = (self.cfg.get("token") or "").strip()
        # 批量页来的文件知道仓库是否受限（缺 Token 时提示）；常规页来的文件只是“当时填了 Token”，没有 Token 也尝试下载
        need = [r for r in rows if r.get("use_token") and str(r.get("source") or "").startswith("批量")]
        if need and not token:
            return None, {"error": "其中有受限/私有仓库的文件，需要 HF Token，请先在左侧填写。"}
        # Token 都带上（只在需要时才真正发送，见 worker / segdl）；下载源用左侧“源”按钮现在的设置
        return [dict(r, token=token, use_mirror=self.use_mirror(), force=force) for r in rows], None

    def api_basic_action(self, b):
        act, ids = b.get("action"), b.get("ids") or []
        rows = self.basic.get_rows(ids)
        if not rows:
            return {"info": "请先在列表中选中文件（单击选中，Ctrl/Shift 多选）。"}
        first = rows[0]["local"]
        if act == "open":
            return shell.open_file(first)
        if act == "open_with":
            return shell.open_with(first)
        if act == "folder":
            return shell.open_folder(first)
        if act in ("stop", "kill"):
            return self.basic.stop_files(ids, immediate=(act == "kill"))
        if act in ("continue", "redownload"):
            force = act == "redownload"
            busy = [r for r in rows if r["state"] in ("下载中", "等待中")]
            rows = [r for r in rows if r["state"] not in ("下载中", "等待中")]
            if not rows:
                return {"info": "选中的文件已经在下载或等待中。"}
            if not force:                           # 已下载完成的不用再下一遍（要重下请用“重新下载”）
                done = [r for r in rows if r["state"] in ("完成", "已是最新")]
                rows = [r for r in rows if r["state"] not in ("完成", "已是最新")]
                if not rows:
                    return {"info": f"选中的 {len(done)} 个文件都已经下载完成（要重新下载请右键 →“重新下载”）。"}
            if force and "redownload" not in (b.get("confirmed") or []):
                return {"confirm": "redownload", "title": "重新下载",
                        "message": f"将重新下载 {len(rows)} 个文件（忽略本地已有的文件，从头下载，下载完成后覆盖）。\n是否继续？"}
            jobs, reply = self._jobs_from_rows(rows, force)
            if jobs is None:
                return reply
            r = self.basic.enqueue(jobs, self._engine_params(), "")
            if r.get("ok") and busy:
                r["info"] = f"另有 {len(busy)} 个文件已经在下载中，已跳过。"
            return r
        if act == "remove":
            del_files = bool(b.get("delete_files"))
            if "remove" not in (b.get("confirmed") or []):
                extra = "\n\n同时删除本地文件（包括未下载完的部分）！" if del_files else "\n\n只从列表中移除，不会删除已下载的文件。"
                return {"confirm": "remove", "title": "移除", "message": f"从列表中移除 {len(rows)} 条记录？{extra}"}
            r = self.basic.remove(ids, del_files)
            if r.get("ok") and del_files:
                r["info"] = f"已移除 {len(rows)} 条记录，删除了 {r.get('deleted', 0)} 个本地文件。"
            return r
        if act == "props":
            return {"props": [self._props(r) for r in rows[:1]]}
        return {"error": f"未知操作：{act}"}

    def api_basic_clear(self, b):
        mode = b.get("mode") or "done"
        if "clear" not in (b.get("confirmed") or []):
            what = "全部记录（正在下载/等待中的除外）" if mode == "all" else "已完成的记录"
            return {"confirm": "clear", "title": "清除历史", "message": f"清除{what}？\n只清除列表记录，不会删除已下载的文件。"}
        r = self.basic.clear(mode)
        if r.get("ok"):
            r["info"] = f"已清除 {r.get('removed', 0)} 条记录。"
        return r

    @staticmethod
    def _props(r):
        local = r["local"]
        exists = os.path.isfile(local)
        actual = os.path.getsize(local) if exists else None
        import time as _t
        fmt = lambda t: _t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime(t)) if t else "—"
        return [
            ["文件", r.get("label") or r["hf"]], ["仓库", f"{r['repo']}（{r['repo_type']} @ {r['revision']}）"],
            ["HF 路径", r["hf"]], ["保存位置", local],
            ["大小", size_both(r["size"]) + (f"（{r['size']:,} 字节）" if r["size"] else "")],
            ["本地文件", (f"已存在，{size_both(actual)}" + ("，大小一致" if actual == r["size"] else "，大小不一致"))
             if exists else "不存在"],
            ["状态", r["state"] + (f"：{r['err']}" if r.get("err") else "")],
            ["下载源", "国内镜像 hf-mirror.com" if r.get("use_mirror") else "官方 huggingface.co"],
            ["来源", r.get("source") or "—"], ["加入时间", fmt(r.get("added"))], ["结束时间", fmt(r.get("t1"))],
        ]
