# -*- coding: utf-8 -*-
"""接口：批量下载（设置 / 仓库 / 勾选 / 筛选 / 路径 / 导入导出 / 推送下载 / aria2）。

由 core.server.App 继承使用。
"""
import os

from core.common import size_both, file_matches
from batch.aria2 import preview_opts, describe_proxy
from shellops import files as shell


class BatchApiMixin:
    # ---------------- 批量 → 常规 ----------------
    def api_batch_to_basic(self, b):
        """批量页勾选（或右键选中）的文件 → 常规下载（自动开始；正在下载时加入当前任务）"""
        self.batch.update_settings(b)
        if b.get("dont_ask"):                          # 确认框里勾了“不再提示”
            self.batch.update_settings({"settings": {"skip_basic_confirm": True}})
        params, reply = self.batch.to_basic_jobs(b.get("confirmed") or [], b.get("iids"), bool(b.get("force")))
        if params is None:
            return reply
        self.save_config()
        r = self.basic.enqueue(params["jobs"], self._engine_params(params["use_mirror"]), params["source"])
        if r.get("ok"):
            joined = "（已加入正在进行的下载任务）" if r.get("joined") else ""
            self.batch.log(f"已发送到“常规”下载：{params['source']}{joined}。进度请在“常规”页查看。")
            r["switch"] = "basic"
        return r

    # ---------------- 设置 / 仓库 ----------------
    def api_batch_settings(self, b):
        r = self.batch.update_settings(b)
        if b.get("save"):
            self.save_config()
        return r

    def api_batch_add(self, b):
        return self.batch.add_repos(b)

    def api_batch_refresh(self, b):
        return self.batch.refresh_repos(b.get("keys"), b.get("confirmed") or [])

    def api_batch_remove(self, b):
        return self.batch.remove_repos(b.get("keys") or [], b.get("confirmed") or [])

    def api_batch_check(self, b):
        return self.batch.set_checked(b.get("op"), b.get("iids"), b.get("view"))

    def api_batch_preview(self, b):
        return self.batch.filter_preview(b.get("filters"), b.get("view"))

    def api_batch_filter(self, b):
        return self.batch.apply_filter(b.get("mode"), b.get("filters"), b.get("view"), live=bool(b.get("live")))

    # ---------------- 路径 ----------------
    def api_batch_path_info(self, b):
        return self.batch.path_info(b.get("iid"), b.get("text"))

    def api_batch_set_path(self, b):
        return self.batch.set_path(b.get("iid"), b.get("text"))

    def api_batch_change_dir(self, b):
        return self.batch.change_dir(b.get("iids"), b.get("dir"), bool(b.get("keep")))

    def api_batch_reset_paths(self, b):
        return self.batch.reset_paths(b.get("iids"))

    def api_batch_new_folder(self, b):
        r = self.batch.new_folder(b.get("parent"), b.get("name"))
        self.save_config()
        return r

    def api_batch_open_folder(self, b):
        return self.batch.open_folder(b.get("view"))

    def api_batch_export_name(self, b):
        return {"name": self.batch.export_name()}

    # ---------------- 导入导出 / 下载 ----------------
    def api_batch_export_csv(self, b):
        return self.batch.export_csv(b.get("path"))

    def api_batch_import_csv(self, b):
        return self.batch.import_csv(b.get("path"))

    def api_batch_push_idm(self, b):
        return self.batch.push_idm(b.get("confirmed") or [])

    def api_batch_aria2(self, b):
        return self.batch.run_aria2(b.get("confirmed") or [])

    def api_batch_export_aria2(self, b):
        return self.batch.export_aria2(b.get("path"), b.get("confirmed") or [])

    def api_batch_export_urls(self, b):
        return self.batch.export_urls(b.get("path"), b.get("confirmed") or [])

    def api_batch_import_tasks(self, b):
        return self.batch.import_tasks(b.get("paths") or [], b.get("confirmed") or [])

    def api_batch_stop(self, b):
        return self.batch.stop(b.get("confirmed") or [])

    # ---------------- aria2 高级设置 ----------------
    def api_aria2_preview(self, b):
        errs, cli, proxy = preview_opts(b.get("opts"))
        return {"errors": errs, "cli": cli, "proxy": proxy}

    def api_aria2_save(self, b):
        return self.batch.set_aria2_opts(b.get("opts"), b.get("concurrency"))

    def api_detect_proxy(self, b):
        text = describe_proxy({"proxy_mode": "system"})
        self.hub.log("batch", text)
        return {"text": text}

    # ---------------- 右键菜单：批量页列表 ----------------
    def api_batch_action(self, b):
        act, iids = b.get("action"), b.get("iids") or []
        with self.batch.lock:
            its = [self.batch.by_iid[i] for i in iids if i in self.batch.by_iid]
            infos = [(it, self.batch.repos[it["repo_key"]]) for it in its]
        if not infos:
            return {"info": "请先在列表中选中文件。"}
        it, m = infos[0]
        if act == "open":
            return shell.open_file(it["local"])
        if act == "open_with":
            return shell.open_with(it["local"])
        if act == "folder":
            return shell.open_folder(it["local"])
        if act == "props":
            local = it["local"]
            exists = os.path.isfile(local)
            actual = os.path.getsize(local) if exists else None
            src = "导入的链接" if m.get("kind") == "import" else f"{m['repo']}（{m['repo_type']} @ {m['revision']}）"
            props = [["文件", it["hf"]], ["仓库", src], ["保存位置", local],
                     ["大小", size_both(it["size"]) + (f"（{it['size']:,} 字节）" if it["size"] else "")],
                     ["本地文件", (f"已存在，{size_both(actual)}" + ("，大小一致" if file_matches(local, it["size"]) else "，大小不一致"))
                      if exists else "不存在"],
                     ["检查", it["check"] or "—"], ["状态", it["state"] or "—"], ["勾选", "是" if it["checked"] else "否"]]
            if it.get("url"):
                props.append(["下载地址", it["url"]])
            return {"props": [props]}
        if act in ("continue", "redownload"):
            body = dict(b, iids=iids, force=act == "redownload")
            return self.api_batch_to_basic(body)
        if act == "stop":
            ids = [os.path.normcase(os.path.normpath(x["local"])) for x, _ in infos]
            return self.basic.stop_files(ids)
        return {"error": f"未知操作：{act}"}
