/* “批量”页列表的右键菜单 */
"use strict";
(function () {
  const { $, size, sizeHF } = HFD;
  /* ---------------- “批量”页列表的菜单 ---------------- */
  HFD.batchMenu = function (iids) {
    const T = HFD.batch, I = T.I;
    const rows = iids.map((id) => T.byId.get(id)).filter(Boolean);
    const one = rows.length === 1;
    const n = rows.length > 1 ? `（${rows.length} 个）` : "";
    const imp = rows.some((r) => (T.repos.find((m) => m[0] === r[I.key]) || [])[7] === "import");
    const active = rows.filter((r) => /常规下载：(下载中|排队中)/.test(r[I.status] || "")).length;
    const run = async (action, extra) => {
      const body = Object.assign({ action, iids, settings: T.readSettings() }, extra || {});
      const r = await HFD.actConfirm("batch_action", body);
      if (r && r.props) HFD.showProps(r.props);
      if (r && r.ok && r.switch) HFD.switchTab(r.switch);
    };
    return [
      { text: "打开", key: "Enter", disabled: !(one && rows[0][I.exists]), click: () => run("open") },
      { text: "打开方式…", disabled: !(one && rows[0][I.exists]), click: () => run("open_with") },
      { text: "打开文件夹", disabled: !one, click: () => run("folder") },
      "-",
      { text: "用常规下载" + n + (imp ? "（跳过导入的链接）" : ""), click: () => run("continue") },
      { text: "重新下载" + n, click: () => run("redownload") },
      { text: "停止下载" + n, disabled: !active, click: () => run("stop") },
      "-",
      { text: "勾选" + n, click: () => act("batch_check", { op: "check", iids }) },
      { text: "取消勾选" + n, click: () => act("batch_check", { op: "uncheck", iids }) },
      { text: "修改保存路径…", disabled: !one, click: () => T.editPath(iids[0]) },
      { text: "恢复默认路径" + n, click: () => act("batch_reset_paths", { iids }) },
      { text: "复制保存路径", disabled: !one, click: () => HFD.copy(rows[0][I.local]) },
      "-",
      { text: "属性", disabled: !one, click: () => run("props") },
    ];
  };
})();
