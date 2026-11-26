/* 页面 1：常规下载 */
"use strict";
(function () {
  const { $, api, act, size, dur, esc } = HFD;
  const RANK = { "下载中": 0, "等待中": 1, "失败": 2, "已中断": 3, "已停止": 3, "已取消": 4, "完成": 5, "已是最新": 6 };
  /* 行数据：[id, 文件名, 大小, 已下载, 状态, 已开始传输, 速度, 平均速度, 错误, 仓库, 保存位置, 时间] */
  const B = HFD.basic = { struct: null, rev: 0, rows: new Map(), order: [], view: [], running: false, dirtyOrder: true,
    sel: new HFD.Selection(), checked: new Set() };

  function form() {
    return {
      repo: normRepo($("#b-repo").value), pattern: $("#b-pattern").value.trim(), path: $("#b-path").value.trim(),
      token: $("#b-token").value.trim(), no_symlinks: $("#b-nosym").checked,
      workers: Number($("#b-workers").value) || 4, threads: Number($("#b-threads").value) || 16,
      high_perf: $("#b-highperf").checked,
    };
  }

  /* 允许直接粘贴网页地址：https://huggingface.co/用户/仓库/... → 用户/仓库 */
  function normRepo(t) {
    t = (t || "").trim();
    const m = t.match(/^https?:\/\/[^/]+\/(?:models\/)?([^/\s?#]+\/[^/\s?#]+)/);
    return m ? m[1] : t.replace(/^\/+|\/+$/g, "");
  }

  B.init = function (s) {
    $("#b-repo").value = s.repo || "";
    $("#b-pattern").value = s.pattern || "";
    $("#b-path").value = s.path || "";
    $("#b-token").value = s.token || "";
    $("#b-nosym").checked = s.no_symlinks !== false;
    $("#b-workers").value = s.workers || 4;
    $("#b-threads").value = s.threads || 16;
    $("#b-highperf").checked = !!s.high_perf;

    const save = HFD.debounce(() => api("basic_save", form()).catch(() => {}), 600);
    ["#b-repo", "#b-pattern", "#b-path", "#b-token", "#b-workers", "#b-threads"]
      .forEach((k) => $(k).addEventListener("input", save));
    ["#b-nosym", "#b-highperf"].forEach((k) => $(k).addEventListener("change", save));

    $("#b-browse").onclick = async () => {
      const d = await HFD.pick({ kind: "dir", title: "选择保存路径", initialdir: $("#b-path").value });
      if (d) { $("#b-path").value = d; save(); }
    };
    $("#b-token-eye").onclick = () => {
      const t = $("#b-token");
      t.type = t.type === "password" ? "text" : "password";
      $("#b-token-eye").textContent = t.type === "password" ? "显示" : "隐藏";
    };
    $("#b-token-link").onclick = () => api("open_url", { url: "https://huggingface.co/settings/tokens" });
    $("#b-start").onclick = start;
    $("#b-repo").addEventListener("keydown", (e) => { if (e.key === "Enter") start(); });
    $("#b-stop").onclick = () => stopChecked(false);
    $("#b-kill").onclick = () => stopChecked(true);
    $("#b-head-chk").onclick = toggleAllChecked;
    $("#b-hide-done").onchange = () => { B.dirtyOrder = true; B.list.schedule(); };
    $("#b-log-clear").onclick = () => B.log.clear();
    HFD.logToggle("#b-log-toggle");

    B.log = new HFD.LogBox($("#b-log"));
    B.list = new HFD.VList({
      el: $("#b-list"), rowHeight: 30,
      count: () => { if (B.dirtyOrder) reorder(); return B.view.length; },
      renderRow,
      onClick: (i, e) => {
        const id = B.list.idAt(i);
        if (id === undefined) return;
        if (e.target.dataset.ck) { toggleChecked([id]); return; }     // 首列复选框：只切换勾选，不改变选中行
        B.sel.click(id, i, e, B.list.idAt);
        B.list.schedule();
      },
      onDblClick: (i, e) => {          // 双击：已完成 → 打开文件；其他 → 打开文件夹
        if (e.target.dataset.ck) return;
        const r = B.rows.get(B.view[i]);
        if (r) act("basic_action", { action: r[4] === "完成" || r[4] === "已是最新" ? "open" : "folder", ids: [r[0]] });
      },
    });
    B.list.idAt = (i) => B.view[i];
    B.list.setEmpty("还没有下载记录。");
    HFD.menu.attach(B.list, B.sel, HFD.basicMenu);
    B.list.el.addEventListener("keydown", (e) => {
      const ids = B.sel.list();
      if (e.key === " " && ids.length) { e.preventDefault(); toggleChecked(ids); }
      else if (e.key === "Delete" && ids.length) { e.preventDefault(); act("basic_action", { action: "remove", ids }); }
      else if (e.key === "Enter" && ids.length === 1) { e.preventDefault(); act("basic_action", { action: "open", ids }); }
      else if (e.key === "a" && e.ctrlKey) { e.preventDefault(); B.view.forEach((k) => B.sel.set.add(k)); B.list.schedule(); }
    });
    $("#b-clear-done").onclick = () => act("basic_clear", { mode: "done" });
    $("#b-clear-all").onclick = () => act("basic_clear", { mode: "all" });
    const rmBtn = $("#b-remove"), rmDelBtn = $("#b-remove-del");   // 页面还是老版本（没有这两个按钮）时不报错
    if (rmBtn) rmBtn.onclick = () => removeChecked(false);
    if (rmDelBtn) rmDelBtn.onclick = () => removeChecked(true);
  };

  /* ---------------- 勾选（批量操作用） ---------------- */
  function checkedIds() { return Array.from(B.checked); }

  function toggleChecked(ids, value) {
    ids.forEach((id) => {
      if (value === undefined ? !B.checked.has(id) : value) B.checked.add(id);
      else B.checked.delete(id);
    });
    B.list.schedule();
    paintChecked();
  }

  function toggleAllChecked() {
    const all = B.view.length > 0 && B.view.every((k) => B.checked.has(k));
    B.view.forEach((k) => { if (all) B.checked.delete(k); else B.checked.add(k); });
    B.list.schedule();
    paintChecked();
  }

  function setText(el, text) { if (el && el.textContent !== text) el.textContent = text; }

  /* 勾选数量 + 按钮提示（勾选后底部三个按钮只对勾选的文件生效） */
  function paintChecked() {
    const n = B.checked.size;
    const box = $("#b-checked");
    box.hidden = !n;
    setText(box, n ? `已勾选 ${n} 个` : "");
    setText($("#b-hint"), n ? `已勾选 ${n} 个文件：开始下载 = 只下载这些，停止 / 立即中止 = 只停这些`
      : "下载在独立进程中进行，界面不会被下载拖慢");
    const all = n > 0 && B.view.length > 0 && B.view.every((k) => B.checked.has(k));
    $("#b-head-ck").classList.toggle("on", all);
    updateButtons();
  }

  async function start() {
    if (B.checked.size) {                       // 勾选了文件：只下载这些（正在下载时会加入当前任务）
      await api("basic_save", form()).catch(() => {});   // 先把保存路径等设置同步过去
      const r = await act("basic_action", { action: "continue", ids: checkedIds() });
      if (r && r.ok) { B.running = true; updateButtons(); }
      return;
    }
    const f = form();
    $("#b-repo").value = f.repo;
    if (!f.repo) { await HFD.alert("error", "错误", "请输入仓库ID！"); return; }
    const r = await act("basic_start", f);
    if (r && r.ok) { B.running = true; updateButtons(); }
  }

  async function stopChecked(force) {
    const ids = checkedIds();
    if (ids.length) {                           // 只停止勾选的文件
      const msg = force
        ? `立即中止勾选的 ${ids.length} 个文件？\n下载进程会被结束，其他正在下载的文件会自动继续（续传）。`
        : `停止勾选的 ${ids.length} 个文件？\n已下载的部分会保留，右键“继续下载”可以续传。`;
      if (!(await HFD.confirm(force ? "立即中止选中的文件" : "停止选中的文件", msg))) return;
      await act("basic_action", { action: force ? "kill" : "stop", ids });
      return;
    }
    if (force) {                                // 没勾选：和以前一样对整个任务生效
      if (!(await HFD.confirm("立即中止", "立即结束下载进程？\n正在下载的文件会中断，下次点“开始下载”时会自动续传。"))) return;
      act("basic_stop", { force: true });
    } else {
      act("basic_stop", {});
    }
  }

  /* 把“勾选”的文件从列表移除；del=true 表示同时删除硬盘上的文件（后台会再弹一次确认框） */
  async function removeChecked(del) {
    const ids = checkedIds();
    if (!ids.length) { HFD.toast("请先在列表左侧勾选要移除的文件"); return; }
    await act("basic_action", { action: "remove", ids, delete_files: !!del });
  }

  function reorder() {
    B.dirtyOrder = false;
    const hide = $("#b-hide-done").checked;
    const list = [];
    // 进行中的排最前；其他按时间倒序（最近的在上面）
    B.order.forEach((p) => {
      const r = B.rows.get(p);
      if (!r) return;
      if (hide && (r[4] === "完成" || r[4] === "已是最新")) return;
      const rank = RANK[r[4]] ?? 9;
      list.push([rank <= 1 ? rank : 2, -(r[11] || 0), p]);
    });
    list.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    B.view = list.map((x) => x[2]);
    const alive = new Set(B.view);
    B.sel.list().forEach((k) => { if (!alive.has(k)) B.sel.set.delete(k); });
    B.checked.forEach((k) => { if (!B.rows.has(k)) B.checked.delete(k); });    // 记录被移除后，勾选也一起清掉
  }

  /* 鼠标停在“已下载 / 总大小”上时，给出文件总大小的两种算法（HF 网站 / Windows） */
  function sizeTitle(done, sz) {
    if (!sz) return "大小未知";
    return `HF：${HFD.sizeHF(sz)}\nWin：${size(sz)}`;
  }

  function renderRow(i, d) {
    const r = B.rows.get(B.view[i]);
    if (!r) return;
    const [id, path, sz, done, st, bar, speed, avg, err, repo, local] = r;
    const sel = B.sel.has(id);
    let frac = 0, speedTxt = "", stateTxt = st, cls = "t-wait", barCls = "";
    if (st === "下载中") {
      frac = sz ? done / sz : 0;
      speedTxt = bar && speed !== null && speed !== undefined ? size(speed) + "/s" : "…";
      stateTxt = bar ? "下载中" : "准备中";
      cls = "t-active";
    } else if (st === "完成" || st === "已是最新") {
      frac = 1;
      speedTxt = "";                       // 速度列只显示实时速度；平均速度放在悬停提示里
      stateTxt = st === "完成" ? "完成" : "已是最新（跳过）";
      cls = "t-done"; barCls = "done";
    } else if (st === "失败") {
      frac = sz ? done / sz : 0;
      cls = "t-fail"; barCls = "fail";
    } else if (st === "已中断" || st === "已停止") {
      frac = sz ? done / sz : 0;
      stateTxt = st;
      cls = "t-stop";
    }
    frac = Math.max(0, Math.min(1, frac));
    const ck = B.checked.has(id);
    const key = id + "|" + done + "|" + st + "|" + speedTxt + "|" + sz + (sel ? "|s" : "") + (ck ? "|c" : "");
    if (d._key === key) return;            // 内容没变就不重绘
    d._key = key;
    d.className = "vl-row " + cls + (sel ? " sel" : "");
    d.title = `${path}\n仓库：${repo}\n保存到：${local}` + (avg ? `\n平均速度：${size(avg)}/s` : "") +
      (err ? "\n失败原因：" + err : "") + "\n（右键查看更多操作）";
    d.innerHTML =
      `<div class="c"><span class="ck${ck ? " on" : ""}" data-ck="1"></span></div>` +
      `<div>${esc(path)}</div>` +
      `<div class="cell-bar"><div class="pbar ${barCls}"><div class="fill" style="width:${(frac * 100).toFixed(1)}%"></div></div>` +
      `<span>${(frac * 100).toFixed(1)}%</span></div>` +
      `<div class="r num-cell" title="${sizeTitle(done, sz)}">${size(sz ? Math.min(done, sz) : done)} / ${size(sz)}</div>` +
      `<div class="r num-cell">${esc(speedTxt)}</div><div class="c">${esc(stateTxt)}</div>`;
  }

  function updateButtons() {
    $("#b-start").disabled = B.running && !B.checked.size;   // 有勾选时可以随时加进正在进行的任务
    $("#b-stop").disabled = !B.running;
    $("#b-kill").disabled = !B.running;
    // 底部“从列表移除 / 移除并删除本地文件”：只对勾选的文件生效；正在下载或等待中的文件不能移除
    const busy = Array.from(B.checked).some((k) => {
      const r = B.rows.get(k);
      return r && (r[4] === "下载中" || r[4] === "等待中");
    });
    const can = B.checked.size > 0 && !busy;
    const rm = $("#b-remove"), rmd = $("#b-remove-del");      // 老版本页面里没有这两个按钮，跳过
    if (rm) rm.disabled = !can;
    if (rmd) rmd.disabled = !can;
  }

  function setStat(id, text) {
    const el = $(id);
    if (el.textContent !== text) el.textContent = text;
  }

  function renderStats(t, frac) {
    if (!t || !t.started) {
      ["#bs-files", "#bs-size", "#bs-speed", "#bs-eta", "#bs-time"].forEach((id) => setStat(id, "—"));
      setStat("#b-title", "下载进度");
      return;
    }
    setStat("#bs-files", `${t.n_ok} / ${t.n_all}`);
    setStat("#bs-size", `${size(t.done)} / ${size(t.total)}`);
    $("#bs-size").parentElement.title = `总大小：${HFD.size2(t.total)}\n\n${HFD.SIZE_TIP}`;
    setStat("#bs-time", dur(t.elapsed));
    if (t.final) {
      setStat("#bs-speed", `均 ${size(t.avg)}/s`);
      setStat("#bs-eta", t.n_fail ? `失败 ${t.n_fail}` : "已结束");
      setStat("#bs-eta-l", "状态");
    } else {
      setStat("#bs-speed", t.speed !== null && t.speed !== undefined ? `${size(t.speed)}/s` : "计算中…");
      setStat("#bs-eta", t.eta !== null && t.eta !== undefined ? dur(t.eta) : "计算中…");
      setStat("#bs-eta-l", "剩余时间");
    }
    setStat("#b-title", `下载进度 · ${(frac * 100).toFixed(1)}%`);
  }

  function renderTotal(t) {
    const el = $("#b-total"), bar = $("#b-total-bar");
    const frac = t && t.started ? (t.total ? t.done / t.total : (t.n_all ? t.n_ok / t.n_all : 0)) : 0;
    renderStats(t, frac);
    if (!t || !t.started) { el.textContent = "尚未开始下载。"; bar.style.width = "0"; return; }
    const parts = [`总进度：${t.n_ok}/${t.n_all} 个文件`, `${size(t.done)} / ${size(t.total)}（${(frac * 100).toFixed(1)}%）`,
      `总大小 ${HFD.size2(t.total)}`];
    if (t.final) {
      parts.push(`用时 ${dur(t.elapsed)}，平均 ${size(t.avg)}/s`);
    } else {
      parts.push(t.speed !== null && t.speed !== undefined ? `速度 ${size(t.speed)}/s` : "速度 计算中…");
      parts.push(`已用 ${dur(t.elapsed)}`);
      parts.push(t.eta !== null && t.eta !== undefined ? `剩余约 ${dur(t.eta)}` : "剩余时间 计算中…");
      parts.push(`正在下载 ${t.n_active} 个`);
    }
    if (t.n_fail) parts.push(`失败 ${t.n_fail} 个`);
    el.textContent = parts.join("  |  ");
    bar.style.width = (Math.min(1, frac) * 100).toFixed(2) + "%";
    bar.parentElement.classList.toggle("done", !!t.final && !t.n_fail && t.n_ok === t.n_all);
  }

  /* 轮询结果 */
  B.pollArgs = () => ({ struct: B.struct, since: B.rev });
  B.update = function (s) {
    if (!s) return;
    if (s.full) {
      B.struct = s.struct;
      B.rows = new Map();
      B.order = [];
      s.rows.forEach((r) => { B.rows.set(r[0], r); B.order.push(r[0]); });
      B.dirtyOrder = true;
    } else {
      s.rows.forEach((r) => {
        const old = B.rows.get(r[0]);
        if (!old) { B.order.push(r[0]); B.dirtyOrder = true; }
        else if (old[4] !== r[4]) B.dirtyOrder = true;
        B.rows.set(r[0], r);
      });
    }
    const hc = $("#b-hist-count");
    if (hc) hc.textContent = `共 ${B.rows.size} 条记录`;
    B.rev = s.rev;
    const src = $("#b-source");
    if (src && src.textContent !== (s.source || "")) { src.textContent = s.source || ""; src.hidden = !s.source; }
    if (B.running !== s.running) { B.running = s.running; updateButtons(); }
    renderTotal(s.total);
    B.list.setEmpty(s.running ? "正在获取文件列表…" : "还没有下载记录。");
    if (s.rows.length || s.full) B.list.schedule();
    paintChecked();
  };
  B.updateButtons = updateButtons;
})();
