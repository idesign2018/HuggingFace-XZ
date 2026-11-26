/* 启动：切换页面、初始化、定时轮询（一次请求取回日志、提示、两个页面的变化）

   保持在线：后台不会因为“太久没联系”而退出，这里做三件事保证一直连得上
     · 0.5 秒轮询一次；切回窗口、页面重新可见时立刻补一次
     · 后台标签页的定时器会被浏览器降频，所以再用一个后台线程（Worker）兜底
     · 关闭窗口时用 sendBeacon 通知后台（刷新页面时后台会等几秒确认，不会误退） */
"use strict";
(function () {
  const { $, $$, api } = HFD;
  const POLL_MS = 500;
  const POLL_TIMEOUT = 15000;          // 单次轮询最长等待时间，卡住时不会把界面一起拖死
  let seq = 0, polling = false, failures = 0;

  HFD.switchTab = (name) => switchTab(name);
  function switchTab(name) {
    $$(".nav[data-tab]").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
    $$(".page").forEach((p) => p.classList.toggle("active", p.id === "page-" + name));
    try { localStorage.setItem("hfd-tab", name); } catch (e) { /* 忽略 */ }
    if (name === "batch") HFD.batch.list.schedule(); else HFD.basic.list.schedule();
  }

  function setConn(ok) {
    const d = $("#conn-dot");
    if (!d) return;
    d.classList.toggle("off", !ok);
    d.title = ok ? "已连接后台" : "与后台的连接已断开（程序可能已关闭）";
  }

  /* 后台的提示（下载完成、出错等）→ 右下角通知，不弹窗、不打断操作 */
  function showNotices(list) {
    list.forEach((n) => {
      if (!n || n.length < 5) return;
      const title = (n[3] || "") + (n[1] === "batch" ? "（批量下载）" : "");
      HFD.notify(n[2], title, n[4]);
    });
  }

  async function poll() {
    if (polling) return;
    polling = true;
    try {
      const r = await api("poll", { seq, basic: HFD.basic.pollArgs(), batch: HFD.batch.pollArgs() }, POLL_TIMEOUT);
      failures = 0;
      setConn(true);
      seq = r.seq;
      if (r.logs.basic) HFD.basic.log.append(r.logs.basic);
      if (r.logs.batch) HFD.batch.log.append(r.logs.batch);
      HFD.basic.update(r.basic);
      HFD.batch.update(r.batch);
      if (r.notices && r.notices.length) showNotices(r.notices);
    } catch (e) {
      failures++;
      if (failures >= 3) setConn(false);
    } finally {
      polling = false;
    }
  }

  /* 操作完成后马上刷新一次（不用等下一次定时轮询） */
  HFD.kick = () => setTimeout(poll, 30);

  /* 后台标签页里浏览器的定时器会被降频到每分钟一次；用 Worker 兜底多联系几次 */
  function startHeartbeat() {
    try {
      const src = "setInterval(function(){postMessage(1)},4000)";
      const url = URL.createObjectURL(new Blob([src], { type: "text/javascript" }));
      const w = new Worker(url);
      w.onmessage = () => poll();
    } catch (e) { /* 浏览器不支持时忽略：页面自己的定时器仍然有效 */ }
  }

  /* 关闭窗口时通知后台（刷新页面时，网页会在几秒内重新连上，后台会因此不退出） */
  function sayBye() {
    const body = "{}";
    try {
      if (navigator.sendBeacon) {
        navigator.sendBeacon("/api/bye?k=" + encodeURIComponent(HFD.key), body);
        return;
      }
    } catch (e) { /* 走下面的 fetch */ }
    try {
      fetch("/api/bye", {
        method: "POST", keepalive: true,
        headers: { "Content-Type": "application/json", "X-HFD-Key": HFD.key },
        body: body,
      });
    } catch (e) { /* 忽略 */ }
  }

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  async function boot() {
    let data = null;
    for (let i = 0; i < 40 && !data; i++) {
      try { data = await api("init", {}, 8000); } catch (e) { await sleep(Math.min(300 + i * 150, 2000)); }
    }
    if (!data) {                 // 后台还没起来：等一会儿再试，不留下一个“死”界面
      setConn(false);
      setTimeout(boot, 3000);
      return;
    }
    HFD.basic.init(data.basic);
    HFD.batch.init(data);
    if (data.about && data.about.version) {          // 左下角 Logo 的提示里带上版本号
      const logo = $(".logo");
      if (logo) logo.title = `${data.about.name} v${data.about.version}（${data.about.build || ""}）`;
    }
    $$(".nav[data-tab]").forEach((t) => { t.onclick = () => switchTab(t.dataset.tab); });
    $("#nav-token").onclick = () => api("open_url", { url: "https://huggingface.co/settings/tokens" });
    $("#nav-more").onclick = () => HFD.moreMenu($("#nav-more"));
    $("#nav-src").onclick = () => HFD.sourceMenu($("#nav-src"));
    HFD.setSourceUI(data.source);
    let tab = "basic";
    try { tab = location.hash.slice(1) || localStorage.getItem("hfd-tab") || "basic"; } catch (e) { /* 忽略 */ }
    switchTab(tab === "batch" ? "batch" : "basic");
    await poll();
    setInterval(poll, POLL_MS);
    startHeartbeat();
    document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
    window.addEventListener("focus", () => poll());
  }

  // 关闭窗口前：有任务在进行时提醒
  window.addEventListener("beforeunload", (e) => {
    if (HFD.basic.running || HFD.batch.busy) { e.preventDefault(); e.returnValue = ""; }
  });
  window.addEventListener("pagehide", sayBye);

  boot();
})();
