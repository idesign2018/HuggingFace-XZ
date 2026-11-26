# -*- coding: utf-8 -*-
"""
IDM 批量下载（网页版）—— 导入 / 导出：
  清单 CSV（可用 Excel 批量改路径）、aria2 任务文件、URL 列表、导入任务（aria2 任务文件 / URL 列表）。
由 batch/manager.py 的 BatchManager 继承使用。
"""
import os
import re
import csv
import time

from core.common import (
    sanitize_component, hf_name, complete_local_path, validate_local_path, repo_key, parse_hf_url, url_filename,
)
from batch.aria2 import (
    normalize_aria2_opts, aria2_option_args, format_cli, write_aria2_file, read_text_file, parse_task_text,
)


def _confirm(cid, title, message):
    return {"confirm": cid, "title": title, "message": message}


class BatchIOMixin:
    # ------------------------------------------------------------
    # 清单 CSV
    # ------------------------------------------------------------
    def export_name(self):
        with self.lock:
            fetched = [m for m in self.repos.values() if m.get("fetched")]
            if len(fetched) == 1:
                return sanitize_component(fetched[0]["repo"].replace("/", "_"))[0]
            return "多仓库"

    def export_csv(self, path):
        with self.lock:
            rows = [[1 if it["checked"] else 0, self.repos[it["repo_key"]]["repo"],
                     self.repos[it["repo_key"]]["repo_type"], self.repos[it["repo_key"]]["revision"], it["hf"],
                     "" if it["size"] is None else it["size"], it["local"]] for it in self.items]
        if not rows:
            return {"info": "请先添加仓库并获取文件列表。"}
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:     # utf-8-sig：Excel 打开不乱码
                w = csv.writer(f)
                w.writerow(["下载(1/0)", "仓库", "类型", "分支", "HF路径", "大小(字节)", "本地保存路径"])
                w.writerows(rows)
        except Exception as e:
            return {"error": f"导出失败：{e}"}
        self.log(f"清单已导出：{path}")
        return {"ok": True, "info": f"已导出 {len(rows)} 行。\n可用 Excel 修改“下载”和“本地保存路径”两列，再导入回来。\n\n{path}"}

    def import_csv(self, path):
        rows = None
        for enc in ("utf-8-sig", "gbk"):            # Excel 另存时可能变成 GBK 编码
            try:
                with open(path, "r", newline="", encoding=enc) as f:
                    rows = list(csv.reader(f))
                break
            except UnicodeDecodeError:
                continue
            except Exception as e:
                return {"error": f"导入失败：{e}"}
        if not rows:
            return {"error": "导入失败：文件为空或编码无法识别。"}
        with self.lock:
            if not self.items:
                return {"info": "请先添加清单中的仓库并获取文件列表，再导入清单。"}
            lookup = {(it["repo_key"], it["hf"]): it for it in self.items}
            updated, unmatched, bad, touched = 0, 0, [], []
            for row in rows[1:]:
                if len(row) < 5:
                    continue
                it = lookup.get((repo_key(row[1].strip(), row[2].strip(), row[3].strip()), row[4].strip()))
                if not it:
                    unmatched += 1
                    continue
                it["checked"] = row[0].strip().lower() in ("1", "是", "true", "y", "yes", "√", "☑")
                local = row[6].strip() if len(row) > 6 else ""
                if local:
                    local, _ = complete_local_path(local, hf_name(it["hf"]))
                    err = validate_local_path(local)
                    if err:
                        bad.append(f"{it['hf']}：{err}")
                    else:
                        default, renamed = self._default_local(it)
                        it["custom"] = os.path.normcase(local) != os.path.normcase(default)
                        it["renamed"] = False if it["custom"] else renamed
                        it["local"] = local
                        touched.append(it)
                updated += 1
            self._after_change()
            self._check_exists_async(touched)
        msg = f"已更新 {updated} 个文件。"
        if unmatched:
            msg += f"\n有 {unmatched} 行在当前列表中找不到对应文件（仓库未添加或文件已变化），已忽略。"
        if bad:
            msg += f"\n有 {len(bad)} 个路径无效，保持原路径：\n" + "\n".join(bad[:10]) + ("\n……" if len(bad) > 10 else "")
        return {"ok": True, "info": msg}

    # ------------------------------------------------------------
    # 导出 aria2 任务文件 / URL 列表
    # ------------------------------------------------------------
    def export_aria2(self, path, confirmed):
        jobs, reply = self._collect_jobs(confirmed)
        if jobs is None:
            return reply
        try:
            write_aria2_file(jobs, path, make_dirs=False)
        except Exception as e:
            return {"error": f"导出失败：{e}"}
        header = ' --header="Authorization: Bearer 你的Token"' if any(j["needs_token"] for j in jobs) else ""
        opts, _ = normalize_aria2_opts(self.aria2_opts, fallback=True)
        args = aria2_option_args(opts) + self._aria2_auto_proxy(opts)[0]       # 官方源：自动带上代理
        cmd = f'aria2c -i "{path}" -j {self._concurrency()} ' + format_cli(args) + header
        self.log(f"aria2 任务文件已导出（{len(jobs)} 个文件，不含 Token）：{path}\n使用命令：\n{cmd}")
        return {"ok": True, "info": f"已导出 {len(jobs)} 个文件（文件中不含 Token）。\n使用命令已打印在下方日志中，可直接复制。"}

    def export_urls(self, path, confirmed):
        jobs, reply = self._collect_jobs(confirmed)
        if jobs is None:
            return reply
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(j["url"] for j in jobs) + "\n")
        except Exception as e:
            return {"error": f"导出失败：{e}"}
        self.log(f"URL 列表已导出：{path}")
        note = ("\n\n注意：纯 URL 列表不包含保存路径，在 IDM 里“导入 → 从文本文件导入”后文件会放在同一个文件夹，"
                "不保留目录结构；需要保留结构请用“推送到 IDM”。")
        if any(j["needs_token"] for j in jobs):
            note += "\n\n其中有需要 Token 的仓库，这些地址在 IDM 中无法直接下载。"
        return {"ok": True, "info": f"已导出 {len(jobs)} 个地址。{note}"}

    # ------------------------------------------------------------
    # 导入任务：aria2 任务文件 / URL 列表
    # ------------------------------------------------------------
    def import_tasks(self, paths, confirmed):
        if self.busy:
            return {"info": "当前有任务正在进行，请稍候。"}
        entries, n_magnet, n_bad = [], 0, 0
        for p in paths:
            try:
                e, m, b = parse_task_text(read_text_file(p))
            except Exception as ex:
                return {"error": f"读取失败：{p}\n{ex}"}
            entries += e
            n_magnet += m
            n_bad += b
        if not entries:
            return {"warn": "文件里没有找到 http/https/ftp 下载地址。"}
        hf_files, hf_repos, others = {}, [], []
        for en in entries:
            info = parse_hf_url(en["urls"][0])
            if info is None:
                others.append(en)
            elif info[0] == "file":
                hf_files.setdefault(info[1], []).append((info[2], en))
            elif info[1] not in hf_repos:
                hf_repos.append(info[1])
        specs = list(hf_files.keys()) + [s for s in hf_repos if s not in hf_files]
        if "import" not in confirmed:
            text = [f"共识别到 {len(entries)} 个下载任务："]
            if hf_files:
                text.append(f"  · Hugging Face 文件链接 {sum(len(v) for v in hf_files.values())} 个"
                            f"（来自 {len(hf_files)} 个仓库，会自动获取仓库信息、只勾选这些文件）")
            if hf_repos:
                text.append(f"  · Hugging Face 仓库链接 {len(hf_repos)} 个（整个仓库加入列表）")
            if others:
                text.append(f"  · 其他网站的普通链接 {len(others)} 个（按原地址直接下载）")
            if n_magnet:
                text.append(f"  · 磁力链接 {n_magnet} 个：不支持，已跳过")
            if n_bad:
                text.append(f"  · 无法识别的行 {n_bad} 行：已忽略")
            n_dir = sum(1 for en in entries if en["opts"].get("dir") or en["opts"].get("out"))
            if n_dir:
                text.append(f"\n其中 {n_dir} 个任务在文件里写了保存位置（dir/out），会照原样使用。")
            return _confirm("import", "导入任务", "\n".join(text) + "\n\n是否导入？")
        if others:
            self._add_import_items(others, os.path.basename(paths[0]))
        if specs:
            self.log(f"\n--- 导入：正在获取 {len(specs)} 个 Hugging Face 仓库的文件列表 ---")
            return self._start_fetch(specs, after=lambda: self._apply_imported_hf(hf_files))
        self.log(f"✅ 已导入 {len(others)} 个普通链接。")
        return {"ok": True}

    def _entry_local(self, en, fallback_rel):
        """根据任务文件中的 dir/out 计算保存路径；都没写时返回 None（使用默认路径）"""
        d, out = en["opts"].get("dir", "").strip(), en["opts"].get("out", "").strip()
        if not d and not out:
            return None
        out_parts = [sanitize_component(s)[0] for s in re.split(r"[\\/]+", out) if s] if out else []
        if not out_parts:
            out_parts = [hf_name(fallback_rel)]
        base = (d if os.path.isabs(d) else os.path.join(self.root_path(), d)) if d else self.root_path()
        return os.path.normpath(os.path.join(base, *out_parts))

    def _set_import_local(self, it, local):
        if not local:
            return
        err = validate_local_path(local)
        if err:
            self.log(f"⚠ {it['hf']}：任务文件中的保存位置无效（{err}），改用默认位置。")
            return
        default, _ = self._default_local(it)
        it["local"] = local
        it["custom"] = os.path.normcase(local) != os.path.normcase(default)
        if it["custom"]:
            it["renamed"] = False

    def _add_import_items(self, entries, source_name):
        """把普通链接加入列表（作为一个“导入的链接”组）"""
        with self.lock:
            key = "import:" + time.strftime("%Y%m%d-%H%M%S") + f"-{self._next_iid}"
            self.repos[key] = {"key": key, "repo": f"导入：{source_name}", "repo_type": "import", "revision": "-",
                               "kind": "import", "fetched": True, "needs_token": False, "gated": False, "private": False}
            used, new_items = set(), []
            for en in entries:
                name = sanitize_component(url_filename(en["urls"][0]))[0]
                rel, n = name, 2
                stem, ext = os.path.splitext(name)
                while rel.lower() in used:          # 同名文件自动编号，避免互相覆盖
                    rel = f"{stem}_{n}{ext}"
                    n += 1
                used.add(rel.lower())
                it = self._new_item(key, rel, None, url=en["urls"][0], mirrors=en["urls"][1:],
                                    headers=list(en["headers"]))
                self._set_import_local(it, self._entry_local(en, rel))
                new_items.append(it)
            self.items.extend(new_items)
            self.struct_rev += 1
            self._after_change()
        n_hdr = sum(1 for it in new_items if it["headers"])
        self.log(f"✅ 已导入普通链接 {len(new_items)} 个（默认保存在根目录下，文件大小要开始下载后才知道）。")
        if n_hdr:
            self.log(f"   提示：其中 {n_hdr} 个带自定义请求头（header），只有 aria2 能使用；IDM 推送时会忽略。")

    def _apply_imported_hf(self, hf_files):
        """HF 仓库获取完成后：只勾选导入的那些文件，并套用任务文件里的保存位置"""
        n_ok, missing = 0, []
        with self.lock:
            touched = []
            for spec, lst in hf_files.items():
                k = repo_key(*spec)
                if not self.repos.get(k, {}).get("fetched"):
                    missing += [p for p, _ in lst]
                    continue
                items = {it["hf"]: it for it in self.items if it["repo_key"] == k}
                for it in items.values():
                    it["checked"] = False
                for hf_path, en in lst:
                    it = items.get(hf_path)
                    if it is None:
                        missing.append(f"{spec[0]}/{hf_path}")
                        continue
                    it["checked"] = True
                    self._set_import_local(it, self._entry_local(en, hf_path))
                    touched.append(it)
                    n_ok += 1
            self._after_change()
            self._check_exists_async(touched)
        self.log(f"✅ 导入完成：已勾选 {n_ok} 个 Hugging Face 文件。")
        if missing:
            self.log(f"⚠ 有 {len(missing)} 个文件在仓库中没有找到（可能已被删除或改名），例如：{missing[0]}")
