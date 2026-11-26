/* 左侧导航栏的“源”按钮：国内镜像 / 官方，两个页面共用一个设置
     国内镜像：直连，不走代理
     官方：自动走代理（系统代理开着就用它；没开就用系统代理设置里登记过的地址）*/
"use strict";
(function () {
  const { $, api } = HFD;
  const NAME = { mirror: "国内镜像 hf-mirror.com", official: "官方 huggingface.co" };
  HFD.source = "mirror";

  HFD.setSourceUI = function (src) {
    HFD.source = src === "official" ? "official" : "mirror";
    const btn = $("#nav-src");
    if (!btn) return;
    $("#nav-src-txt").textContent = HFD.source === "official" ? "官方" : "镜像";
    btn.classList.toggle("official", HFD.source === "official");
    btn.dataset.tip = `下载源：${NAME[HFD.source]}` + (HFD.source === "official" ? "（自动走代理）" : "（直连）") +
      " · 点击切换，两个页面共用";
  };

  async function choose(src) {
    let r;
    try { r = await api("set_source", { source: src }); } catch (e) {
      HFD.notify("error", "连接中断", "无法连接到后台：" + e.message);
      return;
    }
    if (r.error) { HFD.notify("error", "", r.error); return; }
    HFD.setSourceUI(r.source);
    const bad = r.source === "official" && /连不上|没有找到/.test(r.text || "");
    HFD.notify(bad ? "warn" : "ok", "下载源：" + NAME[r.source], (r.text || "") + (r.note ? "\n" + r.note : ""));
    HFD.kick && HFD.kick();
  }

  HFD.sourceMenu = function (btn) {
    const r = btn.getBoundingClientRect();
    const cur = HFD.source;
    HFD.menu([
      { text: "国内镜像（直连）", key: cur === "mirror" ? "✓" : "", click: () => choose("mirror") },
      { text: "官方（自动走代理）", key: cur === "official" ? "✓" : "", click: () => choose("official") },
    ], r.right + 8, r.top, { compact: true });
  };
})();
