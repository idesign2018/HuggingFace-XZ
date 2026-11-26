/* “常规”页列表的右键菜单 */
"use strict";
(function () {
  const { $, act } = HFD;
  /* ---------------- “常规”页列表的菜单 ---------------- */
  HFD.basicMenu = function (ids) {
    const B = HFD.basic;
    const rows = ids.map((k) => B.rows.get(k)).filter(Boolean);
    const one = rows.length === 1, st = rows.map((r) => r[4]);
    const active = st.filter((s) => s === "下载中" || s === "等待中").length;
    const doneOne = one && (st[0] === "完成" || st[0] === "已是最新");
    const run = async (action, extra) => {
      const r = await act("basic_action", Object.assign({ action, ids }, extra || {}));
      if (r && r.props) HFD.showProps(r.props);
    };
    const n = rows.length > 1 ? `（${rows.length} 个）` : "";
    return [
      { text: "打开", key: "Enter", disabled: !doneOne, click: () => run("open") },
      { text: "打开方式…", disabled: !doneOne, click: () => run("open_with") },
      { text: "打开文件夹", disabled: !one, click: () => run("folder") },
      "-",
      { text: "继续下载" + n, disabled: active === rows.length, click: () => run("continue") },
      { text: "重新下载" + n, disabled: active === rows.length, click: () => run("redownload") },
      { text: "停止下载" + n, disabled: !active, click: () => run("stop") },
      "-",
      { text: "复制保存路径", disabled: !one, click: () => HFD.copy(rows[0][10]) },
      "-",
      { text: "从列表移除" + n, key: "Del", disabled: active > 0, click: () => run("remove") },
      { text: "移除并删除本地文件" + n, danger: true, disabled: active > 0, click: () => run("remove", { delete_files: true }) },
      "-",
      { text: "属性", disabled: !one, click: () => run("props") },
    ];
  };
})();
