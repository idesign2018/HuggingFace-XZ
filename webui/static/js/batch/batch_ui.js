/* 页面 2：批量下载 —— 按钮、修改路径窗口、aria2 高级设置窗口 */
"use strict";
(function () {
  const { $, $$, api, act, esc } = HFD;
  const T = HFD.batch;

  T.init = function (data) {
    const b = data.batch;
    T.writeSettings(b.settings, b.filters);
    $("#t-target").textContent = b.target || "";
    T.aria2Opts = b.aria2_opts;
    T.meta = data;
    const preset = $("#t-preset");
    Object.entries(data.presets).forEach(([k, v]) => {
      const o = document.createElement("option");
      o.value = v; o.textContent = k;
      preset.appendChild(o);
    });
    preset.onchange = () => {     // 常用类型：替换筛选框里原有的后缀条件，其他条件保留
      if (preset.value) {
        const q = $("#t-q");
        const rest = q.value.split(/\s+/).filter((t) => t && !/^\*?\.[^\s./\\*?]+$/.test(t) && !/^(ext|后缀)[:：]/i.test(t));
        q.value = [preset.value].concat(rest).join(" ");
        onFilterInput();
      }
      preset.value = "";
    };

    T.initList();
    T.initRepos();

    // 设置变化 → 同步到后台
    $$("[data-s]").forEach((el) => el.addEventListener(el.type === "checkbox" || el.type === "radio" ||
      el.tagName === "SELECT" ? "change" : "input", T.syncSettings));
    $("#t-q").addEventListener("input", onFilterInput);
    $('[data-s="live_filter"]').addEventListener("change", () => { if ($('[data-s="live_filter"]').checked) T.liveApply(); });
    $("#t-q").addEventListener("keydown", (e) => {      // 回车 = 只勾选符合条件的
      if (e.key === "Enter") act("batch_filter", { mode: "set", filters: T.readFilters(), view: T.view });
    });
    $("#t-view").onchange = () => T.setView($("#t-view").value);

    // ① 仓库
    const add = () => act("batch_add", { settings: T.readSettings(), filters: T.readFilters() });
    $("#t-add").onclick = add;
    $("#t-repo").addEventListener("keydown", (e) => { if (e.key === "Enter") add(); });
    $("#t-import").onclick = importTasks;
    $("#t-refresh-sel").onclick = () => act("batch_refresh", { keys: Array.from(T.repoSel) });
    $("#t-refresh-all").onclick = () => act("batch_refresh", { keys: null });
    $("#t-remove").onclick = () => act("batch_remove", { keys: Array.from(T.repoSel) });

    // ② 保存位置
    $("#t-root-browse").onclick = async () => {
      const d = await HFD.pick({ kind: "dir", title: "选择根目录（对话框里也可以直接新建文件夹）",
        initialdir: $('[data-s="root"]').value });
      if (d) { $('[data-s="root"]').value = d; T.syncSettings(); }
    };
    $("#t-root-new").onclick = async () => {
      const parent = $('[data-s="root"]').value;
      const name = await HFD.prompt("新建文件夹", `将在下面的目录中新建文件夹：\n${parent}\n\n文件夹名称（支持中文和特殊字符）：`);
      if (!name || !name.trim()) return;
      const r = await act("batch_new_folder", { parent, name });
      if (r && r.root) $('[data-s="root"]').value = r.root;
    };
    $("#t-root-open").onclick = () => act("batch_open_folder", { view: T.view });

    // ③ 筛选
    $$("[data-filter]").forEach((b) => {
      b.onclick = () => act("batch_filter", { mode: b.dataset.filter, filters: T.readFilters(), view: T.view });
    });
    $("#t-filter-clear").onclick = () => {
      $("#t-q").value = "";
      onFilterInput();
      $("#t-q").focus();
    };
    $("#t-q-help").onclick = filterHelp;

    // 清单操作
    $$("[data-check]").forEach((b) => {
      b.onclick = () => {
        const op = b.dataset.check;
        const sel = op === "check" || op === "uncheck";
        act("batch_check", { op, iids: sel ? T.sel.list() : null, view: T.view });
      };
    });
    $("#t-head-chk").onclick = () => act("batch_check", { op: "toggle_all", view: T.view });
    $("#t-chdir").onclick = changeDir;
    $("#t-reset").onclick = () => act("batch_reset_paths", { iids: T.sel.list() });
    $("#t-export-csv").onclick = async () => {
      if (!T.items.length) { HFD.alert("info", "提示", "请先添加仓库并获取文件列表。"); return; }
      const name = (await api("batch_export_name", {})).name;
      const p = await HFD.pick({ kind: "save", title: "导出下载清单", ext: ".csv",
        filetypes: [["CSV 文件", "*.csv"]], initialfile: name + "_下载清单.csv" });
      if (p) act("batch_export_csv", { path: p });
    };
    $("#t-import-csv").onclick = async () => {
      if (!T.items.length) { HFD.alert("info", "提示", "请先添加清单中的仓库并获取文件列表，再导入清单。"); return; }
      const p = await HFD.pick({ kind: "open", title: "导入下载清单", filetypes: [["CSV 文件", "*.csv"], ["所有文件", "*.*"]] });
      if (p) act("batch_import_csv", { path: p });
    };

    // ④ 下载
    $("#t-idm-browse").onclick = () => browseExe("idm_path", "选择 IDMan.exe");
    $("#t-aria2-browse").onclick = () => browseExe("aria2_path", "选择 aria2c.exe");
    $("#t-push-idm").onclick = () => withSettings("batch_push_idm", {});
    $("#t-aria2").onclick = () => withSettings("batch_aria2", {});
    $("#t-to-basic").onclick = async () => {
      // 常规页正在下载时，会加入当前任务一起下载
      await api("batch_settings", { settings: T.readSettings(), filters: T.readFilters(), save: true });
      const r = await HFD.actConfirm("batch_to_basic", {});
      if (r && r.ok) {
        HFD.basic.running = true;
        HFD.basic.updateButtons();
        HFD.switchTab("basic");          // 自动切到“常规”页看进度
      }
    };
    $("#t-aria2-opts").onclick = aria2Settings;
    $("#t-export-aria2").onclick = () => exportFile("batch_export_aria2", "导出 aria2 任务文件", "_aria2任务.txt");
    $("#t-export-urls").onclick = () => exportFile("batch_export_urls", "导出 URL 列表", "_URL列表.txt");
    $("#t-stop").onclick = () => act("batch_stop", {});
    $("#t-log-clear").onclick = () => T.log.clear();
    HFD.logToggle("#t-log-toggle");
    T.log = new HFD.LogBox($("#t-log"));
  };

  function onFilterInput() {
    T.syncSettings();
    T.updatePreview();
    T.liveApply();
  }

  /* 筛选写法说明 */
  function filterHelp() {
    const rows = [
      ["文件后缀", "<code>.safetensors</code> <code>*.gguf</code> <code>ext:json</code>"],
      ["文件名包含", "<code>fp16</code> <code>lora</code> <code>*q4_k*</code>（可用通配符 * ?）"],
      ["路径/文件夹包含", "<code>dir:vae</code> <code>path:unet</code> <code>text_encoder/</code>（带 / 的词也算路径）"],
      ["文件大小", "<code>&gt;1GB</code> <code>&lt;500MB</code> <code>&gt;=2G</code> <code>100MB~2GB</code> <code>10gb</code>（= 至少 10GB；" +
        "带比较符时只写数字按 MB；按 Windows 算法，1 GB = 1024³ 字节，<a href=\"#\" data-size-help>为什么和 HF 不一样？</a>）"],
      ["常见后缀", "<code>md</code> <code>json</code> <code>safetensors</code> <code>gguf</code> 等不带点也按后缀处理；想按文件名找用 <code>name:md</code>"],
      ["排除", "<code>-test</code> <code>-.md</code> <code>!*demo*</code>（前面加 - 或 !）"],
      ["有空格的词", "<code>\"my model\"</code>（用双引号括起来）"],
    ];
    const html = `<div class="muted" style="margin-bottom:8px">所有条件写在一个框里，用空格分隔，可以混写。` +
      `同一类条件之间是“或者”，不同类条件之间是“并且”；留空 = 不限制。</div>` +
      `<table class="help-tbl">${rows.map(([a, b]) => `<tr><td>${a}</td><td>${b}</td></tr>`).join("")}</table>` +
      `<div style="margin-top:10px">例：<code style="font-family:var(--mono)">.safetensors fp16 dir:unet &gt;1GB -test</code>` +
      `<div class="muted small">= 后缀是 .safetensors，并且文件名含 fp16，并且路径含 unet，并且大于 1GB，并且路径里不含 test</div></div>` +
      `<div class="muted small" style="margin-top:8px">也可以用中文关键字：后缀: 文件名: 路径: 文件夹: 大小: 排除:` +
      `<br>开启“实时勾选”时，输入停顿 0.4 秒自动“只勾选符合的”（清空条件 = 全部勾选；条件写错时不改动）；` +
      `关闭时按回车 = “只勾选符合的”。筛选只作用于当前显示的仓库。</div>`;
    HFD.modal.onOpen = (mask) => {
      HFD.modal.onOpen = null;
      const a = mask.querySelector("[data-size-help]");
      if (a) a.onclick = (e) => { e.preventDefault(); HFD.sizeHelp(); };
    };
    HFD.modal({ title: "筛选写法", html });
  }

  /* 先把界面上的设置同步到后台，再执行操作（避免输入后马上点按钮时设置还没同步） */
  async function withSettings(name, body) {
    await api("batch_settings", { settings: T.readSettings(), filters: T.readFilters(), save: true });
    return act(name, body);
  }

  async function browseExe(key, title) {
    const el = $(`[data-s="${key}"]`);
    const p = await HFD.pick({ kind: "open", title, initialdir: el.value, filetypes: [["程序", "*.exe"], ["所有文件", "*.*"]] });
    if (p) { el.value = p; T.syncSettings(); }
  }

  async function exportFile(name, title, suffix) {
    const base = (await api("batch_export_name", {})).name;
    const p = await HFD.pick({ kind: "save", title, ext: ".txt", filetypes: [["文本文件", "*.txt"]], initialfile: base + suffix });
    if (p) withSettings(name, { path: p });
  }

  async function importTasks() {
    const paths = await HFD.pick({ kind: "open_multi", title: "导入任务（aria2 任务文件 或 URL 列表，可多选）",
      filetypes: [["文本文件", "*.txt *.aria2 *.lst *.list *.session *.csv"], ["所有文件", "*.*"]] });
    if (paths && paths.length) withSettings("batch_import_tasks", { paths });
  }

  async function changeDir() {
    const ids = T.sel.list();
    if (!ids.length) {
      HFD.alert("info", "提示", "请先在列表中选中要修改的文件（单击行，按住 Ctrl/Shift 可多选；Ctrl+A 全选）。");
      return;
    }
    const d = await HFD.pick({ kind: "dir", title: `为选中的 ${ids.length} 个文件选择保存目录`, initialdir: $('[data-s="root"]').value });
    if (!d) return;
    const keep = await HFD.choose("保留目录结构？",
      "保留：保存为 “所选目录 + HF 中的原路径”（保留子文件夹）\n只要文件名：直接放在所选目录下",
      [{ text: "取消", value: null }, { text: "只要文件名", value: false }, { text: "保留目录结构", value: true, primary: true }]);
    if (keep === null || keep === undefined) return;
    act("batch_change_dir", { iids: ids, dir: d, keep });
  }

  /* ---------------- 修改单个文件的保存路径 ---------------- */
  T.editPath = async function (iid) {
    const info = await api("batch_path_info", { iid });
    if (info.error && !info.hf) { HFD.alert("error", "出错", info.error); return; }
    const html =
      `<div>仓库：${esc(info.repo)}　　HF 路径：${esc(info.hf)}（${esc(info.size)}）</div>` +
      `<div class="muted" style="margin-top:4px">保存位置：可以只填文件夹（如 F:\\APP1），会自动补上原文件名；也可以填完整路径来改文件名。</div>` +
      `<input class="wide" id="pe-input" style="margin-top:8px" spellcheck="false" value="${esc(info.local)}">` +
      `<div id="pe-final" style="margin-top:6px"></div>`;
    const check = HFD.debounce(async () => {
      const m = document.getElementById("pe-input");
      if (!m) return;
      const r = await api("batch_path_info", { iid, text: m.value });
      const el = document.getElementById("pe-final");
      if (!el) return;
      if (r.error) { el.className = "warn"; el.textContent = "⚠ " + r.error; }
      else { el.className = "info"; el.textContent = "最终保存为：" + r.final + (r.added ? "（已自动补上文件名）" : ""); }
    }, 200);
    HFD.modal.onOpen = (mask) => {
      HFD.modal.onOpen = null;
      mask.querySelector("#pe-input").addEventListener("input", check);
      check();
    };
    const setVal = (m, v) => { const el = m.querySelector("#pe-input"); el.value = v; check(); };
    const startDir = (m) => m.querySelector("#pe-input").value;
    await HFD.modal({
      title: "修改保存路径", html,
      buttons: [
        { text: "选择文件夹…", left: true, click: async (m) => {
          const d = await HFD.pick({ kind: "dir", title: "选择保存文件夹（文件名保持不变）", initialdir: startDir(m) });
          if (d) setVal(m, d.replace(/[\\/]+$/, "") + "\\" + info.filename);
          return false;
        } },
        { text: "另存为（可改文件名）…", left: true, click: async (m) => {
          const cur = startDir(m);
          const p = await HFD.pick({ kind: "save", title: "选择保存位置和文件名", initialdir: cur,
            initialfile: cur.split(/[\\/]/).pop() || info.filename, overwrite: false });
          if (p) setVal(m, p);
          return false;
        } },
        { text: "恢复默认", left: true, click: (m) => { setVal(m, info.default); return false; } },
        { text: "取消", value: false },
        { text: "确定", value: true, primary: true, click: async (m) => {
          const r = await act("batch_set_path", { iid, text: m.querySelector("#pe-input").value });
          return !!(r && r.ok);
        } },
      ],
    });
  };

  /* ---------------- aria2 高级设置 ---------------- */
  async function aria2Settings() {
    const meta = T.meta;
    const o = Object.assign({}, T.aria2Opts);
    const rows = meta.aria2_fields.map(([k, label, kind, hint]) => {
      let input;
      if (k === "file_alloc") {
        input = `<select data-a="${k}">${meta.file_alloc.map((v) => `<option${v === o[k] ? " selected" : ""}>${v}</option>`).join("")}</select>`;
      } else if (k === "proxy_mode") {
        input = `<select data-a="${k}">${Object.entries(meta.proxy_modes).map(([v, t]) =>
          `<option value="${v}"${v === o[k] ? " selected" : ""}>${esc(t)}</option>`).join("")}</select>`;
      } else {
        input = `<input data-a="${k}" class="wide" ${kind === "number" ? 'type="number"' : ""} spellcheck="false" value="${esc(o[k])}">`;
      }
      return `<div class="lab">${esc(label)}：</div><div>${input}</div><div class="hint">${esc(hint)}</div>`;
    }).join("");
    const html = `<div class="form-grid">${rows}</div>` +
      `<div class="muted" style="margin-top:8px">同时下载几个文件，用页面上的“同时下载文件数”设置（最大 16）；` +
      `总连接数 = 同时下载文件数 × 每个文件线程数。连接太多可能被服务器限流（429），速度不升反降。</div>` +
      `<div id="a2-preview" class="info" style="margin-top:6px;word-break:break-all"></div>`;
    let conc = null;
    const collect = (m) => {
      const r = {};
      m.querySelectorAll("[data-a]").forEach((el) => { r[el.dataset.a] = el.value; });
      return r;
    };
    const preview = HFD.debounce(async (m) => {
      const r = await api("aria2_preview", { opts: collect(m) });
      const el = m.querySelector("#a2-preview");
      if (!el) return;
      if (r.errors.length) { el.className = "warn"; el.textContent = "⚠ " + r.errors.join("；"); }
      else { el.className = "info"; el.textContent = "将使用的参数：" + r.cli + "\n" + r.proxy; }
      el.style.whiteSpace = "pre-wrap";
      const manual = m.querySelector('[data-a="proxy_mode"]').value === "manual";
      m.querySelector('[data-a="proxy"]').disabled = !manual;
    }, 150);
    HFD.modal.onOpen = (mask) => {
      HFD.modal.onOpen = null;
      mask.querySelectorAll("[data-a]").forEach((el) => el.addEventListener("input", () => preview(mask)));
      preview(mask);
    };
    const set = (m, vals) => {
      Object.entries(vals).forEach(([k, v]) => { const el = m.querySelector(`[data-a="${k}"]`); if (el) el.value = v; });
      preview(m);
    };
    await HFD.modal({
      title: "aria2 高级设置", html,
      buttons: [
        { text: "一键速度最大化", left: true, click: (m) => {
          set(m, { connections: 16, min_split: "1M", lowest_speed: "50K", overall_limit: "0", file_limit: "0", disk_cache: "64M" });
          conc = 16;
          HFD.toast("已套用“速度最大化”：每个文件 16 线程、分块 1M、同时下载 16 个文件、不限速。保存后生效。");
          return false;
        } },
        { text: "恢复默认", left: true, click: (m) => { set(m, meta.aria2_defaults); return false; } },
        { text: "检测系统代理", left: true, click: async () => {
          const r = await api("detect_proxy", {});
          HFD.toast(r.text, 6000);
          return false;
        } },
        { text: "官方参数说明", left: true, click: () => {
          api("open_url", { url: "https://aria2.github.io/manual/en/html/aria2c.html" });
          return false;
        } },
        { text: "取消", value: false },
        { text: "保存", value: true, primary: true, click: async (m) => {
          const opts = collect(m);
          const r = await act("aria2_save", { opts, concurrency: conc });
          if (!(r && r.ok)) return false;
          T.aria2Opts = opts;
          if (conc) $('[data-s="concurrency"]').value = conc;
          return true;
        } },
      ],
    });
  }
})();
