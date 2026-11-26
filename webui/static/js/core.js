/* 公共工具：接口调用、格式化、虚拟列表、日志框、行选择（弹窗在 ui/modal.js，通知在 ui/notice.js） */
"use strict";

const HFD = window.HFD = {};
HFD.key = document.querySelector('meta[name="hfd-key"]').content;
HFD.$ = (sel, root) => (root || document).querySelector(sel);
HFD.$$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

/* ---------------- 接口 ---------------- */
HFD.api = async function (name, body, timeout) {
  const ctrl = typeof AbortController === "function" ? new AbortController() : null;
  let timer = 0;
  if (ctrl && timeout) timer = setTimeout(() => ctrl.abort(), timeout);
  try {
    const r = await fetch("/api/" + name, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-HFD-Key": HFD.key },
      body: JSON.stringify(body || {}),
      signal: ctrl ? ctrl.signal : undefined,
    });
    if (!r.ok) throw new Error("HTTP " + r.status);
    return await r.json();
  } finally {
    if (timer) clearTimeout(timer);
  }
};

/* 调用会改变数据的接口，并统一处理回复：
   {confirm, title, message} → 弹出确认框，确认后带上 confirmed 再调用一次
   {error} / {warn} / {info} → 右下角通知（不打断操作） */
HFD.act = async function (name, body) {
  body = Object.assign({}, body || {});
  const confirmed = body.confirmed = (body.confirmed || []).slice();
  for (let i = 0; i < 8; i++) {
    let r;
    try {
      r = await HFD.api(name, body);
    } catch (e) {
      HFD.notify("error", "连接中断", "无法连接到后台：" + e.message);
      return null;
    }
    if (r.confirm) {
      if (!(await HFD.confirm(r.title, r.message))) return null;
      confirmed.push(r.confirm);
      continue;
    }
    if (r.error) HFD.notify("error", "", r.error);
    else if (r.warn) HFD.notify("warn", "", r.warn);
    else if (r.info) HFD.notify("info", "", r.info);
    HFD.kick && HFD.kick();          // 马上刷新一次数据
    return r;
  }
  return null;
};

/* 原生文件对话框（由后台弹出系统窗口） */
HFD.pick = async function (opts) {
  try {
    const r = await HFD.api("dialog", opts);
    if (r.error) HFD.toast(r.error);
    if (r.info) HFD.toast(r.info);
    return r.result || (opts.kind === "open_multi" ? [] : "");
  } catch (e) {
    HFD.toast("无法打开选择窗口：" + e.message);
    return opts.kind === "open_multi" ? [] : "";
  }
};

/* ---------------- 格式化 ---------------- */
/* 文件大小有两种算法：
   Windows（资源管理器）：1 KB = 1024 字节，1 GB = 1024³ = 1,073,741,824 字节
   Hugging Face 网站：    1 KB = 1000 字节，1 GB = 1000³ = 1,000,000,000 字节
   同一个文件字节数相同，只是换算不同，所以 HF 上的 20.2 GB 在 Windows 里显示约 18.8 GB */
HFD.size = function (n, base) {
  if (n === null || n === undefined) return "未知";
  base = base || 1024;
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  n = Number(n);
  while (Math.abs(n) >= base && i < u.length - 1) { n /= base; i++; }
  return i === 0 ? Math.floor(n) + " B" : n.toFixed(2) + " " + u[i];
};
HFD.sizeHF = (n) => HFD.size(n, 1000);
/* 两种一起显示：HF 20.20 GB ≈ 18.81 GB */
HFD.size2 = function (n) {
  if (n === null || n === undefined) return "未知";
  if (n < 1000) return Math.floor(n) + " B";
  return `HF ${HFD.sizeHF(n)} ≈ ${HFD.size(n)}`;
};
HFD.SIZE_TIP = "文件大小有两种算法（字节数相同，只是换算方式不同）：\n" +
  "· Hugging Face 网站：1 KB = 1000 字节，1 GB = 1000³ = 1,000,000,000 字节（国际单位制）\n" +
  "· Windows 资源管理器：1 KB = 1024 字节，1 GB = 1024³ = 1,073,741,824 字节\n" +
  "所以同一个文件，HF 上显示 20.2 GB，下载后在 Windows 里显示约 18.8 GB（相差约 7%）。\n" +
  "本程序列表里的大小按 Windows 算法显示；“HF xx ≈ yy”中，前面是 HF 网站的数字，后面是 Windows 的数字。";
