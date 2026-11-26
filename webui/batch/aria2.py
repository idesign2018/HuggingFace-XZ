# -*- coding: utf-8 -*-
"""
aria2 相关（网页版，无界面代码）：
  - 高级设置：默认值、参数检查、生成命令行参数
  - 代理：不使用 / 使用系统中的设置 / 手动填写
  - 任务文件：写入（保留目录结构）、解析（导入 aria2 任务文件 / URL 列表）
"""
import os
import re
import shutil
import urllib.request

APP_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(APP_DIR)

PROXY_MODES = {"none": "不使用代理", "system": "使用系统中的设置", "manual": "手动填写"}
PROXY_MODE_VALUES = {v: k for k, v in PROXY_MODES.items()}

ARIA2_DEFAULTS = {
    "connections": 16,      # 每个文件线程数（-x / -s，官方版 aria2 最大 16）
    "min_split": "1M",      # 分块最小大小（-k，1M~1024M）
    "max_tries": 10,        # 失败重试次数（0 = 无限）
    "retry_wait": 5,        # 重试间隔（秒）
    "timeout": 60,          # 读取超时（秒）
    "connect_timeout": 30,  # 连接超时（秒）
    "lowest_speed": "50K",  # 慢速断开阈值（0 = 不启用）
    "overall_limit": "0",   # 总限速（0 = 不限）
    "file_limit": "0",      # 单个文件限速（0 = 不限）
    "disk_cache": "64M",    # 磁盘缓存（0 = 关闭）
    "file_alloc": "none",   # 磁盘空间分配方式
    "proxy_mode": "none",   # none / system / manual
    "proxy": "",            # 手动填写的代理地址
    "user_agent": "",       # 自定义 User-Agent
    "extra": "",            # 其他自定义参数（aria2 原始写法）
}
FILE_ALLOC_CHOICES = ("none", "prealloc", "trunc", "falloc")

# 设置窗口的字段说明（网页端据此生成表单）
ARIA2_FIELDS = [
    ("connections", "每个文件线程数", "number", "1~16。官方版 aria2 最大 16（同时设置 -x 和 -s）"),
    ("min_split", "分块最小大小", "text", "1M~1024M。越小切得越细，1M 最细"),
    ("max_tries", "失败重试次数", "number", "0 = 无限重试"),
    ("retry_wait", "重试间隔（秒）", "number", "每次重试前等待的秒数"),
    ("timeout", "读取超时（秒）", "number", "一段时间收不到数据就重连"),
    ("connect_timeout", "连接超时（秒）", "number", "连接服务器的最长等待时间"),
    ("lowest_speed", "慢速断开阈值", "text", "某个连接低于这个速度就断开重连，如 50K；0 = 不启用"),
    ("overall_limit", "总限速", "text", "所有文件合计的下载速度上限，如 20M；0 = 不限"),
    ("file_limit", "单个文件限速", "text", "每个文件的速度上限；0 = 不限"),
    ("disk_cache", "磁盘缓存", "text", "如 64M；高速下载时减轻硬盘压力；0 = 关闭"),
    ("file_alloc", "磁盘空间分配", "select", "none 开始最快；falloc 可减少大文件碎片（NTFS）"),
    ("proxy_mode", "代理方式", "select", "选“使用系统中的设置”会自动读取 Windows 系统代理"),
    ("proxy", "代理地址", "text", "仅“手动填写”时使用，如 http://127.0.0.1:7890"),
    ("user_agent", "User-Agent", "text", "留空 = 使用 aria2 默认值"),
    ("extra", "其他参数", "text", "直接写 aria2 参数，空格分隔，如 --check-certificate=false"),
]


def detect_aria2():
    """自动查找 aria2c：先查系统 PATH，再查程序所在目录"""
    p = shutil.which("aria2c")
    if p:
        return p
    for d in (APP_DIR, PARENT_DIR):
        p = os.path.join(d, "aria2c.exe")
        if os.path.isfile(p):
            return p
    return ""


