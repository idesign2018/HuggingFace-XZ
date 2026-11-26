# -*- coding: utf-8 -*-
"""
批量页 → “常规”下载：把批量清单中的文件整理成下载任务，交给常规下载（huggingface_hub）。
由 batch/manager.py 的 BatchManager 继承使用。

  - 默认发送“全部仓库中已勾选的文件”；右键菜单可以只发送选中的行
  - 导入的普通链接（不是 Hugging Face 的文件）不能用 huggingface_hub 下载，会被跳过
  - 下载源跟随“批量”页的选择；同时下载数、高性能模式等沿用“常规”页的设置
  - 常规下载的进度会同步显示在批量清单的“状态”列
"""
import os

from core.common import size_both, file_matches


def _confirm(cid, title, message):
    return {"confirm": cid, "title": title, "message": message}


BASIC_STATE_TEXT = {"等待中": "常规下载：排队中", "下载中": "常规下载：下载中", "完成": "已完成（常规下载）",
                    "已是最新": "已完成（已是最新）", "失败": "失败（常规下载）", "已停止": "已停止（常规下载）",
                    "已中断": "未完成（已中断）"}


class BatchBasicMixin:
    def _selected_jobs(self, iids, force=False):
        """右键菜单：选中的行 → 任务（不看勾选；force=True 时不跳过已存在的文件）"""
        with self.lock:
            sel = [self.by_iid[i] for i in iids or [] if i in self.by_iid]
        if not sel:
            return None, {"info": "请先在列表中选中文件。"}
        jobs, n_skip = [], 0
        for it in sel:
            if not force and self.s["skip_existing"] and file_matches(it["local"], it["size"]):
                n_skip += 1
                continue
            m = self.repos[it["repo_key"]]
            jobs.append({"it": it, "repo": m["repo"], "needs_token": bool(m.get("needs_token")), "size": it["size"],
                         "local": it["local"]})
        if not jobs:
            return None, {"info": f"选中的 {n_skip} 个文件本地都已存在（大小一致），已跳过。\n需要重新下载请用右键“重新下载”。"}
        return jobs, None

    def to_basic_jobs(self, confirmed, iids=None, force=False):
        """
        返回 (参数, 回复)。参数 = {"jobs": [...], "use_mirror": bool, "source": 说明}；不能继续时参数为 None。
        iids=None：全部已勾选的文件；否则只处理这些行。
        """
        jobs, reply = self._collect_jobs(confirmed) if iids is None else self._selected_jobs(iids, force)
        if jobs is None:
            return None, reply
        hf_jobs = [j for j in jobs if self.repos[j["it"]["repo_key"]].get("kind") != "import"]
        n_import = len(jobs) - len(hf_jobs)
        if not hf_jobs:
            return None, {"info": "选中/勾选的都是导入的普通链接（不是 Hugging Face 的文件），常规下载无法处理。\n"
                                  "请用“推送到 IDM”或“用 aria2 下载”。"}
        token, reply = self._token_for_download(hf_jobs, confirmed)
        if token is None:
            return None, reply
        if "to_basic" not in confirmed and not self.s.get("skip_basic_confirm"):
            msg = (f"共 {len(hf_jobs)} 个文件，合计 {size_both(sum(j['size'] or 0 for j in hf_jobs))}\n"
                   f"{self._jobs_summary(hf_jobs)}\n\n"
                   f"将发送到“常规”下载并自动开始（使用 huggingface_hub，支持断点续传，每个文件都有进度）。\n"
                   f"下载源：{'国内镜像 hf-mirror.com' if self.s['source'] == 'mirror' else '官方 huggingface.co'}；"
                   f"同时下载文件数、hf_xet 高性能模式沿用“常规”页的设置。")
            if force:
                msg += "\n\n“重新下载”会忽略本地已有的文件，从头下载。"
            if n_import:
                msg += f"\n\n另有 {n_import} 个导入的普通链接不是 Hugging Face 的文件，会被跳过（请用 IDM / aria2）。"
            r = _confirm("to_basic", "用常规下载", msg + "\n\n是否开始？")
            r["dont_ask"] = "skip_basic_confirm"          # 网页显示“不再提示”复选框
            return None, r

        out = []
        use_mirror = self.s["source"] == "mirror"
        all_token = (self.get_token() or "").strip()   # 都带上：只在需要时才真正发送（见 basic/worker.py）
        multi = len(set(j["repo"] for j in hf_jobs)) > 1
        with self.lock:
            for j in hf_jobs:
                it = j["it"]
                m = self.repos[it["repo_key"]]
                local = os.path.normpath(it["local"])
                # 默认路径 = 下载目录 + HF 原路径（文件名没被改过）：可以直接下载到这个位置
                base = self.download_base(m)
                direct_path = os.path.normpath(os.path.join(base, *it["hf"].split("/")))
                direct = (not it["custom"]) and (not it["renamed"]) and os.path.normcase(local) == os.path.normcase(direct_path)
                out.append({
                    "id": os.path.normcase(local), "iid": it["iid"], "repo": m["repo"], "repo_type": m["repo_type"],
                    "revision": m["revision"], "hf": it["hf"], "size": it["size"], "local": local, "local_dir": base,
                    "direct": direct, "use_mirror": use_mirror, "use_token": bool(m.get("needs_token")),
                    "token": all_token, "force": force,
                    "label": f"{m['repo']}/{it['hf']}" if multi else it["hf"], "source": f"批量：{m['repo']}",
                })
                it["state"] = BASIC_STATE_TEXT["等待中"]
                self._touch(it)
        n_repo = len(set(j["repo"] for j in out))
        source = f"来自批量下载：{len(out)} 个文件，{n_repo} 个仓库"
        return {"jobs": out, "use_mirror": use_mirror, "source": source}, None

    def on_basic_states(self, states):
        """常规下载的文件状态变化 → 更新批量清单中对应文件（按本地保存路径对应）"""
        with self.lock:
            if not states:
                return
            touched = []
            for it in self.items:
                st = states.get(os.path.normcase(os.path.normpath(it["local"])))
                if st is None:
                    continue
                text = BASIC_STATE_TEXT.get(st)
                if text and it["state"] != text:
                    it["state"] = text
                    if st in ("完成", "已是最新"):
                        it["exists"] = True
                        touched.append(it)
                    self._touch(it)
            if touched:
                self._refresh_flags()
                self._render_all()
