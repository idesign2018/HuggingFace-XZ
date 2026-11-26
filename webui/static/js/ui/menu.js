/* 通用右键菜单：菜单本体、属性窗口、复制、带确认的接口调用
   （“常规”页和“批量”页各自的菜单在 basic/menu.js、batch/menu.js） */
"use strict";
(function () {
  const { $, act, esc } = HFD;
  let cur = null;

  function close() {
    if (cur) { cur.remove(); cur = null; }
    document.removeEventListener("mousedown", outside, true);
    document.removeEventListener("keydown", onKey, true);
    window.removeEventListener("blur", close);
    window.removeEventListener("resize", close);
  }
  function outside(e) { if (cur && !cur.contains(e.target)) close(); }
  function onKey(e) {
    if (!cur) return;
    const items = Array.from(cur.querySelectorAll(".mi:not(.dis)"));
    const i = items.indexOf(document.activeElement);
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(); }
    else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const n = items.length;
      if (n) items[(i + (e.key === "ArrowDown" ? 1 : n - 1) + (i < 0 ? (e.key === "ArrowDown" ? 0 : 1) : 0)) % n].focus();
    } else if (e.key === "Enter" && i >= 0) { e.preventDefault(); items[i].click(); }
  }

  /* items: [{text, key, disabled, danger, click}] 或 "-"（分隔线）。x, y：屏幕位置
     opts.compact：只有几个短文字的菜单（导航栏“更多”），窄一点、三项一样高 */
  HFD.menu = function (items, x, y, opts) {
    close();
    const m = document.createElement("div");
    m.className = "ctx" + (opts && opts.compact ? " compact" : "");
    m.setAttribute("role", "menu");
    items.forEach((it) => {
      if (it === "-") { m.appendChild(Object.assign(document.createElement("div"), { className: "sep" })); return; }
      const b = document.createElement("button");
      b.className = "mi" + (it.disabled ? " dis" : "") + (it.danger ? " danger" : "");
      b.innerHTML = `<span>${esc(it.text)}</span>${it.key ? `<kbd>${esc(it.key)}</kbd>` : ""}`;
      b.tabIndex = -1;
      if (it.disabled) b.disabled = true;
      b.onclick = () => { close(); if (!it.disabled) it.click(); };
      m.appendChild(b);
    });
    document.body.appendChild(m);
    const w = m.offsetWidth, h = m.offsetHeight;
    m.style.left = Math.max(4, Math.min(x, innerWidth - w - 6)) + "px";
    m.style.top = Math.max(4, y + h > innerHeight - 6 ? y - h : y) + "px";
    cur = m;
    setTimeout(() => {
      document.addEventListener("mousedown", outside, true);
      document.addEventListener("keydown", onKey, true);
      window.addEventListener("blur", close);
      window.addEventListener("resize", close);
    }, 0);
  };
  HFD.menu.close = close;

  /* 给虚拟列表挂上右键菜单：右键的行如果没被选中，先只选中它（和资源管理器一样） */
  HFD.menu.attach = function (list, sel, build) {
    const open = (i, x, y) => {
      const id = list.idAt(i);
      if (id !== undefined && !sel.has(id)) { sel.click(id, i, {}, list.idAt); list.schedule(); }
      const ids = sel.list();
      if (ids.length) HFD.menu(build(ids), x, y);
    };
    list.el.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      const row = e.target.closest(".vl-row");
      if (row && row.dataset.i !== "") open(Number(row.dataset.i), e.clientX, e.clientY);
    });
    list.el.addEventListener("click", (e) => {            // 行尾“⋯”按钮
      const btn = e.target.closest("[data-more]");
      if (!btn) return;
      e.stopPropagation();
      const r = btn.getBoundingClientRect();
      open(Number(btn.closest(".vl-row").dataset.i), r.left - 150, r.bottom + 2);
    }, true);
    list.el.addEventListener("keydown", (e) => {            // 键盘：菜单键 或 Shift+F10
      if (e.key === "ContextMenu" || (e.key === "F10" && e.shiftKey)) {
        e.preventDefault();
        const i = sel.anchor === null ? 0 : sel.anchor;
        const row = list.pool.find((d) => d.dataset.i === String(i));
        const r = (row || list.el).getBoundingClientRect();
        open(i, r.left + 40, r.top + 20);
      }
    });
  };

  /* 属性窗口 */
  HFD.showProps = function (props) {
    if (!props || !props.length) return;
    const html = `<table class="help-tbl props">${props[0].map(([k, v]) =>
      `<tr><td>${esc(k)}</td><td>${esc(v)}</td></tr>`).join("")}</table>`;
    HFD.modal({ title: "属性", html, outside: true });
  };

  /* 和 HFD.act 一样，但确认框带“不再提示”复选框（回复中带 dont_ask 时） */
  HFD.actConfirm = async function (name, body) {
    body = Object.assign({}, body);
    const confirmed = body.confirmed = (body.confirmed || []).slice();
    for (let i = 0; i < 8; i++) {
      let r;
      try { r = await HFD.api(name, body); } catch (e) {
        HFD.notify("error", "连接中断", "无法连接到后台：" + e.message);
        return null;
      }
      if (r.confirm) {
        const res = await HFD.confirmDontAsk(r.title, r.message, !!r.dont_ask);
        if (!res.ok) return null;
        if (res.dontAsk) {
          body.dont_ask = true;
          const sw = document.querySelector('[data-s="skip_basic_confirm"]');
          if (sw) sw.checked = true;
        }
        confirmed.push(r.confirm);
        continue;
      }
      if (r.error) HFD.notify("error", "出错", r.error);
      else if (r.warn) HFD.notify("warn", "提示", r.warn);
      else if (r.info) HFD.notify("info", "", r.info);
      HFD.kick && HFD.kick();
      return r;
    }
    return null;
  };

  HFD.confirmDontAsk = async function (title, msg, withBox) {
    let dontAsk = false;
    const box = withBox ? `<label class="dont-ask"><input type="checkbox" id="dlg-dont-ask">不再提示` +
      `<span class="muted">（以后直接开始；可在下载按钮下方的“用常规下载时不再确认”开关恢复）</span></label>` : "";
    const ok = await HFD.modal({
      title, level: "warn", html: `<div class="pre">${esc(msg)}</div>${box}`,
      buttons: [
        { text: "取消", value: false },
        { text: "确定", value: true, primary: true, click: (m) => {
          const c = m.querySelector("#dlg-dont-ask");
          dontAsk = !!(c && c.checked);
        } },
      ],
    });
    return { ok: !!ok, dontAsk };
  };
})();
