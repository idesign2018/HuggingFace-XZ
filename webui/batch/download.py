# -*- coding: utf-8 -*-
"""
IDM 批量下载（网页版）—— 下载部分：推送到 IDM、用 aria2 下载、aria2 高级设置、停止。
导入/导出在 batch/io.py。由 batch/manager.py 的 BatchManager 继承使用。
"""
import os
import time
import threading
import subprocess
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlparse

from core.common import (
    ENDPOINTS, IDM_DEFAULT_HINT, CREATE_NEW_CONSOLE, size_both as human_size, hf_name, file_matches, build_resolve_url,
    resolve_direct_link, download_with_token, explain_http_error, is_process_running, make_headers, NoRedirect,
)
from batch.aria2 import (
    aria2_exe, normalize_aria2_opts, aria2_option_args, format_cli, describe_proxy, write_aria2_file,
)
from core import net

IDM_QUEUE_TIPS = ("\n\n📌 在 IDM 里找不到这些文件？\n"
                  "还没开始下载的文件，IDM 不会显示大小、状态和“最后连接时间”，\n"
                  "所以在“全部任务”里按时间排序时，它们会排在列表最底部。\n"
                  "查看方法：点 IDM 左侧“未完成”分类，或“队列 → 主要下载队列”。")


def _confirm(cid, title, message):
    return {"confirm": cid, "title": title, "message": message}


