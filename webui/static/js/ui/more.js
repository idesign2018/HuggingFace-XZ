/* 导航栏底部的“更多”菜单：说明 / 关于作者 / 关于软件

   “关于作者”“关于软件”里的图文不在代码里，改这两个文件就能生效（图片也放同一个文件夹）：
       程序目录\关于\关于作者.html
       程序目录\关于\关于软件.html
   版本号在 webui\core\version.py 里改。 */
"use strict";
(function () {
  const { api, esc } = HFD;

  /* 版本与运行环境（缓存一次，重新打开窗口时刷新） */
  HFD.loadAbout = async function (force) {
    if (!force && HFD._about) return HFD._about;
    try {
      HFD._about = await api("about", {});
    } catch (e) {
      HFD._about = null;
    }
    return HFD._about;
  };

  function frame(file, mtime, short) {
    const url = "/content/" + encodeURIComponent(file) + "?v=" + (mtime || Date.now());
    return `<iframe class="content-frame${short ? " short" : ""}" src="${url}" title="${esc(file)}"></iframe>`;
  }

  /* 在 iframe 自己的页面里复制（点击发生在 iframe 里，那边才有焦点），失败再用主页面的办法 */
  async function copyIn(win, text) {
    try {
      await win.navigator.clipboard.writeText(text);
      HFD.toast("已复制：" + text);
    } catch (e) {
      try {
        const d = win.document, t = Object.assign(d.createElement("textarea"), { value: text });
        d.body.appendChild(t); t.select();
        const ok = d.execCommand("copy");
        t.remove();
        if (!ok) throw new Error("copy");
        HFD.toast("已复制：" + text);
      } catch (e2) { HFD.copy(text); }
    }
  }

  /* 内容页里的点击：
       data-copy / mailto: 链接 → 复制到剪贴板
       http(s) 链接            → 用默认浏览器打开
       二维码图片（.qr）        → 用默认浏览器打开大图（还是占位图时提示怎么换） */
  function bindFrame(iframe) {
    iframe.addEventListener("load", () => {
      let win, doc;
      try { win = iframe.contentWindow; doc = win.document; } catch (e) { return; }
      doc.addEventListener("click", (e) => {
        const img = e.target.closest("img.qr");
        if (img) {
          e.preventDefault();
          const name = img.alt || "二维码";
          if (!img.dataset.real) {
            HFD.notify("info", "还没有放" + name + "二维码",
              `把图片命名为“${name}.png”（或 .jpg / .webp / .svg）放进“关于”文件夹，关掉窗口再打开就会显示。`);
          } else {
            api("open_url", { url: img.src.split("?")[0] });
          }
          return;
        }
        const a = e.target.closest("a[href], a[data-copy]");
        if (!a) return;
        const href = a.getAttribute("href") || "";
        if (a.dataset.copy || href.startsWith("mailto:")) {
          e.preventDefault();
          copyIn(win, a.dataset.copy || decodeURIComponent(href.slice(7).split("?")[0]));
        } else if (/^https?:\/\//i.test(href)) {
          e.preventDefault();
          api("open_url", { url: a.href });
        } else if (href.startsWith("#")) {
          /* 页内锚点：保持默认 */
        } else if (href) {
          e.preventDefault();          // 其他相对链接（如图片文件）：也交给默认浏览器
          api("open_url", { url: a.href });
        }
      });
    });
  }

  function missing(info) {
    return `<div class="about-missing">还没有找到 <b>${esc(info.name)}</b>。<br>` +
      `把文件放到这个位置即可（图片也放同一个文件夹，直接写 <code>&lt;img src="图片名.png"&gt;</code>）：<br>` +
      `<code>${esc(info.path)}</code><br>` +
      `放好以后关掉这个窗口再点开就会显示，不用重启程序。</div>`;
  }

  function openContent() {
    api("open_content", {});
    return false;                       // 不关闭当前窗口
  }

  /* ---------------- 关于作者 ---------------- */
  HFD.aboutAuthor = async function () {
    const a = await HFD.loadAbout(true);
    const info = (a && a.content && a.content.author) || { name: "关于作者.html", path: "", exists: false };
    HFD.modal.onOpen = (mask) => {
      HFD.modal.onOpen = null;
      mask.querySelectorAll("iframe.content-frame").forEach(bindFrame);
    };
    await HFD.modal({
      title: "关于作者",
      size: "info",
      outside: true,
      fill: info.exists,
      html: info.exists
        ? frame(info.name, info.mtime) +
          `<div class="about-note">内容文件：${esc(info.path)}（可以直接编辑，图片放同一个文件夹）</div>`
        : missing(info),
      buttons: [
        { text: "打开内容文件夹", left: true, click: openContent },
        { text: "关闭", value: true, primary: true },
      ],
    });
  };

  /* ---------------- 关于软件 ---------------- */
  HFD.aboutApp = async function () {
    const a = await HFD.loadAbout(true);
    if (!a) {
      HFD.notify("error", "读取失败", "拿不到版本信息，请重新启动程序再试。");
      return;
    }
    const d = a.deps || {}, p = a.paths || {}, st = a.stats || {};
    const row = (k, v, cls) => `<tr><td>${esc(k)}</td><td class="${cls || ""}">${esc(v)}</td></tr>`;
    const path = (k, v) => `<tr><td>${esc(k)}</td>` +
      `<td class="path" data-copy="${esc(v)}" title="点击复制">${esc(v)}</td></tr>`;

    const main = [
      row("版本", "v" + a.version + (a.build ? "（" + a.build + "）" : "")),
      a.author ? row("作者", a.author) : "",
      a.copyright ? row("版权", a.copyright) : "",
      a.license ? row("许可", a.license) : "",
      a.homepage ? row("主页", a.homepage) : "",
    ].join("");
    /* 不常看的：默认折叠，点标题展开 */
    const more = [
      row("运行环境", "Python " + a.python + " · " + a.system),
      row("窗口模式", a.window),
      row("本次已运行", HFD.dur(a.uptime)),
      row("已记录", `${st.history || 0} 条下载记录 · ${st.repos || 0} 个批量仓库`),
      row("依赖", `huggingface_hub ${d.huggingface_hub || "—"} · hf_xet ${d.hf_xet || "—"} · pywebview ${d.pywebview || "—"}`),
      row("外部程序", `IDM：${d.IDM || "—"}　aria2：${d.aria2 || "—"}`),
    ].join("") + Object.keys(p).filter((k) => p[k]).map((k) => path(k, p[k])).join("");
    const hasApp = !!(a.content && a.content.app && a.content.app.exists);

    const html =
      `<div class="about-h"><div class="about-logo">HF</div><div>` +
      `<div class="about-name">${esc(a.name)}${a.sub ? "（" + esc(a.sub) + "）" : ""}</div>` +
      `<div class="about-ver">版本 v${esc(a.version)}${a.build ? " · " + esc(a.build) : ""}</div></div></div>` +
      `<table class="help-tbl about-tbl">${main}</table>` +
      `<details class="about-more"><summary>运行环境与文件位置</summary>` +
      `<table class="help-tbl about-tbl">${more}</table></details>` +
      (hasApp
        ? frame(a.content.app.name, a.content.app.mtime)
        : `<div class="about-note">可以把自己的“版本说明 / 致谢 / 免责声明”写进：` +
          `<code>${esc((a.content && a.content.dir) || "")}\\关于软件.html</code></div>`);

    HFD.modal.onOpen = (mask) => {
      HFD.modal.onOpen = null;
      mask.querySelectorAll("[data-copy]").forEach((td) => {
        td.onclick = () => HFD.copy(td.dataset.copy);
      });
      mask.querySelectorAll("iframe.content-frame").forEach(bindFrame);
    };
    await HFD.modal({
      title: "关于软件",
      size: "info",
      outside: true,
      fill: hasApp,
      html,
      buttons: [
        { text: "打开内容文件夹", left: true, click: openContent },
        { text: "确定", value: true, primary: true },
      ],
    });
    HFD.modal.onOpen = null;
  };

  /* ---------------- “更多”菜单 ---------------- */
  HFD.moreMenu = function (btn) {
    const r = btn.getBoundingClientRect();
    HFD.menu([
      { text: "说明", click: () => HFD.sizeHelp() },
      { text: "关于作者", click: () => HFD.aboutAuthor() },
      { text: "关于软件", click: () => HFD.aboutApp() },
    ], r.right + 8, r.top, { compact: true });
  };
})();
