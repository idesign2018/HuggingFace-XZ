/* 右下角通知：不打断操作的小卡片（下载完成、出错、提示都走这里）

   用法：
     HFD.notify(level, title, msg[, ms])   level: ok / info / warn / error
     HFD.toast(msg)                        一行字的轻提示
     HFD.alert(level, title, msg)          兼容旧写法，同样不弹窗

   要点：
     · 不会挡住界面，也不用点“确定”，几秒后自己消失（鼠标停在上面时不消失）
     · 同样的消息连续出现时不会刷屏，只在卡片上计数
     · 内容长的时候点一下卡片可以展开，全文也能在“运行日志”里看到 */
"use strict";
(function () {
  const MAX = 4;                                  // 同时最多显示几张卡片
  const ICON = { ok: "✓", info: "i", warn: "!", error: "×" };
  const TIME = { ok: 4000, info: 4500, warn: 8000, error: 10000 };
  let last = { key: "", t: 0, el: null, n: 1 };

  function host() {
    let el = document.getElementById("toasts");
    if (!el) {
      el = document.createElement("div");
      el.id = "toasts";
      el.className = "toasts";
      document.body.appendChild(el);
    }
    return el;
  }

  function drop(el) {
    if (!el || !el.parentNode) return;
    el.classList.add("out");
    setTimeout(() => el.remove(), 220);
  }

  HFD.notify = function (level, title, msg, ms) {
    level = String(level || "info");
    if (level === "success") level = "ok";
    if (!ICON[level]) level = "info";
    title = String(title == null ? "" : title);
    msg = String(msg == null ? "" : msg);
    if (!title && !msg) return null;

    const now = Date.now();
    const key = level + "|" + title + "|" + msg;
    if (key === last.key && now - last.t < 4000 && last.el && last.el.isConnected) {
      // 连续相同的消息：不重复弹出，只在原卡片上计数
      last.n += 1;
      last.t = now;
      const badge = last.el.querySelector(".nt-badge");
      if (badge) { badge.textContent = "×" + last.n; badge.hidden = false; }
      last.el.classList.remove("bump");
      void last.el.offsetWidth;
      last.el.classList.add("bump");
      return last.el;
    }

    const box = host();
    const el = document.createElement("div");
    el.className = "nt nt-" + level;
    el.setAttribute("role", "status");
    el.innerHTML =
      `<span class="nt-ico">${ICON[level]}</span>` +
      `<div class="nt-main">` +
      (title ? `<div class="nt-title">${HFD.esc(title)}<span class="nt-badge" hidden></span></div>` : "") +
      (msg ? `<div class="nt-msg">${HFD.esc(msg)}</div>` : "") +
      `</div><button class="nt-x" type="button" title="关闭">×</button>`;

    box.appendChild(el);
    while (box.children.length > MAX) drop(box.firstElementChild);

    let timer = 0;
    const close = () => { clearTimeout(timer); drop(el); };
    const wait = () => {
      clearTimeout(timer);
      timer = setTimeout(close, ms || TIME[level]);
    };
    el.querySelector(".nt-x").onclick = (e) => { e.stopPropagation(); close(); };
    el.onclick = () => el.classList.toggle("open");     // 内容长：点一下展开/收起
    el.onmouseenter = () => clearTimeout(timer);
    el.onmouseleave = wait;
    wait();

    last = { key: key, t: now, el: el, n: 1 };
    return el;
  };

  /* 一行字的轻提示 */
  HFD.toast = (msg, ms) => HFD.notify("info", "", msg, ms || 3200);

  /* 旧写法：以前是模态弹窗，现在统一走通知，不会打断操作 */
  HFD.alert = (level, title, msg) => HFD.notify(level, title, msg);
})();