class BatchDownloadMixin:
    # ------------------------------------------------------------
    # 下载前的准备
    # ------------------------------------------------------------
    def _collect_jobs(self, confirmed):
        """收集所有仓库中要下载的文件。返回 (任务列表, 给网页的回复)；不能继续时任务列表为 None"""
        with self.lock:
            if self.busy:
                return None, {"info": "当前有任务正在进行，请等待完成或点“停止”。"}
            if not self.items:
                return None, {"info": "请先添加仓库并获取文件列表。"}
            fixed = 0
            for it in self.items:   # 保存路径是已存在的文件夹时，自动补上原文件名（避免 IDM 报“拒绝访问”）
                if it["checked"] and os.path.isdir(it["local"]):
                    it["local"] = os.path.join(it["local"], hf_name(it["hf"]))
                    fixed += 1
            if fixed:
                self.log(f"提示：有 {fixed} 个文件的保存路径只填了文件夹，已自动补上文件名。")
            checked = [it for it in self.items if it["checked"]]
        # 检查本地文件时不持有锁（硬盘慢时也不会挡住网页刷新）
        exists = {it["iid"]: file_matches(it["local"], it["size"]) for it in checked}
        with self.lock:
            for it in checked:
                it["exists"] = exists.get(it["iid"], it["exists"])
            self._after_change()
            if not checked:
                return None, {"info": "没有勾选任何文件。"}
            conflicts = [it for it in checked if it["conflict"]]
            if conflicts and "conflict" not in confirmed:
                return None, _confirm("conflict", "路径冲突",
                                      f"有 {len(conflicts)} 个文件的本地保存路径重复，这些文件将被跳过。\n"
                                      f"例如：{conflicts[0]['local']}\n\n是否继续？")
            endpoint = ENDPOINTS.get(self.s["source"], ENDPOINTS["mirror"])
            jobs, skipped = [], 0
            for it in checked:
                if it["conflict"]:
                    continue
                if self.s["skip_existing"] and it["exists"]:
                    skipped += 1
                    continue
                m = self.repos[it["repo_key"]]
                if m.get("kind") == "import":
                    url, mirrors = it["url"], it.get("mirrors") or []
                else:
                    url, mirrors = build_resolve_url(endpoint, m["repo_type"], m["repo"], m["revision"], it["hf"]), []
                jobs.append({"it": it, "repo": m["repo"], "needs_token": bool(m.get("needs_token")), "url": url,
                             "mirrors": mirrors, "headers": it.get("headers") or [], "local": it["local"],
                             "size": it["size"]})
        if skipped:
            self.log(f"跳过本地已存在的文件 {skipped} 个。")
        if not jobs:
            return None, {"info": "没有需要下载的文件（勾选的文件都已存在或被跳过）。"}
        return jobs, None

    @staticmethod
    def _jobs_summary(jobs):
        stat = {}
        for j in jobs:
            s = stat.setdefault(j["repo"], [0, 0])
            s[0] += 1
            s[1] += j["size"] or 0
        lines = [f"  · {r}：{n} 个，{human_size(sz)}" for r, (n, sz) in list(stat.items())[:10]]
        if len(stat) > 10:
            lines.append(f"  ……共 {len(stat)} 个仓库")
        return "\n".join(lines)

    def _token_for_download(self, jobs, confirmed):
        """返回 (token, 回复)。公开仓库不发送 Token；受限 / 私有仓库没填 Token 时提示。
        IDM / aria2 没法“失败了再带 Token 重试”，受限 / 私有仓库的文件直接带上"""
        if not any(j["needs_token"] for j in jobs):
            return "", None
        token = self.get_token()
        if not token:
            return None, {"error": "勾选的文件中有受限/私有仓库，请先在“常规下载”页填写 HF Token，"
                                   "并确认已在网页上同意这些仓库的使用协议。"}
        if self.s["source"] == "mirror":
            self.log("提示：受限/私有仓库的文件会带上 HF Token（Token 会经过 hf-mirror.com，由镜像站转交官方验证）。")
        return token, None

    # ------------------------------------------------------------
    # 推送到 IDM
    # ------------------------------------------------------------
    @staticmethod
    def _call_idm(cmd):
        """调用 IDMan.exe。IDM 已在运行时，这个命令会把任务交给 IDM 后立即退出"""
        p = subprocess.Popen(cmd)
        try:
            p.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass

    def _ensure_idm_running(self, idm):
        """IDM 没运行时先启动它，否则第一次调用会一直卡住"""
        if is_process_running("IDMan.exe"):
            return
        self.log("IDM 未运行，正在启动 IDM…")
        subprocess.Popen([idm])
        for _ in range(20):
            time.sleep(1)
            if is_process_running("IDMan.exe"):
                break
        time.sleep(2)

    def push_idm(self, confirmed):
        idm = str(self.s["idm_path"] or "").strip()
        if not idm or not os.path.isfile(idm):
            return {"error": f"请在“IDM 程序”中选择 IDM 安装目录里的 IDMan.exe\n（默认位置：{IDM_DEFAULT_HINT}）"}
        jobs, reply = self._collect_jobs(confirmed)
        if jobs is None:
            return reply
        token, reply = self._token_for_download(jobs, confirmed)
        if token is None:
            return reply
        public = [j for j in jobs if not j["needs_token"]]
        gated = [j for j in jobs if j["needs_token"]]
        conc, auto_start = self._concurrency(), bool(self.s["auto_start"])
        if "push" not in confirmed:
            desc = []
            if public:
                desc.append(f"公开仓库 {len(public)} 个文件：加入 IDM“主下载队列”"
                            + ("，并自动开始。" if auto_start else "（不会自动开始）。"))
            if gated:
                desc.append(f"受限/私有仓库 {len(gated)} 个文件：用 Token 换取临时直链，分批交给 IDM 立即下载（同时 {conc} 个）。\n"
                            f"   临时直链约 1 小时内有效，所以不能先排队后下载；请保持本程序打开直到全部完成。")
            total = human_size(sum(j["size"] or 0 for j in jobs))
            return _confirm("push", "确认推送到 IDM",
                            f"共 {len(jobs)} 个文件，合计 {total}\n{self._jobs_summary(jobs)}\n\n" + "\n".join(desc)
                            + "\n\n是否继续？")
        self.on_save()
        self.stop_event.clear()
        with self.lock:
            self._set_busy(True)
        threading.Thread(target=self._idm_worker, args=(public, gated, idm, token, conc, auto_start),
                         daemon=True).start()
        return {"ok": True}

    def _idm_worker(self, public, gated, idm, token, concurrency, auto_start):
        try:
            self._check_mirror((public or gated)[0]["url"])
            if self.s["source"] == "official":
                self.log("提示：IDM 用它自己的代理设置（IDM → 选项 → 代理服务器），本程序管不到；"
                         "官方源连不上时请在 IDM 里设置代理。")
            self._ensure_idm_running(idm)
            n_queued = self._idm_queue_part(public, idm, auto_start) if public else 0
            done = failed = left = 0
            if gated and not self.stop_event.is_set():
                done, failed, left = self._idm_presigned_part(gated, idm, token, concurrency)
            msg = []
            if public:
                msg.append(f"公开仓库：已把 {n_queued} 个文件加入 IDM 的“主下载队列”"
                           + ("，并已开始下载。" if auto_start else "（未自动开始）。") + IDM_QUEUE_TIPS)
            if gated:
                msg.append(f"受限/私有仓库：完成 {done} 个，失败 {failed} 个，未处理 {left} 个。"
                           + ("\n失败多为 403：请确认已在网页上同意仓库协议、Token 有读取权限。" if failed else ""))
            msg.append("建议：IDM 选项 → 连接 中把最大连接数设为 8~16；队列里同时下载 3~5 个文件。")
            self.notice("info", "推送结束", "\n\n".join(msg))
        except Exception as e:
            self.log(f"❌ 推送出错：{e}")
            self.notice("error", "推送出错", str(e))
        finally:
            with self.lock:
                self._set_busy(False)

    def _check_mirror(self, url):
        """hf-mirror.com 有时会把下载请求转到官方，这里检测一下并在日志中说明"""
        if urlparse(url).netloc.lower() != urlparse(ENDPOINTS["mirror"]).netloc:
            return
        try:
            req = urllib.request.Request(url, method="HEAD", headers=make_headers(None))
            net.opener_for(url, NoRedirect).open(req, timeout=20).close()
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location") or ""
            e.close()
            if "huggingface.co" in urlparse(urljoin(url, loc)).netloc.lower():
                self.log("提示：镜像站目前会把文件下载转到官方 huggingface.co，IDM 实际会从官方下载（可以正常使用）。")
        except Exception:
            pass

    def _idm_queue_part(self, jobs, idm, auto_start):
        """公开仓库：全部加入 IDM 主下载队列"""
        self.log(f"\n--- 推送到 IDM 主下载队列：{len(jobs)} 个文件 ---")
        n_ok = 0
        for i, j in enumerate(jobs, 1):
            if self.stop_event.is_set():
                self.log("已停止推送。")
                break
            folder, name = os.path.split(j["local"])
            os.makedirs(folder, exist_ok=True)
            # /d 地址  /p 保存文件夹  /f 文件名  /n 静默  /a 只加入队列不开始
            self._call_idm([idm, "/d", j["url"], "/p", folder, "/f", name, "/n", "/a"])
            n_ok += 1
            self._set_state(j["it"], "已加入 IDM 队列")
            if i % 20 == 0 or i == len(jobs):
                self.log(f"已推送 {i}/{len(jobs)}")
            time.sleep(0.3)     # 推送太快 IDM 可能漏接
        if auto_start and n_ok and not self.stop_event.is_set():
            self._call_idm([idm, "/s"])
            self.log("已开始 IDM 主下载队列。")
        self.log(f"✅ 已加入队列：{n_ok} 个文件。")
        if n_ok:
            self.log("   提示：未开始下载的文件在 IDM“全部任务”中排在最底部；"
                     "可点 IDM 左侧“未完成”或“队列 → 主要下载队列”查看。")
        return n_ok

    def _idm_presigned_part(self, jobs, idm, token, concurrency):
        """受限仓库：边换取临时直链边交给 IDM 立即下载，同时最多 concurrency 个；完成一个补一个"""
        pending, active = list(jobs), []
        done = failed = 0
        self.log(f"\n--- 临时直链模式：{len(jobs)} 个文件，同时 {concurrency} 个 ---")
        while (pending or active) and not self.stop_event.is_set():
            while pending and len(active) < concurrency and not self.stop_event.is_set():
                j = pending.pop(0)
                folder, name = os.path.split(j["local"])
                try:
                    os.makedirs(folder, exist_ok=True)
                    link = resolve_direct_link(j["url"], token)
                    if link is None:        # 服务器直接返回内容（普通小文件）：由本工具直接下载
                        self._set_state(j["it"], "本工具下载中…")
                        download_with_token(j["url"], token, j["local"], self.stop_event)
                        self._set_state(j["it"], "已完成")
                        done += 1
                        continue
                    self._call_idm([idm, "/d", link, "/p", folder, "/f", name, "/n"])
                    active.append([j, time.time(), False])
                    self._set_state(j["it"], "IDM 下载中（临时直链）")
                    time.sleep(0.5)
                except urllib.error.HTTPError as e:
                    failed += 1
                    self.log(f"❌ {j['repo']} / {j['it']['hf']}：{explain_http_error(e)}")
                    self._set_state(j["it"], f"失败：HTTP {e.code}")
                except InterruptedError:
                    pending.insert(0, j)
                    break
                except Exception as e:
                    failed += 1
                    self.log(f"❌ {j['repo']} / {j['it']['hf']}：{e}")
                    self._set_state(j["it"], "失败")
            still = []
            for rec in active:
                j, t0, warned = rec
                if os.path.isfile(j["local"]) and (j["size"] is None or file_matches(j["local"], j["size"])):
                    done += 1
                    self._set_state(j["it"], "已完成")
                    self.log(f"✅ 完成 {done}/{len(jobs)}：{j['repo']} / {j['it']['hf']}")
                else:
                    if not warned and time.time() - t0 > 50 * 60:
                        rec[2] = True
                        self.log(f"[提醒] {j['it']['hf']} 已下载超过 50 分钟，临时直链可能即将失效；"
                                 f"如果 IDM 报错，可改用“用 aria2 下载”（会自动续传）。")
                    still.append(rec)
            active = still
            if pending or active:
                self.stop_event.wait(5)
        if self.stop_event.is_set():
            self.log("已停止：不再推送新文件；IDM 中已经开始的下载会继续。")
        self.log(f"--- 临时直链模式结束：完成 {done} 个，失败 {failed} 个，未处理 {len(pending)} 个 ---")
        return done, failed, len(pending)

    # ------------------------------------------------------------
    # aria2
    # ------------------------------------------------------------
    def run_aria2(self, confirmed):
        if self.aria2_proc and self.aria2_proc.poll() is None:
            return {"info": "aria2 正在下载中。如需重新开始，请先点“停止”。"}
        exe = aria2_exe(self.s["aria2_path"])
        if not exe:
            return {"error": "找不到 aria2c。请先下载 aria2（Windows 版 zip），解压后在“aria2c 程序”中选择 aria2c.exe。\n"
                             "下载页面：https://github.com/aria2/aria2/releases"}
        jobs, reply = self._collect_jobs(confirmed)
        if jobs is None:
            return reply
        token, reply = self._token_for_download(jobs, confirmed)
        if token is None:
            return reply
        opts, errs = normalize_aria2_opts(self.aria2_opts)
        if errs:
            return {"error": "aria2 高级设置有误：\n" + "\n".join(errs)}
        root = self.root_path()
        try:
            os.makedirs(root, exist_ok=True)
            listfile = os.path.join(root, "_aria2_任务列表.txt")
            logfile = os.path.join(root, "_aria2_日志.txt")
            write_aria2_file(jobs, listfile)
        except Exception as e:
            return {"error": f"出错：{e}"}
        conc = self._concurrency()
        cmd = [exe, f"--input-file={listfile}", f"--max-concurrent-downloads={conc}",
               "--summary-interval=60", "--console-log-level=warn",
               f"--log={logfile}", "--log-level=notice"] + aria2_option_args(opts)
        auto_proxy, proxy_text = self._aria2_auto_proxy(opts)
        cmd += auto_proxy
        if token:
            auth_header = "Authorization: Bearer " + token     # Token 只通过命令行传给 aria2，不写进任务文件
            cmd.append("--header=" + auth_header)
        self.on_save()
        try:
            self.aria2_proc = subprocess.Popen(cmd, creationflags=CREATE_NEW_CONSOLE)
        except Exception as e:
            return {"error": f"启动 aria2 失败：{e}"}
        for j in jobs:
            self._set_state(j["it"], "aria2 下载中")
        self.log(f"\n--- 已启动 aria2：{len(jobs)} 个文件，同时 {conc} 个，每个文件 {opts['connections']} 线程"
                 f"（进度见新弹出的黑色窗口）---")
        self.log(f"任务列表：{listfile}\n日志：{logfile}")
        self.log(proxy_text or describe_proxy(opts))
        threading.Thread(target=self._watch_aria2, args=(self.aria2_proc, jobs), daemon=True).start()
        return {"ok": True}

    def _aria2_auto_proxy(self, opts):
        """官方源 + aria2 设置里选的是“不使用代理”：自动用“源”按钮检测到的代理。返回 (参数列表, 说明)"""
        if self.s["source"] != "official" or opts.get("proxy_mode", "none") != "none":
            return [], ""
        p = net.official_proxy(refresh=True)
        if p["url"]:
            return [f"--all-proxy={p['url']}"], f"代理：{p['text']}（官方源自动使用）"
        return [], "代理：" + p["text"]

    def _watch_aria2(self, proc, jobs):
        code = proc.wait()
        n_ok = 0
        for j in jobs:
            ok = file_matches(j["local"], j["size"]) if j["size"] is not None else os.path.isfile(j["local"])
            n_ok += ok
            self._set_state(j["it"], "已完成" if ok else "失败/未完成")
        with self.lock:
            self._after_change()
        self.log(f"--- aria2 已退出（代码 {code}）：完成 {n_ok}/{len(jobs)} ---")
        if code != 0:
            self.log("提示：未完成的文件可再点一次“用 aria2 下载”自动续传；具体原因见 _aria2_日志.txt")

    def set_aria2_opts(self, raw, concurrency=None):
        opts, errs = normalize_aria2_opts(raw)
        if errs:
            return {"error": "\n".join(errs)}
        with self.lock:
            self.aria2_opts = opts
            if concurrency is not None:
                self.s["concurrency"] = concurrency
                self.s["concurrency"] = self._concurrency()
        self.on_save()
        self.log(f"aria2 高级设置已保存：{format_cli(aria2_option_args(opts))}")
        self.log("   " + describe_proxy(opts))
        return {"ok": True}

    # ------------------------------------------------------------
    # 停止
    # ------------------------------------------------------------
    def stop(self, confirmed):
        stopped = False
        if self.busy:
            self.stop_event.set()
            self.log("正在停止…（当前这一步完成后停止）")
            stopped = True
        if self.aria2_proc and self.aria2_proc.poll() is None:
            if "stop_aria2" not in confirmed:
                return _confirm("stop_aria2", "停止 aria2", "aria2 正在下载，是否结束它？（之后可再次启动，自动续传）")
            self.aria2_proc.terminate()
            self.log("已结束 aria2。")
            stopped = True
        if not stopped:
            return {"info": "当前没有进行中的任务。"}
        return {"ok": True}

    def shutdown(self):
        self.stop_event.set()