def aria2_exe(path_text):
    p = (path_text or "").strip() or "aria2c"
    if os.path.isfile(p):
        return p
    return shutil.which(p) or ""


# ============================================================
# 高级设置
# ============================================================
def aria2_size_norm(text):
    """整数，可带 K 或 M；也接受 10MB、500kb。返回统一写法；格式不对返回 None"""
    m = re.fullmatch(r"\s*(\d+)\s*([KkMm]?)[Bb]?\s*", str(text))
    if not m:
        return None
    return m.group(1) + m.group(2).upper()


def aria2_size_bytes(text):
    n = aria2_size_norm(text)
    if n is None:
        return None
    unit = n[-1] if n[-1] in "KM" else ""
    return int(n[:-1] if unit else n) * {"": 1, "K": 1024, "M": 1024 ** 2}[unit]


def split_cli_args(text):
    """按空格拆分参数，双引号里的空格保留；不对反斜杠转义（Windows 路径可直接写）"""
    out, cur, quoted, has = [], [], False, False
    for ch in text or "":
        if ch == '"':
            quoted, has = not quoted, True
            continue
        if ch.isspace() and not quoted:
            if cur or has:
                out.append("".join(cur))
            cur, has = [], False
            continue
        cur.append(ch)
    if quoted:
        raise ValueError("引号没有配对")
    if cur or has:
        out.append("".join(cur))
    return [t for t in out if t]


def normalize_aria2_opts(raw, fallback=False):
    """检查并统一设置。返回 (设置, 错误列表)；fallback=True 时格式不对的项恢复默认值"""
    o = dict(ARIA2_DEFAULTS)
    o.update({k: v for k, v in (raw or {}).items() if k in ARIA2_DEFAULTS})
    errors = []

    def bad(key, msg):
        errors.append(msg)
        if fallback:
            o[key] = ARIA2_DEFAULTS[key]

    for key, lo, hi, label in (("connections", 1, 16, "每个文件线程数"), ("max_tries", 0, 9999, "失败重试次数"),
                               ("retry_wait", 0, 600, "重试间隔"), ("timeout", 1, 600, "读取超时"),
                               ("connect_timeout", 1, 600, "连接超时")):
        try:
            v = int(str(o[key]).strip())
            if not lo <= v <= hi:
                raise ValueError
            o[key] = v
        except Exception:
            bad(key, f"{label}：请填 {lo}~{hi} 之间的整数")
    for key, label in (("min_split", "分块最小大小"), ("lowest_speed", "慢速断开阈值"),
                       ("overall_limit", "总限速"), ("file_limit", "单个文件限速"), ("disk_cache", "磁盘缓存")):
        n = aria2_size_norm(o[key])
        if n is None:
            bad(key, f"{label}：格式不对，请写 0、500K、10M 这样的整数（单位只支持 K、M）")
        else:
            o[key] = n
    b = aria2_size_bytes(o["min_split"])
    if b is not None and not (1024 ** 2 <= b <= 1024 ** 3):
        bad("min_split", "分块最小大小：范围是 1M~1024M")
    if o["file_alloc"] not in FILE_ALLOC_CHOICES:
        bad("file_alloc", "磁盘空间分配：请从下拉框中选择")
    o["proxy"] = str(o["proxy"]).strip()
    mode = str(o["proxy_mode"]).strip()
    mode = PROXY_MODE_VALUES.get(mode, mode)
    if "proxy_mode" not in (raw or {}):
        mode = "manual" if o["proxy"] else "none"     # 旧版配置：填过代理地址的当作“手动填写”
    if mode not in PROXY_MODES:
        bad("proxy_mode", "代理方式：请从下拉框中选择")
        mode = ARIA2_DEFAULTS["proxy_mode"]
    o["proxy_mode"] = mode
    if mode == "manual":
        if not o["proxy"]:
            bad("proxy", "代理：选择了“手动填写”，请填写代理地址，如 http://127.0.0.1:7890")
            if fallback:
                o["proxy_mode"] = "none"
        elif any(c.isspace() for c in o["proxy"]):
            bad("proxy", "代理：地址中不能有空格，如 http://127.0.0.1:7890")
    o["user_agent"] = str(o["user_agent"]).strip()
    o["extra"] = str(o["extra"]).strip()
    try:
        for t in split_cli_args(o["extra"]):
            if not t.startswith("-"):
                bad("extra", f"其他参数：“{t}” 不是 aria2 参数（应以 -- 开头，如 --check-certificate=false）")
                break
    except ValueError as e:
        bad("extra", f"其他参数：{e}")
    return o, errors