/* 文件大小说明窗口 */
HFD.sizeHelp = function () {
  const ex = (b) => `<td>${HFD.sizeHF(b)}</td><td>${HFD.size(b)}</td>`;
  const html =
    `<div>同一个文件的<b>字节数是完全一样的</b>，只是把字节换算成 KB / MB / GB 时，用的进位不同：</div>` +
    `<table class="help-tbl" style="margin:10px 0"><tr><td></td><td><b>Hugging Face 网站</b></td><td><b>Windows 资源管理器</b></td></tr>` +
    `<tr><td>1 KB</td><td>1000 字节</td><td>1024 字节</td></tr>` +
    `<tr><td>1 MB</td><td>1000² = 1,000,000 字节</td><td>1024² = 1,048,576 字节</td></tr>` +
    `<tr><td>1 GB</td><td>1000³ = 1,000,000,000 字节</td><td>1024³ = 1,073,741,824 字节</td></tr>` +
    `<tr><td>标准</td><td>国际单位制（硬盘厂商也用这种）</td><td>二进制（严格写法是 KiB / MiB / GiB）</td></tr></table>` +
    `<div>因为 Windows 的 1 GB 更大，所以同一个文件在 Windows 里数字更小，GB 级别大约小 7%：</div>` +
    `<table class="help-tbl" style="margin:10px 0"><tr><td>字节数</td><td><b>HF 显示</b></td><td><b>Windows 显示</b></td></tr>` +
    `<tr><td>20,200,000,000</td>${ex(20.2e9)}</tr><tr><td>5,000,000,000</td>${ex(5e9)}</tr>` +
    `<tr><td>500,000,000</td>${ex(5e8)}</tr></table>` +
    `<div><b>本程序的显示方式：</b></div>` +
    `<div class="muted">· 列表里大字是 Windows 算法（和下载后在资源管理器里看到的一致），下面的小字是 HF 网站算法。<br>` +
    `· 统计中的“HF 20.20 GB ≈ 18.81 GB”：前面是 HF 网站上的数字，后面是 Windows 的数字。<br>` +
    `· 筛选里写的大小（如 &gt;10GB）按 Windows 算法计算；条件标签里同时标出了对应的 HF 数字。<br>` +
    `· 下载后的文件和 HF 上的完全相同，不会因为显示数字不同而少下载任何内容。</div>`;
  return HFD.modal({ title: "文件大小为什么和 HF 网站不一样？", html, size: "info", outside: true });
};
HFD.dur = function (sec) {
  if (sec === null || sec === undefined || sec < 0 || !isFinite(sec)) return "--";
  sec = Math.floor(sec);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  if (h) return h + "小时" + String(m).padStart(2, "0") + "分";
  if (m) return m + "分" + String(s).padStart(2, "0") + "秒";
  return s + "秒";
};
HFD.esc = function (s) {
  return String(s === null || s === undefined ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
};
HFD.debounce = function (fn, ms) {
  let t = null;
  return function (...a) { clearTimeout(t); t = setTimeout(() => fn.apply(this, a), ms); };
};

/* ---------------- 弹窗 ---------------- */
/* size: "info" → 固定尺寸（说明 / 关于作者 / 关于软件 三个窗口一样大）；fill: 内容区撑满（放 iframe 用）
   outside: true → 点窗口外的空白处关闭（只用于查看类窗口；需要做决定的窗口不加，免得误点） */
HFD.modal = function ({ title, level, body, html, buttons, onKey, size, fill, outside }) {
  return new Promise((resolve) => {
    const mask = document.createElement("div");
    mask.className = "mask";
    mask.innerHTML = `<div class="dlg${size ? " dlg-" + size : ""}" role="dialog"><div class="dlg-h"><span class="dot ${level || ""}"></span>` +
      `<span>${HFD.esc(title || "")}</span></div><div class="dlg-b${html ? " form" : ""}${fill ? " fill" : ""}"></div><div class="dlg-f"></div></div>`;
    const b = mask.querySelector(".dlg-b"), f = mask.querySelector(".dlg-f");
    if (html) b.innerHTML = html; else b.textContent = body || "";
    const close = (v) => { document.removeEventListener("keydown", key, true); mask.remove(); resolve(v); };
    (buttons || [{ text: "确定", value: true, primary: true }]).forEach((bt) => {
      const el = document.createElement("button");
      el.textContent = bt.text;
      el.className = bt.primary ? "primary" : bt.left ? "ghost" : "";
      el.onclick = async () => {
        if (bt.click) { const r = await bt.click(mask); if (r === false) return; }
        close(bt.value);
      };
      (bt.left ? (f.querySelector(".left") || f.appendChild(Object.assign(document.createElement("span"), { className: "left" }))) : f)
        .appendChild(el);
    });
    function key(e) {
      if (mask !== HFD.$("#modals").lastElementChild) return;     // 只有最上层的弹窗响应键盘
      if (onKey && onKey(e, mask, close)) return;
      if (e.key === "Escape") { e.stopPropagation(); close(null); }
      if (e.key === "Enter" && e.target.tagName !== "TEXTAREA") {
        const p = f.querySelector("button.primary");
        if (p) { e.preventDefault(); p.click(); }
      }
    }
    document.addEventListener("keydown", key, true);
    if (outside) {          // 按下和松开都在遮罩上才算（在窗口里拖选文字拖到外面不会误关）
      let downOnMask = false;
      mask.addEventListener("mousedown", (e) => { downOnMask = e.target === mask; });
      mask.addEventListener("click", (e) => { if (downOnMask && e.target === mask) close(null); });
    }
    HFD.$("#modals").appendChild(mask);
    const first = mask.querySelector("input, select") || f.querySelector("button.primary");
    if (first) setTimeout(() => first.focus(), 0);
    HFD.modal.onOpen && HFD.modal.onOpen(mask);
  });
};
HFD.confirm = (title, msg) => HFD.modal({
  title, level: "warn", body: msg,
  buttons: [{ text: "取消", value: false }, { text: "确定", value: true, primary: true }],
});
HFD.choose = (title, msg, choices) => HFD.modal({ title, body: msg, buttons: choices });
HFD.prompt = async function (title, msg, value) {
  const html = `<div>${HFD.esc(msg).replace(/\n/g, "<br>")}</div><input class="wide" style="margin-top:8px">`;
  let v = null;
  const ok = await HFD.modal({
    title, html,
    buttons: [{ text: "取消", value: false }, { text: "确定", value: true, primary: true,
      click: (m) => { v = m.querySelector("input").value; } }],
  });
  return ok ? v : null;
};

/* ---------------- 虚拟列表：只绘制看得见的行 ----------------
   opts: { el, rowHeight, count(), renderRow(i, div), onClick(i, e), onDblClick(i, e) } */
HFD.VList = class {
  constructor(opts) {
    Object.assign(this, opts);
    this.rowHeight = opts.rowHeight || 26;
    this.head = this.el.querySelector(".vl-head");
    this.headH = this.head ? this.head.offsetHeight || 28 : 0;
    this.spacer = document.createElement("div");
    this.spacer.style.cssText = "width:1px;pointer-events:none";
    this.el.appendChild(this.spacer);
    this.empty = document.createElement("div");
    this.empty.className = "vl-empty";
    this.el.appendChild(this.empty);
    this.pool = [];
    this.raf = 0;
    this.el.addEventListener("scroll", () => this.schedule(), { passive: true });
    new ResizeObserver(() => this.schedule()).observe(this.el);
    this.el.addEventListener("click", (e) => this._evt(e, this.onClick));
    this.el.addEventListener("dblclick", (e) => this._evt(e, this.onDblClick));
  }
  _evt(e, fn) {
    const row = e.target.closest(".vl-row");
    if (row && fn && row.dataset.i !== undefined) fn(Number(row.dataset.i), e);
  }
  setEmpty(text) { this.emptyText = text; this.schedule(); }
  schedule() {
    if (this.raf) return;
    this.raf = requestAnimationFrame(() => { this.raf = 0; this.render(); });
  }
  scrollTo(i) {
    const top = i * this.rowHeight, view = this.el.clientHeight - this.headH;
    if (top < this.el.scrollTop) this.el.scrollTop = top;
    else if (top + this.rowHeight > this.el.scrollTop + view) this.el.scrollTop = top + this.rowHeight - view;
  }
  render() {
    const n = this.count();
    this.spacer.style.height = n * this.rowHeight + "px";
    this.empty.style.display = n ? "none" : "";
    this.empty.textContent = this.emptyText || "";
    const st = this.el.scrollTop, h = this.el.clientHeight;
    const first = Math.max(0, Math.floor(st / this.rowHeight) - 4);
    const last = Math.min(n, Math.ceil((st + h) / this.rowHeight) + 4);
    const need = last - first;
    while (this.pool.length < need) {
      const d = document.createElement("div");
      d.className = "vl-row";
      this.el.appendChild(d);
      this.pool.push(d);
    }
    for (let k = 0; k < this.pool.length; k++) {
      const d = this.pool[k];
      if (k >= need) { d.style.display = "none"; d.dataset.i = ""; continue; }
      const i = first + k;
      d.style.display = "";
      d.style.top = this.headH + i * this.rowHeight + "px";
      d.dataset.i = i;
      this.renderRow(i, d);
    }
  }
};

/* ---------------- 日志框：追加文字，限制长度，在底部时自动滚动 ---------------- */
HFD.LogBox = class {
  constructor(el, maxChars) {
    this.el = el;
    this.max = maxChars || 400000;
  }
  append(text) {
    if (!text) return;
    const el = this.el;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 30;
    el.appendChild(document.createTextNode(text));
    if (el.childNodes.length > 400) {          // 合并文本节点，控制总长度
      let all = el.textContent;
      if (all.length > this.max) all = all.slice(all.length - Math.floor(this.max * 0.8));
      el.textContent = all;
    }
    if (atBottom) el.scrollTop = el.scrollHeight;
  }
  clear() { this.el.textContent = ""; }
};

/* 日志框“收起/展开”（默认收起；点过“展开”就记住，下次仍是展开） */
HFD.logToggle = function (btnSel) {
  const btn = HFD.$(btnSel), box = btn.closest(".logbox"), key = "hfd-log2-" + btnSel;
  const apply = (c) => { box.classList.toggle("collapsed", c); btn.textContent = c ? "展开" : "收起"; };
  let c = true;
  try { const v = localStorage.getItem(key); if (v !== null) c = v === "1"; } catch (e) { /* 忽略 */ }
  apply(c);
  btn.onclick = () => {
    c = !box.classList.contains("collapsed");
    apply(c);
    try { localStorage.setItem(key, c ? "1" : "0"); } catch (e) { /* 忽略 */ }
    window.dispatchEvent(new Event("resize"));
  };
};

/* ---------------- 行选择（单击 / Ctrl / Shift） ---------------- */
HFD.Selection = class {
  constructor() { this.set = new Set(); this.anchor = null; }
  click(id, idx, e, idOf) {
    if (e.shiftKey && this.anchor !== null) {
      const a = Math.min(this.anchor, idx), b = Math.max(this.anchor, idx);
      if (!e.ctrlKey) this.set.clear();
      for (let i = a; i <= b; i++) { const v = idOf(i); if (v !== undefined) this.set.add(v); }
    } else if (e.ctrlKey) {
      this.set.has(id) ? this.set.delete(id) : this.set.add(id);
      this.anchor = idx;
    } else {
      this.set.clear();
      this.set.add(id);
      this.anchor = idx;
    }
  }
  has(id) { return this.set.has(id); }
  list() { return Array.from(this.set); }
  clear() { this.set.clear(); this.anchor = null; }
};

/* 复制到剪贴板 */
HFD.copy = async function (text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch (e) {
    const t = Object.assign(document.createElement("textarea"), { value: text });
    document.body.appendChild(t);
    t.select();
    document.execCommand("copy");
    t.remove();
  }
  HFD.toast("已复制：" + text);
};
