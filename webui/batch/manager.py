# -*- coding: utf-8 -*-
"""
IDM 批量下载（网页版）—— 数据与清单管理。下载在 batch/download.py，导入/导出在 batch/io.py。

优化：
  1. 勾选、改路径等操作只更新“显示有变化”的行（每行有版本号，网页只取变化的部分）。
  2. 检查“本地是否已存在”在后台线程进行，不会因为机械硬盘/网络盘卡住界面。
  3. 筛选条件合并为一个输入框（规则见 batch/filter.py），旧配置中的五个筛选框会自动转换。
"""
import os
import threading
import urllib.error

from core.common import (
    ENDPOINTS, size_both as human_size, sanitize_component, hf_name, complete_local_path, validate_local_path,
    file_matches, fetch_repo_files, explain_http_error, repo_key, parse_repo_input, detect_idm,
)
from shellops import files as shell
from batch.filter import matcher_from_filters, inspect_filters, legacy_to_query
from batch.aria2 import ARIA2_DEFAULTS, normalize_aria2_opts, detect_aria2
from batch.download import BatchDownloadMixin
from batch.io import BatchIOMixin
from batch.transform import BatchBasicMixin

DEFAULT_SETTINGS = {
    "repo_input": "", "repo_type": "model", "revision": "main", "source": "mirror",
    "root": os.getcwd(), "auto_subdir": True, "skip_existing": True,
    "idm_path": "", "aria2_path": "", "concurrency": 3, "auto_start": False,
    "live_filter": True,        # 输入筛选条件时实时勾选符合的文件
    "skip_basic_confirm": False,    # “用常规下载”不再弹出确认框
}
# 网页设置项 ↔ 配置文件中的键（沿用旧版的键名，配置可以和旧版共用）
CONFIG_KEYS = {
    "repo_input": "idm_repo_input", "repo_type": "idm_repo_type", "revision": "idm_revision",
    "source": "idm_source", "root": "idm_root", "auto_subdir": "idm_auto_subdir",
    "skip_existing": "idm_skip_existing", "idm_path": "idm_path", "aria2_path": "aria2_path",
    "concurrency": "idm_concurrency", "auto_start": "idm_auto_start", "live_filter": "idm_live_filter",
    "skip_basic_confirm": "idm_skip_basic_confirm",
}
LEGACY_FILTER_KEYS = ("ext", "name", "path", "exclude", "min", "max", "unit")


def need_confirm(cid, title, message):
    """需要用户确认：网页弹出确认框，确认后带上 cid 再调用一次"""
    return {"confirm": cid, "title": title, "message": message}