def aria2_option_args(o):
    """根据高级设置生成 aria2 命令行参数（不含任务文件、日志等）"""
    n = o["connections"]
    args = [
        "--continue=true",
        f"--max-connection-per-server={n}",
        f"--split={n}",
        f"--min-split-size={o['min_split']}",
        f"--max-tries={o['max_tries']}",
        f"--retry-wait={o['retry_wait']}",
        f"--timeout={o['timeout']}",
        f"--connect-timeout={o['connect_timeout']}",
        f"--lowest-speed-limit={o['lowest_speed']}",
        f"--max-overall-download-limit={o['overall_limit']}",
        f"--max-download-limit={o['file_limit']}",
        f"--disk-cache={o['disk_cache']}",
        f"--file-allocation={o['file_alloc']}",
        "--auto-file-renaming=false",
    ]
    args += proxy_args(o)
    if o["user_agent"]:
        args.append(f"--user-agent={o['user_agent']}")
    args += split_cli_args(o["extra"])              # 放在最后：和上面重复时以这里为准
    return args


def format_cli(args):
    """把参数列表变成可以直接复制到命令行的文字"""
    out = []
    for a in args:
        if any(c.isspace() or c in '&|<>^' for c in a):
            k, eq, v = a.partition("=")
            out.append(f'{k}="{v}"' if eq and k.startswith("-") else f'"{a}"')
        else:
            out.append(a)
    return " ".join(out)


def preview_opts(raw):
    """网页设置窗口的实时预览：返回 (错误列表, 参数文字, 代理说明)"""
    opts, errs = normalize_aria2_opts(raw)
    if errs:
        return errs, "", ""
    return [], format_cli(aria2_option_args(opts)), describe_proxy(opts)


# ============================================================
# 代理
# ============================================================
def _proxy_url(addr):
    addr = (addr or "").strip()
    if not addr:
        return ""
    return addr if "://" in addr else "http://" + addr


def get_system_proxy():
    """读取 Windows 系统代理（Clash、v2rayN 开启“系统代理”后写在这里），其次读环境变量"""
    info = {"http": "", "https": "", "source": "", "note": ""}
    enable = server = pac = None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as k:
            def q(name):
                try:
                    return winreg.QueryValueEx(k, name)[0]
                except OSError:
                    return None
            enable, server, pac = q("ProxyEnable"), q("ProxyServer"), q("AutoConfigURL")
    except Exception:
        pass
    if enable and server:
        server = str(server).strip()
        if "=" not in server:
            info["http"] = info["https"] = _proxy_url(server)
        else:
            parts = {}
            for p in server.split(";"):
                if "=" in p:
                    proto, addr = p.split("=", 1)
                    parts[proto.strip().lower()] = addr.strip()
            info["http"] = _proxy_url(parts.get("http", ""))
            info["https"] = _proxy_url(parts.get("https", ""))
            if not info["http"] and not info["https"] and parts.get("socks"):
                info["note"] = ("系统只设置了 SOCKS 代理，aria2 不支持 SOCKS；"
                                "请在代理软件里查看 HTTP 代理端口，改用“手动填写”。")
        if info["http"] or info["https"]:
            info["source"] = "Windows 系统代理设置"
            return info
    try:
        env = urllib.request.getproxies_environment()
    except Exception:
        env = {}
    http, https = env.get("http", ""), env.get("https", "")
    if http or https:
        info.update(http=_proxy_url(http), https=_proxy_url(https), source="环境变量 HTTP_PROXY / HTTPS_PROXY")
        return info
    if pac:
        info["note"] = (f"系统使用的是“自动配置脚本”（{pac}），aria2 无法读取；"
                        "请在代理软件里查看 HTTP 代理端口，改用“手动填写”。")
    elif not info["note"]:
        info["note"] = "系统没有设置代理，将直接连接。"
    return info


