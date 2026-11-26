/* 页面 2：批量下载 —— 数据、文件清单（虚拟列表）、仓库列表（第二栏）、设置同步 */
"use strict";
(function () {
  const { $, $$, api, act, size, esc } = HFD;
  const T = HFD.batch = {
    rev: -1, struct: -1, items: [], byId: new Map(), repos: [], repoSel: new Set(), view: "",
    visible: [], dirtyView: true, sel: new HFD.Selection(), busy: false, aria2: false,
  };
  /* 行数据：[iid, repo_key, hf, size, checked, local, status, tag, exists, conflict] */
  const I = { id: 0, key: 1, hf: 2, size: 3, chk: 4, local: 5, status: 6, tag: 7, exists: 8, conflict: 9 };
  T.I = I;
  const COLORS = ["#4f7cff", "#07c160", "#ff8a3d", "#9b6bff", "#e5484d", "#14b8c4", "#d9a404", "#6b7a90"];

  /* ---------------- 设置（data-s / data-f 输入框自动同步到后台） ---------------- */
  T.readSettings = function () {
    const s = {};
    $$("[data-s]").forEach((el) => {
      const k = el.dataset.s;
      if (el.type === "checkbox") s[k] = el.checked;
      else if (el.type === "radio") { if (el.checked) s[k] = el.value; }
      else if (el.type === "number") s[k] = Number(el.value) || 0;
      else s[k] = el.value;
    });
    return s;
  };
  T.readFilters = () => ({ q: $("#t-q").value });
  T.writeSettings = function (s, f) {
    $$("[data-s]").forEach((el) => {
      const k = el.dataset.s;
      if (!(k in s)) return;
      if (el.type === "checkbox") el.checked = !!s[k];
      else if (el.type === "radio") el.checked = el.value === s[k];
      else el.value = s[k];
    });
    if (f && "q" in f) $("#t-q").value = f.q;
  };
  T.syncSettings = HFD.debounce(async function () {
    try {
      const r = await api("batch_settings", { settings: T.readSettings(), filters: T.readFilters(), save: true });
      if (r && r.target) $("#t-target").textContent = r.target;
    } catch (e) { /* 忽略，下次轮询会显示连接状态 */ }
  }, 400);

  /* 实时勾选：输入筛选条件时（停顿 0.4 秒后）自动“只勾选符合的”；条件写错时不改动勾选 */
  T.liveApply = HFD.debounce(async function () {
    if (!T.readSettings().live_filter || !T.items.length) return;
    try {
      await api("batch_filter", { mode: "set", filters: T.readFilters(), view: T.view, live: true });
      HFD.kick && HFD.kick();
    } catch (e) { /* 忽略 */ }
  }, 400);

  /* 筛选预览：识别出的条件显示成小标签 + 符合条件的数量 */
  T.updatePreview = HFD.debounce(async function () {
    try {
      const r = await api("batch_preview", { filters: T.readFilters(), view: T.view });
      const el = $("#t-preview");
      el.textContent = r.text || "";
      el.className = r.level === "warn" ? "warn" : r.level === "muted" ? "muted" : "info";
      $("#t-chips").innerHTML = (r.chips || []).map(([k, v]) =>
        `<span class="chip${k === "排除" ? " ex" : ""}"><b>${esc(k)}</b>${esc(v)}</span>`).join("");
    } catch (e) { /* 忽略 */ }
  }, 250);

  /* ---------------- 文件清单 ---------------- */
  function rebuildVisible() {
    T.dirtyView = false;
    T.visible = T.view ? T.items.filter((r) => r[I.key] === T.view) : T.items;
  }
  T.idAt = (i) => { const r = T.visible[i]; return r ? r[I.id] : undefined; };

  function renderRow(i, d) {
    const r = T.visible[i];
    if (!r) return;
    const repo = T.repoName.get(r[I.key]) || "";
    const sel = T.sel.has(r[I.id]);
    const key = r.join("\u0001") + (sel ? "|s" : "") + repo;
    if (d._key === key) return;
    d._key = key;
    d.className = "vl-row" + (r[I.tag] ? " t-" + r[I.tag] : "") + (sel ? " sel" : "");
    d.title = `${repo}\nHF 路径：${r[I.hf]}\n保存到：${r[I.local]}${r[I.status] ? "\n状态：" + r[I.status] : ""}\n（右键查看更多操作）`;
    d.innerHTML =
      `<div class="c"><span class="ck${r[I.chk] ? " on" : ""}" data-ck="1"></span></div>` +
      `<div>${esc(repo)}</div><div>${esc(r[I.hf])}</div>` +
      `<div class="r num-cell" title="${r[I.size] == null ? "大小未知" : `HF：${esc(HFD.sizeHF(r[I.size]))}\nWin：${esc(size(r[I.size]))}`}">${size(r[I.size])}` +
      `${r[I.size] >= 1000 ? `<small>${esc(HFD.sizeHF(r[I.size]))}</small>` : ""}</div>` +
      `<div>${esc(r[I.local])}</div><div>${esc(r[I.status])}<button class="more" data-more="1" title="更多操作">⋯</button></div>`;
  }

  T.initList = function () {
    T.list = new HFD.VList({
      el: $("#t-list"), rowHeight: 30,
      count: () => { if (T.dirtyView) rebuildVisible(); return T.visible.length; },
      renderRow,
      onClick: (i, e) => {
        const id = T.idAt(i);
        if (id === undefined) return;
        if (e.target.dataset.ck) {              // 点“下载”列：切换勾选
          setLocalCheck([id], null);
          act("batch_check", { op: "toggle", iids: [id] });
          return;
        }
        T.sel.click(id, i, e, T.idAt);
        T.list.schedule();
      },
      onDblClick: (i, e) => {
        if (e.target.dataset.ck) return;
        const id = T.idAt(i);
        if (id !== undefined) T.editPath(id);
      },
    });
    T.list.setEmpty("尚未添加仓库。在左侧填写仓库ID，点“添加并获取”。");
    T.list.idAt = T.idAt;
    HFD.menu.attach(T.list, T.sel, HFD.batchMenu);         // 右键菜单
    const el = $("#t-list");
    el.addEventListener("keydown", (e) => {
      if (e.key === " ") {
        e.preventDefault();
        const ids = T.sel.list();
        if (ids.length) { setLocalCheck(ids, null); act("batch_check", { op: "toggle", iids: ids }); }
      } else if (e.key === "a" && e.ctrlKey) {
        e.preventDefault();
        T.visible.forEach((r) => T.sel.set.add(r[I.id]));
        T.list.schedule();
      } else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        const cur = T.sel.anchor === null ? -1 : T.sel.anchor;
        const n = Math.max(0, Math.min(T.visible.length - 1, cur + (e.key === "ArrowDown" ? 1 : -1)));
        const id = T.idAt(n);
        if (id !== undefined) { T.sel.click(id, n, e, T.idAt); T.list.scrollTo(n); T.list.schedule(); }
      }
    });
  };

  /* 先在本地改勾选（立刻有反应），后台确认后以后台数据为准 */
  function setLocalCheck(ids, value) {
    ids.forEach((id) => {
      const r = T.byId.get(id);
      if (r) r[I.chk] = value === null ? !r[I.chk] : value;
    });
    T.list.schedule();
  }

  /* ---------------- 仓库列表（第二栏） ---------------- */
  T.repoName = new Map();
  function colorOf(s) {
    let h = 0;
    for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
    return COLORS[h % COLORS.length];
  }
  function renderRepos(stats) {
    const box = $("#t-repos");
    if (!T.repos.length) {
      const html = '<div class="side-empty">还没有仓库<br>在上方填写仓库ID，点“添加并获取”</div>';
      if (box._html !== html) { box.innerHTML = html; box._html = html; }
      return;
    }
    const html = T.repos.map((m) => {
      const [k, repo, type, rev, access, needs, fetched, kind] = m;
      const s = stats.get(k) || [0, 0, 0];
      const isImp = kind === "import";
      const short = repo.replace(/^导入：/, "").split("/").pop() || repo;
      const letter = esc((short.match(/[A-Za-z0-9\u4e00-\u9fff]/) || ["?"])[0].toUpperCase());
      const desc = !fetched ? "未获取文件列表 · 双击获取"
        : `${s[1]}/${s[0]} 个已勾选 · ${isImp && !s[2] ? "大小未知" : HFD.size2(s[2])}`;
      const tagCls = !fetched ? "off" : needs ? "warn" : "";
      const cls = "repo" + (fetched ? "" : " off");
      const title = `${repo}\n${isImp ? "导入的链接" : `${type} @ ${rev}`}\n${access}\n` +
        `单击：查看这个仓库的文件（Ctrl 多选）\n双击：重新获取这个仓库的文件列表（同“刷新选中”）`;
      return `<div class="${cls}" data-k="${esc(k)}" title="${esc(title)}">` +
        `<div class="avatar" style="background:${colorOf(repo)}">${letter}</div>` +
        `<div class="meta"><div class="nm">${esc(repo)}</div><div class="ds">${esc(desc)}</div></div>` +
        `<span class="tag ${tagCls}">${esc(isImp ? "导入" : access.replace("（点刷新）", ""))}</span></div>`;
    }).join("");
    if (box._html !== html) { box.innerHTML = html; box._html = html; }
    markRepos();
  }
  /* 选中/正在查看 只改样式，不重建卡片（否则双击时第一下单击会把卡片换掉） */
  function markRepos() {
    $$("#t-repos .repo[data-k]").forEach((el) => {
      el.classList.toggle("sel", T.repoSel.has(el.dataset.k));
      el.classList.toggle("viewing", T.view === el.dataset.k);
    });
  }
  T.initRepos = function () {
    const box = $("#t-repos");
    /* 单击：选中并查看这个仓库的文件；Ctrl+单击：多选（用于“刷新选中”“移除”），不切换查看 */
    box.addEventListener("click", (e) => {
      const el = e.target.closest(".repo[data-k]");
      if (!el) return;
      const k = el.dataset.k;
      if (e.ctrlKey) {
        T.repoSel.has(k) ? T.repoSel.delete(k) : T.repoSel.add(k);
        markRepos();
        return;
      }
      T.repoSel.clear();
      T.repoSel.add(k);
      if (T.view !== k) T.setView(k); else markRepos();
    });
    /* 双击：重新获取这个仓库的文件列表（和“刷新选中”相同，已获取过的会先确认） */
    box.addEventListener("dblclick", (e) => {
      const el = e.target.closest(".repo[data-k]");
      if (!el) return;
      const k = el.dataset.k;
      const m = T.repos.find((x) => x[0] === k);
      if (m && m[7] === "import") { HFD.toast("导入的链接不需要刷新。"); return; }
      T.repoSel.clear();
      T.repoSel.add(k);
      if (T.view !== k) T.setView(k);
      act("batch_refresh", { keys: [k] });
    });
  };

  T.setView = function (k) {
    T.view = k || "";
    $("#t-view").value = T.view;
    T.sel.clear();
    rebuildVisible();        // 先更新可见列表，下面的统计才是这个仓库的
    T.list.el.classList.toggle("one-repo", !!T.view);    // 只看一个仓库时隐藏“仓库”列，给路径留出空间
    T.list.el.scrollTop = 0;
    T.list.schedule();
    T.refreshSummary(false);
    T.updatePreview();
  };

  /* 统计：仓库列表、标题、汇总文字、显示下拉框 */
  T.refreshSummary = function (reposOnly) {
    const stats = new Map();
    let chk = 0, chkSize = 0, exist = 0, conf = 0;
    T.items.forEach((r) => {
      let s = stats.get(r[I.key]);
      if (!s) stats.set(r[I.key], (s = [0, 0, 0]));
      s[0]++;
      if (r[I.chk]) {
        s[1]++; s[2] += r[I.size] || 0;
        chk++; chkSize += r[I.size] || 0;
        if (r[I.exists]) exist++;
        if (r[I.conflict]) conf++;
      }
    });
    renderRepos(stats);
    if (reposOnly) return;
    if (T.dirtyView) rebuildVisible();
    $("#t-title").textContent = T.view ? (T.repoName.get(T.view) || "全部仓库") : "全部仓库";
    const sum = $("#t-summary");
    if (!T.repos.length) {
      sum.textContent = "尚未添加仓库。";
      sum.className = "h-sub";
    } else {
      // 只统计当前查看的仓库（选“全部仓库”时统计全部）
      let n = 0, vchk = 0, vsize = 0, vall = 0, vexist = 0, vconf = 0;
      T.visible.forEach((r) => {
        n++; vall += r[I.size] || 0;
        if (r[I.chk]) {
          vchk++; vsize += r[I.size] || 0;
          if (r[I.exists]) vexist++;
          if (r[I.conflict]) vconf++;
        }
      });
      let t = (T.view ? "" : `${T.repos.length} 个仓库 · `) +
        `${n} 个文件，共 ${HFD.size2(vall)} · 已勾选 ${vchk} 个，${HFD.size2(vsize)}`;
      if (vexist) t += ` · 本地已存在 ${vexist} 个`;
      if (vconf) t += ` · 路径冲突 ${vconf} 个（将被跳过）`;
      sum.textContent = t;
      sum.className = "h-sub" + (vconf ? " warn" : "");
      sum.title = HFD.SIZE_TIP;
    }
    // 下载栏：下载时会下载“所有仓库”中已勾选的文件，这里显示全部的合计
    const tot = $("#t-dl-total");
    if (tot) {
      tot.textContent = chk ? `将下载全部仓库中已勾选的 ${chk} 个文件，${HFD.size2(chkSize)}` +
        (exist ? `（其中本地已存在 ${exist} 个）` : "") + (conf ? `；路径冲突 ${conf} 个将被跳过` : "") : "没有勾选任何文件";
      tot.className = "dl-total" + (conf ? " warn" : "");
      tot.title = HFD.SIZE_TIP;
    }
    const sel = $("#t-view");
    const opts = ['<option value="">全部仓库</option>'].concat(T.repos.map((m) => {
      const label = m[7] === "import" ? `${m[1]}  [导入]` : `${m[1]}  [${m[2]}@${m[3]}]`;
      return `<option value="${esc(m[0])}">${esc(label)}</option>`;
    })).join("");
    if (sel._opts !== opts) { sel.innerHTML = opts; sel._opts = opts; }
    if (T.view && !T.repos.some((m) => m[0] === T.view)) { T.setView(""); return; }
    sel.value = T.view;
  };

  /* ---------------- 轮询结果 ---------------- */
  T.pollArgs = () => ({ rev: T.rev, struct: T.struct });
  T.update = function (s) {
    if (!s || s.same) return;
    if (s.full) {
      T.items = s.items;
      T.byId = new Map(T.items.map((r) => [r[I.id], r]));
      T.dirtyView = true;
      const alive = new Set(T.items.map((r) => r[I.id]));
      T.sel.list().forEach((id) => { if (!alive.has(id)) T.sel.set.delete(id); });
    } else {
      s.items.forEach((r) => {
        const old = T.byId.get(r[I.id]);
        if (old) { for (let k = 0; k < r.length; k++) old[k] = r[k]; }
      });
    }
    T.repos = s.repos;
    T.repoName = new Map(T.repos.map((m) => [m[0], m[1]]));
    const alive = new Set(T.repos.map((m) => m[0]));
    Array.from(T.repoSel).forEach((k) => { if (!alive.has(k)) T.repoSel.delete(k); });
    T.rev = s.rev;
    T.struct = s.struct;
    if (s.target) $("#t-target").textContent = s.target;
    T.setBusy(s.busy, s.aria2_running);
    if (T.dirtyView) rebuildVisible();
    T.list.setEmpty(T.repos.length ? (T.view ? "这个仓库没有文件。" : "还没有获取文件列表，点左下角“刷新全部”。")
      : "尚未添加仓库。在左侧填写仓库ID，点“添加并获取”。");
    T.list.schedule();
    T.refreshSummary(false);
    if (s.full) T.updatePreview();
  };

  T.setBusy = function (busy, aria2) {
    if (T.busy === busy && T.aria2 === aria2) return;
    T.busy = busy;
    T.aria2 = aria2;
    $$("#page-batch button.act").forEach((b) => { b.disabled = busy; });
    const el = $("#t-busy");
    el.hidden = !(busy || aria2);
    el.textContent = busy ? "正在处理…（可点“停止”）" : "aria2 正在下载（进度见黑色窗口）";
  };
})();