class BatchManager(BatchDownloadMixin, BatchIOMixin, BatchBasicMixin):
    def __init__(self, hub, get_token, on_save):
        self.hub = hub
        self.get_token = get_token          # 读取 HF Token（和“常规下载”页共用）
        self.on_save = on_save              # 保存配置
        self.lock = threading.RLock()
        self.s = dict(DEFAULT_SETTINGS)
        self.filters = {"q": ""}            # 合并后的筛选输入框
        self.aria2_opts = dict(ARIA2_DEFAULTS)
        self.repos = {}                     # 仓库标识 → 仓库信息（保持添加顺序）
        self.items = []                     # 所有仓库的文件清单
        self.by_iid = {}
        self._next_iid = 0
        self.rev = 0                        # 任何显示变化都 +1
        self.struct_rev = 0                 # 行的增删/排序变化时 +1（网页需要整表重取）
        self.busy = False
        self.stop_event = threading.Event()
        self.aria2_proc = None

    def log(self, *args):
        self.hub.log("batch", " ".join(str(a) for a in args))

    def notice(self, level, title, msg):
        self.hub.notice("batch", level, title, msg)

    # ------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------
    def load_config(self, data):
        data = data or {}
        with self.lock:
            for k, ck in CONFIG_KEYS.items():
                if ck in data:
                    self.s[k] = data[ck]
            if "idm_repo_input" not in data and data.get("idm_repo_id"):
                self.s["repo_input"] = data["idm_repo_id"]
            self.s["root"] = self.s["root"] or os.getcwd()
            self.s["idm_path"] = self.s["idm_path"] or detect_idm()
            self.s["aria2_path"] = self.s["aria2_path"] or detect_aria2()
            self.s["concurrency"] = self._concurrency()
            if "idm_filter" in data:
                self.filters["q"] = str(data.get("idm_filter") or "")
            else:       # 旧版的五个筛选框 → 合并成一行
                self.filters["q"] = legacy_to_query({k: data.get(f"idm_f_{k}", "") for k in LEGACY_FILTER_KEYS})
            self.aria2_opts, _ = normalize_aria2_opts(data.get("idm_aria2_opts"), fallback=True)
            for r in data.get("idm_repos", []):          # 上次的仓库列表（需点“刷新”重新获取文件）
                try:
                    repo, rtype, rev = r
                except Exception:
                    continue
                k = repo_key(repo, rtype, rev)
                if k not in self.repos:
                    self.repos[k] = {"key": k, "repo": repo, "repo_type": rtype, "revision": rev, "fetched": False}
            self.rev += 1

    def dump_config(self):
        with self.lock:
            out = {ck: self.s[k] for k, ck in CONFIG_KEYS.items()}
            out["idm_concurrency"] = self._concurrency()
            out["idm_filter"] = self.filters["q"]
            out["idm_aria2_opts"] = dict(self.aria2_opts)
            out["idm_repos"] = [[m["repo"], m["repo_type"], m["revision"]] for m in self.repos.values()
                                if m.get("kind") != "import"]
            return out

    def settings(self):
        with self.lock:
            return {"settings": dict(self.s), "filters": dict(self.filters), "aria2_opts": dict(self.aria2_opts),
                    "target": self.target_text()}

    def update_settings(self, body):
        """网页上的设置变化（根目录变化时自动重新计算默认保存路径）"""
        with self.lock:
            old_root, old_sub = self.root_path(), bool(self.s["auto_subdir"])
            for k in DEFAULT_SETTINGS:
                if k in (body.get("settings") or {}):
                    self.s[k] = body["settings"][k]
            f = body.get("filters") or {}
            if "q" in f:
                self.filters["q"] = str(f["q"] or "")
            self.s["concurrency"] = self._concurrency()
            if self.root_path() != old_root or bool(self.s["auto_subdir"]) != old_sub:
                self._recalc_paths()
            return {"ok": True, "target": self.target_text()}

    def _concurrency(self):
        try:
            return max(1, min(16, int(self.s["concurrency"])))
        except Exception:
            return 3

    # ------------------------------------------------------------
    # 保存位置
    # ------------------------------------------------------------
    def root_path(self):
        root = str(self.s["root"] or "").strip() or os.getcwd()
        return os.path.normpath(os.path.abspath(root))

    def download_base(self, meta):
        root = self.root_path()
        if self.s["auto_subdir"]:
            root = os.path.join(root, sanitize_component(meta["repo"].replace("/", "_"))[0])
        return root

    def target_text(self):
        root = self.root_path()
        if self.s["auto_subdir"]:
            return f"实际下载位置：{os.path.join(root, '<仓库名>')}{os.sep}…（每个仓库一个文件夹，里面的结构和 HF 完全一致）"
        return f"实际下载位置：{root}{os.sep}…（所有仓库的文件直接按 HF 原路径放在这里，可能出现同名冲突）"

    def _default_local(self, it):
        parts, renamed = [], False
        for seg in it["hf"].split("/"):
            s, changed = sanitize_component(seg)
            parts.append(s)
            renamed = renamed or changed
        meta = self.repos[it["repo_key"]]
        base = self.root_path() if meta.get("kind") == "import" else self.download_base(meta)
        return os.path.normpath(os.path.join(base, *parts)), renamed

    def _recalc_paths(self):
        for it in self.items:
            if not it["custom"]:
                it["local"], it["renamed"] = self._default_local(it)
        self._refresh_flags()
        self._render_all()
        self._check_exists_async(self.items)

    def new_folder(self, parent, name):
        parent = parent if parent and os.path.isdir(parent) else os.getcwd()
        if not (name or "").strip():
            return {"error": "请输入文件夹名称。"}
        clean, changed = sanitize_component(name.strip())
        path = os.path.join(parent, clean)
        try:
            os.makedirs(path, exist_ok=True)
        except Exception as e:
            return {"error": f"新建失败：{e}"}
        self.update_settings({"settings": {"root": path}})
        self.log(f"已新建文件夹：{path}")
        out = {"ok": True, "root": path}
        if changed:
            out["info"] = f"名称里有 Windows 不允许的字符，已自动替换为：\n{clean}"
        return out

    def open_folder(self, view_key):
        with self.lock:
            cands = []
            if view_key and view_key in self.repos:
                cands.append(self.download_base(self.repos[view_key]))
            cands.append(self.root_path())
        for p in cands:
            if os.path.isdir(p):
                return shell.open_dir(p)
        return {"info": "文件夹还不存在（推送下载时会自动创建）。"}

    # ------------------------------------------------------------
    # 显示：每行只在内容变化时更新版本号
    # ------------------------------------------------------------
    @staticmethod
    def _row(it):
        st = it["state"]
        if st.startswith("已完成"):
            tag = "done"
        elif st.startswith("失败") or it["conflict"] or "路径过长" in it["check"]:
            tag = "warn"
        elif not it["checked"]:
            tag = "off"
        elif it["exists"]:
            tag = "done"
        elif it["custom"]:
            tag = "custom"
        else:
            tag = ""
        return it["checked"], it["local"], st or it["check"], tag, it["exists"], it["conflict"]

    def _touch(self, it):
        disp = self._row(it)
        if disp != it["_disp"]:
            it["_disp"] = disp
            self.rev += 1
            it["rev"] = self.rev

    def _render_all(self):
        for it in self.items:
            self._touch(it)

    def _refresh_flags(self):
        """检查：路径冲突 / 本地已存在 / 路径过长 / 自动改名 / 自定义路径"""
        count = {}
        for it in self.items:
            if it["checked"]:
                k = os.path.normcase(it["local"])
                count[k] = count.get(k, 0) + 1
        for it in self.items:
            it["conflict"] = it["checked"] and count.get(os.path.normcase(it["local"]), 0) > 1
            flags = []
            if it["conflict"]:
                flags.append("路径冲突")
            if it["exists"]:
                flags.append("本地已存在")
            if len(it["local"]) >= 260:
                flags.append("路径过长")
            if it["custom"]:
                flags.append("自定义路径")
            elif it["renamed"]:
                flags.append("已自动改名")
            it["check"] = "；".join(flags)

    def _after_change(self):
        self._refresh_flags()
        self._render_all()

    def _set_state(self, it, text):
        with self.lock:
            it["state"] = text
            if text.startswith("已完成"):
                it["exists"] = True
                self._refresh_flags()
            self._touch(it)

    def _check_exists_async(self, items):
        """在后台检查本地文件是否已存在（不阻塞网页和其他操作）"""
        jobs = [(it, it["local"], it["size"]) for it in items]
        if not jobs:
            return

        def work():
            res = [(it, local, file_matches(local, size)) for it, local, size in jobs]
            with self.lock:
                changed = False
                for it, local, ex in res:
                    if it["local"] == local and it["exists"] != ex and it["iid"] in self.by_iid:
                        it["exists"] = ex
                        changed = True
                if changed:
                    self._after_change()
        threading.Thread(target=work, daemon=True).start()

    def _new_item(self, key, hf, size, lfs=False, **extra):
        iid = f"i{self._next_iid}"
        self._next_iid += 1
        it = {"iid": iid, "repo_key": key, "hf": hf, "size": size, "lfs": lfs, "checked": True,
              "local": "", "custom": False, "renamed": False, "exists": False, "conflict": False,
              "check": "", "state": "", "_disp": None, "rev": 0}
        it.update(extra)
        it["local"], it["renamed"] = self._default_local(it)
        self.by_iid[iid] = it
        return it

    # ------------------------------------------------------------
    # 给网页的数据
    # ------------------------------------------------------------
    @staticmethod
    def _access_text(meta, long=False):
        if meta.get("kind") == "import":
            return "导入的链接：按原地址直接下载" if long else "导入的链接"
        if not meta.get("fetched"):
            return "未获取（点刷新）"
        if meta.get("private"):
            return "私有仓库：下载需要 Token" if long else "私有·需Token"
        if meta.get("gated"):
            return "受限仓库：下载需要 Token，并需先在网页上同意协议" if long else "受限·需Token"
        return "公开仓库：下载不需要 Token" if long else "公开"

    def snapshot(self, since, struct):
        with self.lock:
            if since == self.rev and struct == self.struct_rev:
                return {"same": True}
            full = struct != self.struct_rev
            src = self.items if full else [it for it in self.items if it["rev"] > since]
            rows = []
            for it in src:
                checked, local, status, tag, exists, conflict = it["_disp"] or self._row(it)
                rows.append([it["iid"], it["repo_key"], it["hf"], it["size"], checked, local, status, tag,
                             exists, conflict])
            repos = [[k, m["repo"], m["repo_type"], m["revision"], self._access_text(m),
                      bool(m.get("needs_token")), bool(m.get("fetched")), m.get("kind", "")]
                     for k, m in self.repos.items()]
            running = self.aria2_proc is not None and self.aria2_proc.poll() is None
            return {"rev": self.rev, "struct": self.struct_rev, "full": full, "items": rows, "repos": repos,
                    "busy": self.busy, "aria2_running": running, "target": self.target_text()}

    def _visible(self, view_key):
        if not view_key:
            return self.items
        return [it for it in self.items if it["repo_key"] == view_key]

    # ------------------------------------------------------------
    # 仓库：添加 / 刷新 / 移除
    # ------------------------------------------------------------
    def add_repos(self, body):
        self.update_settings(body)
        with self.lock:
            text, rtype, rev = self.s["repo_input"], self.s["repo_type"], (self.s["revision"] or "").strip() or "main"
        specs, bad = parse_repo_input(text, rtype, rev)
        if not specs:
            if bad:
                return {"warn": "以下内容不是有效的仓库ID（格式应为 用户名/仓库名）：\n" + "\n".join(bad[:10])}
            return {"error": "请输入仓库ID，如 Sentinel7/h3"}
        res = self._start_fetch(specs, confirmed=body.get("confirmed") or [])
        if bad and res.get("ok"):
            res["warn"] = "以下内容不是有效的仓库ID（格式应为 用户名/仓库名），已忽略：\n" + "\n".join(bad[:10])
        return res

    def refresh_repos(self, keys, confirmed):
        with self.lock:
            if keys is None:
                specs = [(m["repo"], m["repo_type"], m["revision"]) for m in self.repos.values()
                         if m.get("kind") != "import"]
                if not specs:
                    return {"info": "还没有添加仓库。"}
            else:
                keys = [k for k in keys if k in self.repos]
                if not keys:
                    return {"info": "请先在仓库列表中选中要刷新的仓库。"}
                keys = [k for k in keys if self.repos[k].get("kind") != "import"]
                if not keys:
                    return {"info": "导入的链接不需要刷新。"}
                specs = [(self.repos[k]["repo"], self.repos[k]["repo_type"], self.repos[k]["revision"]) for k in keys]
        return self._start_fetch(specs, confirmed=confirmed)

    def remove_repos(self, keys, confirmed):
        with self.lock:
            keys = [k for k in keys if k in self.repos]
            if not keys:
                return {"info": "请先在仓库列表中选中要移除的仓库。"}
            if "remove" not in confirmed:
                return need_confirm("remove", "移除仓库", f"确定从列表中移除 {len(keys)} 个仓库吗？（不会删除已下载的文件）")
            for k in keys:
                self._remove_repo_items(k)
                self.repos.pop(k, None)
            self.rev += 1
            self._after_change()
        self.on_save()
        return {"ok": True}

    def _remove_repo_items(self, key):
        old = [it for it in self.items if it["repo_key"] == key]
        if old:
            for it in old:
                self.by_iid.pop(it["iid"], None)
            self.items = [it for it in self.items if it["repo_key"] != key]
            self.struct_rev += 1

    def _start_fetch(self, specs, after=None, confirmed=()):
        """获取仓库文件列表（后台进行）。after：全部获取完后调用的函数"""
        with self.lock:
            if self.busy:
                return {"info": "当前有任务正在进行，请稍候。"}
            existing = [s for s in specs if self.repos.get(repo_key(*s), {}).get("fetched")]
            if existing and "refetch" not in confirmed and after is None:
                return need_confirm("refetch", "重新获取",
                                    f"有 {len(existing)} 个仓库已在列表中，重新获取会重置它们的勾选和自定义路径。\n是否继续？")
            source = self.s["source"]
            # 列表先不带 Token 获取；遇到 401 / 403 / 404 再带上 Token 重试（国内镜像、官方源都一样）
            token = self.get_token()
            self._set_busy(True)
        self.on_save()
        threading.Thread(target=self._fetch_worker, args=(specs, source, token, after), daemon=True).start()
        return {"ok": True}

    def _fetch_worker(self, specs, source, token, after=None):
        endpoint = ENDPOINTS.get(source, ENDPOINTS["mirror"])
        errors = []
        try:
            for i, (repo, rtype, rev) in enumerate(specs, 1):
                self.log(f"\n--- [{i}/{len(specs)}] 获取文件列表: {repo}（类型 {rtype}，分支 {rev}，来源 {endpoint}）---")
                try:
                    try:
                        info, files = fetch_repo_files(endpoint, rtype, repo, rev, None, log=self.log)
                    except urllib.error.HTTPError as e:
                        if e.code in (401, 403, 404) and token:
                            self.log(f"未带 Token 访问失败（HTTP {e.code}），正在带上 Token 重试…"
                                     + ("（Token 会经过 hf-mirror.com）" if source == "mirror" else ""))
                            info, files = fetch_repo_files(endpoint, rtype, repo, rev, token, log=self.log)
                        else:
                            raise
                except urllib.error.HTTPError as e:
                    msg = explain_http_error(e)
                    if source == "mirror" and e.code in (401, 403, 404):
                        msg += "（私有仓库请先填 HF Token；还不行就在左侧导航栏把下载源切到“官方”）"
                    self.log(f"❌ 获取失败：{msg}")
                    errors.append(f"{repo}：{msg}")
                    continue
                except Exception as e:
                    self.log(f"❌ 获取失败：{e}")
                    errors.append(f"{repo}：{e}（请检查网络，或切换下载源后重试）")
                    continue
                gated, private = info.get("gated") or False, bool(info.get("private"))
                meta = {"key": repo_key(repo, rtype, rev), "repo": repo, "repo_type": rtype, "revision": rev,
                        "gated": gated, "private": private, "needs_token": bool(gated) or private, "fetched": True}
                self._on_repo_fetched(meta, files)
            if errors:
                self.notice("error", "部分仓库获取失败", "\n\n".join(errors[:10]))
            if after is not None:
                after()
        finally:
            with self.lock:
                self._set_busy(False)
            self.on_save()

    def _on_repo_fetched(self, meta, files):
        with self.lock:
            key = meta["key"]
            self._remove_repo_items(key)
            self.repos[key] = meta                  # 已存在的仓库保持原来的顺序
            try:
                matcher = matcher_from_filters(self.filters)
            except ValueError:
                matcher = None
            use_filter = matcher is not None and matcher.active
            new_items = []
            for f in files:
                it = self._new_item(key, f["path"], f["size"], f["lfs"])
                if use_filter:
                    it["checked"] = matcher(it["hf"], it["size"])
                new_items.append(it)
            self.items.extend(new_items)
            order = {k: i for i, k in enumerate(self.repos)}
            self.items.sort(key=lambda x: order.get(x["repo_key"], 0))
            self.struct_rev += 1
            self._after_change()
            self._check_exists_async(new_items)
        total = sum(it["size"] or 0 for it in new_items)
        chk = sum(1 for it in new_items if it["checked"])
        self.log(f"✅ {meta['repo']}：{len(new_items)} 个文件，共 {human_size(total)}。{self._access_text(meta, long=True)}")
        if use_filter:
            self.log(f"   已按当前筛选条件勾选 {chk} 个文件（如需全部，点“全部勾选”或“清空条件”）。")
        if meta["needs_token"] and not self.get_token():
            self.notice("warn", "需要 Token", f"{meta['repo']} 需要 HF Token 才能下载，请先在“常规下载”页填写 Token。")

    # ------------------------------------------------------------
    # 勾选 / 筛选
    # ------------------------------------------------------------
    def set_checked(self, op, iids=None, view_key=None):
        with self.lock:
            if op in ("toggle", "check", "uncheck"):
                targets = [self.by_iid[i] for i in (iids or []) if i in self.by_iid]
                if not targets:
                    return {"info": "请先在列表中选中行（单击行，按住 Ctrl/Shift 可多选）。"}
            else:
                targets = self._visible(view_key)
            if op == "toggle_all":      # 点表头：全部已勾选时全部取消，否则全部勾选
                op = "all_off" if targets and all(it["checked"] for it in targets) else "all_on"
            for it in targets:
                it["checked"] = (not it["checked"]) if op in ("toggle", "invert") else (op in ("check", "all_on"))
            self._after_change()
        return {"ok": True}

    def filter_preview(self, filters, view_key):
        """实时预览：识别出的条件（小标签）+ 符合条件的文件数"""
        try:
            m, chips = inspect_filters(filters)
        except ValueError as e:
            return {"text": f"⚠ {e}（示例：>1GB、<500MB、100MB~2GB）", "level": "warn", "chips": []}
        with self.lock:
            if not m.active:
                return {"text": "未设置筛选条件", "level": "muted", "chips": []}
            if not self.items:
                return {"text": "", "level": "muted", "chips": chips}
            hit = [it for it in self._visible(view_key) if m(it["hf"], it["size"])]
        return {"text": f"符合条件：{len(hit)} 个，{human_size(sum(it['size'] or 0 for it in hit))}",
                "level": "info", "chips": chips}

    def apply_filter(self, mode, filters, view_key, live=False):
        """
        mode：set=只勾选符合的；add=追加勾选；remove=取消勾选符合的
        live=True：输入时实时勾选（不弹提示、不写日志；条件写错时保持原样；清空条件 = 全部勾选）
        """
        with self.lock:
            if filters and "q" in filters:
                self.filters["q"] = str(filters["q"] or "")
            vis = self._visible(view_key)
            if not vis:
                return {"ok": False} if live else {"info": "请先添加仓库并获取文件列表。"}
            try:
                m = matcher_from_filters(self.filters)
            except ValueError as e:
                if live:
                    return {"ok": False}
                return {"error": f"{e}\n\n大小可以这样写：>1GB、<500MB、100MB~2GB（只写数字按 MB）"}
            if not m.active:
                if not live:
                    return {"info": "还没有填写任何筛选条件。\n例如在筛选框里填 .safetensors"}
                for it in vis:      # 实时模式下清空条件 = 不限制 = 全部勾选
                    it["checked"] = True
                self._after_change()
                return {"ok": True, "n": len(vis)}
            n = 0
            for it in vis:
                hit = m(it["hf"], it["size"])
                n += hit
                if mode == "set":
                    it["checked"] = hit
                elif hit:
                    it["checked"] = mode == "add"
            self._after_change()
        if not live:
            self.log(f"筛选：{n} 个文件符合条件（{'只勾选' if mode == 'set' else '追加勾选' if mode == 'add' else '取消勾选'}）。")
        return {"ok": True, "n": n}

    # ------------------------------------------------------------
    # 自定义保存路径
    # ------------------------------------------------------------
    def path_info(self, iid, text=None):
        """修改路径窗口：返回默认路径、补全后的最终路径和错误说明"""
        with self.lock:
            it = self.by_iid.get(iid)
            if not it:
                return {"error": "找不到这个文件。"}
            meta = self.repos[it["repo_key"]]
            default = self._default_local(it)[0]
            cur = it["local"]
        name = hf_name(it["hf"])
        p, added = complete_local_path(cur if text is None else text, name)
        err = validate_local_path(p) if p else "请填写保存位置"
        return {"repo": meta["repo"], "hf": it["hf"], "size": human_size(it["size"]), "local": cur,
                "default": default, "final": p, "added": added, "error": err, "filename": name}

    def set_path(self, iid, text):
        info = self.path_info(iid, text)
        if info.get("error"):
            return {"error": info["error"]}
        with self.lock:
            it = self.by_iid.get(iid)
            if not it:
                return {"error": "找不到这个文件。"}
            p = info["final"]
            default, renamed = self._default_local(it)
            it["custom"] = os.path.normcase(p) != os.path.normcase(default)
            it["renamed"] = renamed if not it["custom"] else False
            it["local"] = p
            it["exists"] = False
            self._after_change()
            self._check_exists_async([it])
        return {"ok": True}

    def change_dir(self, iids, d, keep):
        with self.lock:
            sel = [self.by_iid[i] for i in iids or [] if i in self.by_iid]
            if not sel:
                return {"info": "请先在列表中选中要修改的文件（单击行，按住 Ctrl/Shift 可多选；Ctrl+A 全选）。"}
            for it in sel:
                parts = [sanitize_component(s)[0] for s in it["hf"].split("/")]
                it["local"] = os.path.normpath(os.path.join(d, *(parts if keep else parts[-1:])))
                it["custom"] = True
                it["exists"] = False
            self._after_change()
            self._check_exists_async(sel)
        return {"ok": True}

    def reset_paths(self, iids):
        with self.lock:
            sel = [self.by_iid[i] for i in iids or [] if i in self.by_iid]
            if not sel:
                return {"info": "请先在列表中选中要恢复的文件。"}
            for it in sel:
                it["custom"] = False
                it["local"], it["renamed"] = self._default_local(it)
                it["exists"] = False
            self._after_change()
            self._check_exists_async(sel)
        return {"ok": True}

    def _set_busy(self, busy):
        """调用时需持有 self.lock"""
        self.busy = busy
        self.rev += 1