def proxy_args(o, sysinfo=None):
    mode = o.get("proxy_mode", "none")
    if mode == "manual" and o.get("proxy"):
        return [f"--all-proxy={o['proxy']}"]
    if mode == "system":
        info = sysinfo if sysinfo is not None else get_system_proxy()
        http, https = info.get("http"), info.get("https")
        if http and http == https:
            return [f"--all-proxy={http}"]
        args = []
        if http:
            args.append(f"--http-proxy={http}")
        if https:
            args.append(f"--https-proxy={https}")
        return args
    return []


def describe_proxy(o, sysinfo=None):
    mode = o.get("proxy_mode", "none")
    if mode == "manual":
        return f"代理：手动填写 {o.get('proxy')}" if o.get("proxy") else "代理：不使用"
    if mode == "system":
        info = sysinfo if sysinfo is not None else get_system_proxy()
        if info["http"] or info["https"]:
            if info["http"] == info["https"]:
                addr = info["http"]
            else:
                addr = f"HTTP {info['http'] or '直连'}，HTTPS {info['https'] or '直连'}"
            return f"代理：使用系统中的设置（{info['source']}）→ {addr}"
        return "代理：使用系统中的设置 → " + info["note"]
    return "代理：不使用"


# ============================================================
# 任务文件
# ============================================================
def write_aria2_file(jobs, path, make_dirs=True):
    """每个文件一行地址 + 缩进的 dir/out 选项（保留目录结构），UTF-8 编码"""
    lines = []
    for j in jobs:
        folder, name = os.path.split(j["local"])
        if make_dirs:
            os.makedirs(folder, exist_ok=True)
        lines += ["\t".join([j["url"]] + list(j.get("mirrors") or [])), f"  dir={folder}", f"  out={name}"]
        for h in j.get("headers") or []:
            lines.append(f"  header={h}")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")


_URI_RE = re.compile(r"(?i)\b(?:https?|ftp|sftp)://[^\s\"'<>]+")
_OPT_RE = re.compile(r"^([A-Za-z][\w-]*)=(.*)$")
_URL_TAIL = "，。；、,;)）]】>\"'"


def read_text_file(path):
    """读取文本文件：优先 UTF-8，不行再试 GBK"""
    with open(path, "rb") as f:
        data = f.read()
    for enc in ("utf-8-sig", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def parse_task_text(text):
    """
    解析 aria2 任务文件 / URL 列表。
    返回 (任务列表, 跳过的磁力链接数, 无法识别的行数)；每个任务：{"urls": [...], "opts": {...}, "headers": [...]}
    """
    entries, n_magnet, n_bad = [], 0, 0
    cur = None
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        indented = line[:1] in (" ", "\t")
        m = _OPT_RE.match(s)
        if cur is not None and m and (indented or m.group(1) in ("dir", "out", "header")):
            k, v = m.group(1), m.group(2).strip()
            if k == "header":
                cur["headers"].append(v)
            else:
                cur["opts"][k] = v
            continue
        if s.lower().startswith("magnet:"):
            n_magnet += 1
            cur = None
            continue
        parts = [p.strip() for p in s.split("\t") if p.strip()]
        if parts and all(_URI_RE.fullmatch(p) for p in parts):
            cur = {"urls": parts, "opts": {}, "headers": []}
            entries.append(cur)
            continue
        found = [re.split(r"[，。；、！？）】》“”‘’]", u)[0].rstrip(_URL_TAIL) for u in _URI_RE.findall(s)]
        found = [u for u in found if u]
        if found:
            for u in found:
                cur = {"urls": [u], "opts": {}, "headers": []}
                entries.append(cur)
        else:
            n_bad += 1
            cur = None
    return entries, n_magnet, n_bad
