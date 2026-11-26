# -*- coding: utf-8 -*-
"""
通用工具（网页版）：大小/时间格式、Windows 路径处理、筛选、HF 接口、地址识别、IDM 检测。
只依赖 Python 标准库；不导入 huggingface_hub（启动更快）。
"""
import os
import re
import json
import fnmatch
import subprocess
import urllib.request
import urllib.error
from urllib.parse import quote, unquote, urljoin, urlparse

from core import net        # 官方源走代理，国内镜像直连

# ============================================================
# 常量
# ============================================================
ENDPOINTS = {
    "mirror": "https://hf-mirror.com",      # 国内镜像
    "official": "https://huggingface.co",   # 官方
}
API_TYPES = {"model": "models", "dataset": "datasets", "space": "spaces"}
URL_PREFIX = {"model": "", "dataset": "datasets/", "space": "spaces/"}
USER_AGENT = "HFD-Web/2.0 (+python-urllib)"
LOOPBACK = ".".join(("127", "0", "0", "1"))     # 本机地址（只允许本机访问）

# Windows 文件名中不允许的字符 → 替换成外观相近的全角字符
INVALID_CHAR_MAP = {
    ":": "：", "*": "＊", "?": "？", '"': "＂",
    "<": "＜", ">": "＞", "|": "｜", "\\": "＼",
}
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} \
    | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CREATE_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)

IDM_DEFAULT_HINT = os.path.join(os.environ.get("ProgramFiles(x86)", "Program Files (x86)"),
                                "Internet Download Manager", "IDMan.exe")
PATH_EXAMPLE = "D" + ":" + os.sep + os.path.join("models", "a.safetensors")

FILTER_PRESETS = {
    "模型权重（safetensors ckpt pt pth bin gguf onnx）": "safetensors ckpt pt pth bin gguf onnx",
    "只要 safetensors": "safetensors",
    "GGUF 量化模型": "gguf",
    "配置文件（json yaml yml txt）": "json yaml yml txt",
    "说明文档（md txt）": "md txt",
    "图片（png jpg jpeg webp）": "png jpg jpeg webp",
    "压缩包（zip 7z rar tar gz）": "zip 7z rar tar gz",
    "数据集（parquet arrow csv jsonl json）": "parquet arrow csv jsonl json",
}
SIZE_UNITS = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}


# ============================================================
# 格式化
# ============================================================
def human_size(n, base=1024):
    """
    字节数 → 易读的大小。
    base=1024：Windows 的算法（1 KB = 1024 字节，资源管理器里看到的大小）
    base=1000：Hugging Face 网站的算法（1 KB = 1000 字节，国际单位制）
    """
    if n is None:
        return "未知"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < base or unit == "TB":
            return f"{int(n)} B" if unit == "B" else f"{n:.2f} {unit}"
        n /= base


def human_size_hf(n):
    """按 Hugging Face 网站的算法显示（1 GB = 1000³ 字节）"""
    return human_size(n, 1000)


def size_both(n):
    """两种算法一起显示：HF 20.20 GB ≈ Windows 18.81 GB（同一个文件，只是换算方式不同）"""
    if n is None:
        return "未知"
    if n < 1000:
        return f"{int(n)} B"
    return f"HF {human_size_hf(n)} ≈ Windows {human_size(n)}"


def fmt_duration(sec):
    """秒数 → 1小时02分 / 3分12秒 / 45秒"""
    if sec is None or sec < 0 or sec != sec:
        return "--"
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}小时{m:02d}分"
    if m:
        return f"{m}分{s:02d}秒"
    return f"{s}秒"


# ============================================================
# Windows 路径
# ============================================================
def sanitize_component(name):
    """
    把一段文件名/文件夹名转换成 Windows 可用的名称（只替换不允许的字符）。
    返回 (新名称, 是否被修改)
    """
    new = "".join(INVALID_CHAR_MAP.get(c, "_" if ord(c) < 32 else c) for c in name)
    stripped = new.rstrip(" .")
    if stripped != new:
        new = stripped + "_"
    if not new:
        new = "_"
    if new.split(".")[0].upper() in RESERVED_NAMES:
        new = "_" + new
    return new, new != name


def hf_name(hf_path):
    """HF 路径中的文件名（Windows 合法形式）"""
    return sanitize_component(hf_path.split("/")[-1])[0]


def complete_local_path(text, filename):
    """
    把用户填写的保存位置补全成“完整文件路径”：只填文件夹时自动补上原文件名。
    返回 (完整路径, 是否自动补了文件名)
    """
    t = (text or "").strip().strip('"').strip()
    if not t:
        return "", False
    drive, rest = os.path.splitdrive(t)
    is_dir = t.endswith(("\\", "/")) or not rest.strip("\\/") or os.path.isdir(t)
    if not is_dir:
        base = re.split(r"[\\/]", t.rstrip("\\/"))[-1]
        if not os.path.splitext(base)[1] and os.path.splitext(filename)[1]:
            is_dir = True
    if is_dir:
        return os.path.normpath(os.path.join(t if rest.strip("\\/") else drive + os.sep, filename)), True
    return os.path.normpath(t), False


def validate_local_path(path):
    """检查本地路径是否合法：合法返回 None，否则返回错误说明"""
    if not path:
        return "路径不能为空"
    if not os.path.isabs(path):
        return f"请填写完整路径（以盘符开头，如 {PATH_EXAMPLE}）"
    _drive, rest = os.path.splitdrive(path)
    parts = [p for p in re.split(r"[\\/]+", rest) if p]
    if not parts:
        return "请填写到具体的文件名"
    for p in parts:
        bad = sorted({c for c in p if c in '<>:"|?*' or ord(c) < 32})
        if bad:
            return f"“{p}” 中包含 Windows 不允许的字符：{' '.join(bad)}"
        if p != p.rstrip(" ."):
            return f"“{p}” 不能以空格或英文句点结尾"
        if p.split(".")[0].upper() in RESERVED_NAMES:
            return f"“{p}” 是 Windows 保留名称，不能使用"
    return None


def file_matches(path, size):
    """本地文件是否已存在且大小一致"""
    try:
        return size is not None and os.path.getsize(path) == size
    except OSError:
        return False


# ============================================================
# 筛选：后缀直接写 safetensors 即可，不需要 *. 和分号
# ============================================================
_TOKEN_RE = re.compile(r'"([^"]+)"|“([^”]+)”|([^\s,，;；、|]+)')


def split_tokens(text):
    """空格、逗号、分号、顿号都可以分隔；词里有空格时用双引号括起来"""
    out = []
    for m in _TOKEN_RE.finditer(text or ""):
        t = (m.group(1) or m.group(2) or m.group(3) or "").strip()
        if t:
            out.append(t)
    return out


def normalize_ext(token):
    t = token.strip().lower().lstrip("*")
    if not t or t == ".":
        return ""
    return t if t.startswith(".") else "." + t


def parse_size(text, default_unit="MB"):
    """500、1.5GB、200MB、800K、2G 都可以；留空返回 None；格式不对抛出 ValueError"""
    t = (text or "").strip().upper().replace(" ", "")
    if not t:
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([KMGT]?)(I?B)?", t)
    if not m:
        raise ValueError(f"无法识别的大小：{text}")
    num, letter, b = float(m.group(1)), m.group(2), m.group(3)
    if letter:
        mult = SIZE_UNITS[letter + "B"]
    elif b:
        mult = 1
    else:
        mult = SIZE_UNITS.get(default_unit, SIZE_UNITS["MB"])
    return int(num * mult)


def _has_wildcard(t):
    return any(c in t for c in "*?[")


def _kw_hit(token, text):
    if _has_wildcard(token):
        return fnmatch.fnmatchcase(text, token)
    return token in text


def make_matcher(ext_text="", name_text="", path_text="", exclude_text="", min_size=None, max_size=None):
    """
    生成筛选函数 match(hf路径, 文件大小)。不同条件之间是“并且”；同一格里的多个词是“或者”。
    """
    exts = [e for e in (normalize_ext(t) for t in split_tokens(ext_text)) if e]
    names = [t.lower() for t in split_tokens(name_text)]
    paths = [t.lower().replace("\\", "/") for t in split_tokens(path_text)]
    excludes = [t.lower().replace("\\", "/") for t in split_tokens(exclude_text)]

    def match(hf_path, size):
        p = hf_path.lower()
        name = p.rsplit("/", 1)[-1]
        if exts and not any(name.endswith(e) for e in exts):
            return False
        if names and not any(_kw_hit(t, name) for t in names):
            return False
        if paths and not any(_kw_hit(t, p) for t in paths):
            return False
        if min_size is not None and (size is None or size < min_size):
            return False
        if max_size is not None and (size is None or size > max_size):
            return False
        for t in excludes:
            if t.startswith(".") and not _has_wildcard(t):
                if name.endswith(t):
                    return False
            elif _has_wildcard(t):
                if fnmatch.fnmatchcase(name, t) or fnmatch.fnmatchcase(p, t):
                    return False
            elif t in p:
                return False
        return True

    match.active = bool(exts or names or paths or excludes or min_size is not None or max_size is not None)
    return match


def matcher_from_filters(f):
    """根据网页上的筛选条件生成匹配函数；大小格式不对抛出 ValueError"""
    f = f or {}
    unit = f.get("unit") or "MB"
    mn = parse_size(f.get("min"), unit)
    mx = parse_size(f.get("max"), unit)
    if mn is not None and mx is not None and mn > mx:
        raise ValueError("最小值不能大于最大值")
    return make_matcher(f.get("ext", ""), f.get("name", ""), f.get("path", ""), f.get("exclude", ""), mn, mx)


# ============================================================
# 网络
# ============================================================
def make_headers(token=None):
    headers = {"User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = "Bearer " + token   # Token 只放在请求头里
    return headers


def http_get_json(url, token=None, timeout=60):
    req = urllib.request.Request(url, headers=make_headers(token))
    with net.opener_for(url).open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8")), resp.headers


def explain_http_error(e):
    code = getattr(e, "code", None)
    if code == 401:
        return "401 未授权：这是私有/受限仓库，需要有效的 HF Token"
    if code == 403:
        return "403 拒绝访问：Token 没有权限，或你还没在网页上同意该仓库的使用协议"
    if code == 404:
        return "404 未找到：仓库ID / 仓库类型 / 分支填写有误，或者是私有仓库（未带 Token 时也显示 404）"
    if code == 429:
        return "429 请求过多：被限流了，请稍后再试或调低并发数"
    return f"HTTP {code}：{e}"


def build_resolve_url(endpoint, repo_type, repo_id, revision, hf_path):
    """文件的固定下载地址（不带 Token、不会过期）"""
    return (f"{endpoint}/{URL_PREFIX[repo_type]}{quote(repo_id, safe='/')}"
            f"/resolve/{quote(revision, safe='')}/{quote(hf_path, safe='/')}")


def _fetch_tree(base, revision, token):
    """用 tree 接口逐页获取所有文件（备用方案）"""
    url = f"{base}/tree/{quote(revision, safe='')}?recursive=true&expand=false"
    files = []
    while url:
        data, headers = http_get_json(url, token)
        for e in data:
            if e.get("type") != "file":
                continue
            lfs = e.get("lfs") or None
            files.append({"path": e.get("path"), "size": lfs.get("size") if lfs else e.get("size"),
                          "lfs": bool(lfs)})
        url = None
        m = re.search(r'<([^>]+)>\s*;\s*rel="next"', headers.get("Link", "") or "")
        if m:
            url = urljoin(base, m.group(1))
    return [f for f in files if f["path"]]


def fetch_repo_files(endpoint, repo_type, repo_id, revision, token=None, log=print):
    """获取仓库信息和全部文件列表（含所有子文件夹）。返回 (仓库信息dict, 文件列表)"""
    base = f"{endpoint}/api/{API_TYPES[repo_type]}/{quote(repo_id, safe='/')}"
    info, _ = http_get_json(f"{base}/revision/{quote(revision, safe='')}?blobs=true", token)
    files = []
    for s in info.get("siblings") or []:
        path = s.get("rfilename")
        if not path:
            continue
        lfs = s.get("lfs") or None
        files.append({"path": path, "size": lfs.get("size") if lfs else s.get("size"), "lfs": bool(lfs)})
    if not files or any(f["size"] is None for f in files):      # 部分镜像不返回文件大小
        try:
            tree_files = _fetch_tree(base, revision, token)
            if tree_files:
                files = tree_files
        except Exception as e:
            if not files:
                raise
            log(f"[提示] tree 接口获取失败，部分文件大小会显示为“未知”：{e}")
    files.sort(key=lambda f: f["path"])
    return info, files


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """不自动跟随跳转，用来查看跳转目标"""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _is_official_host(host):
    return host == "huggingface.co" or host.endswith(".huggingface.co") or host == "hf.co"


def mirror_redirect_target(url):
    """镜像站是否把下载请求转到了官方网站：是则返回官方域名，否则 None"""
    opener = net.opener_for(url, NoRedirect)
    req = urllib.request.Request(url, method="HEAD",
                                 headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"})
    try:
        with opener.open(req, timeout=20):
            return None
    except urllib.error.HTTPError as e:
        loc = e.headers.get("Location") if e.code in (301, 302, 303, 307, 308) else None
        e.close()
        if loc:
            host = urlparse(urljoin(url, loc)).netloc.lower()
            if _is_official_host(host):
                return host
    except Exception:
        pass
    return None


def resolve_direct_link(url, token=None, max_hops=6):
    """
    带 Token 访问 resolve 地址，取得 CDN 上带签名的“临时直链”。
    返回 None 表示服务器直接返回了文件内容（普通小文件）。
    """
    opener = net.opener_for(url, NoRedirect)
    hub_hosts = {urlparse(url).netloc.lower(), "huggingface.co", "hf.co"}
    cur = url
    for _ in range(max_hops):
        same_site = urlparse(cur).netloc.lower() in hub_hosts     # 只对仓库站点发送 Token
        req = urllib.request.Request(cur, method="HEAD", headers=make_headers(token if same_site else None))
        try:
            with opener.open(req, timeout=60):
                return None
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                loc = e.headers.get("Location")
                e.close()
                if not loc:
                    raise
                nxt = urljoin(cur, loc)
                if urlparse(nxt).netloc.lower() not in hub_hosts:
                    return nxt
                cur = nxt
                continue
            raise
    raise RuntimeError("跳转次数过多")


def download_with_token(url, token, dest, stop_event=None):
    """本工具直接下载（受限仓库里没有直链的小文件）"""
    tmp = dest + ".part"
    req = urllib.request.Request(url, headers=make_headers(token))
    with net.opener_for(url).open(req, timeout=120) as resp, open(tmp, "wb") as f:
        while True:
            if stop_event is not None and stop_event.is_set():
                raise InterruptedError("用户已停止")
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
    os.replace(tmp, dest)


# ============================================================
# IDM / 进程
# ============================================================
def detect_idm():
    """自动查找 IDMan.exe：先查注册表，再查默认安装目录"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\DownloadManager") as k:
            p, _ = winreg.QueryValueEx(k, "ExePath")
            if p and os.path.isfile(p):
                return p
    except Exception:
        pass
    for env in ("ProgramFiles(x86)", "ProgramFiles"):
        base = os.environ.get(env)
        if base:
            p = os.path.join(base, "Internet Download Manager", "IDMan.exe")
            if os.path.isfile(p):
                return p
    return ""


def is_process_running(image_name):
    try:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {image_name}"],
                             capture_output=True, timeout=15, creationflags=CREATE_NO_WINDOW)
        return image_name.lower() in out.stdout.decode("latin-1", errors="ignore").lower()
    except Exception:
        return False


# ============================================================
# 仓库标识 / 地址识别
# ============================================================
def repo_key(repo, repo_type, revision):
    return f"{repo_type}:{repo}@{revision}"


HF_HOSTS = {"huggingface.co", "www.huggingface.co", "hf.co"} | {urlparse(u).netloc.lower() for u in ENDPOINTS.values()}
_HF_NOT_REPO = {"api", "docs", "settings", "login", "join", "blog", "papers", "pricing", "organizations",
                "collections", "models", "datasets", "spaces", "tasks", "learn", "posts", "new", "chat", "search"}


def parse_hf_url(url):
    """
    文件地址 → ("file", (仓库, 类型, 分支), 文件路径)；仓库地址 → ("repo", (仓库, 类型, 分支), None)；其他 → None
    """
    try:
        u = urlparse(url)
    except Exception:
        return None
    if u.netloc.lower() not in HF_HOSTS:
        return None
    m = re.match(r"^/(?:(datasets|spaces)/)?([^/]+)/([^/]+)/(?:resolve|blob|raw)/([^/]+)/(.+)$", u.path)
    if m:
        if not m.group(1) and m.group(2) in _HF_NOT_REPO:
            return None
        rtype = {"datasets": "dataset", "spaces": "space"}.get(m.group(1), "model")
        repo = unquote(m.group(2)) + "/" + unquote(m.group(3))
        return "file", (repo, rtype, unquote(m.group(4))), unquote(m.group(5))
    m = re.match(r"^/(?:(datasets|spaces)/)?([^/]+)/([^/]+?)(?:/tree/([^/]+)(?:/.*)?)?/?$", u.path)
    if m:
        if not m.group(1) and m.group(2) in _HF_NOT_REPO:
            return None
        rtype = {"datasets": "dataset", "spaces": "space"}.get(m.group(1), "model")
        repo = unquote(m.group(2)) + "/" + unquote(m.group(3))
        return "repo", (repo, rtype, unquote(m.group(4) or "main")), None
    return None


def url_filename(url):
    try:
        name = unquote(urlparse(url).path.rstrip("/").rsplit("/", 1)[-1])
    except Exception:
        name = ""
    return name or "download"


def parse_repo_input(text, default_type, default_rev):
    """解析输入框：支持多个仓库ID、仓库网页地址、文件下载地址。返回 (specs, 无法识别的内容)"""
    specs, bad = [], []
    for t in split_tokens(text):
        rtype, rev, repo = default_type, default_rev, t.strip().strip("/")
        m = re.match(r"https?://[^/]+/(?:(datasets|spaces)/)?([^/\s]+/[^/?#\s]+)", repo)
        if m:
            if m.group(1):
                rtype = m.group(1)[:-1]
            repo = m.group(2)
            m2 = re.search(r"/(?:tree|blob|resolve)/([^/?#]+)", t)
            if m2:
                rev = unquote(m2.group(1))
        if repo.count("/") != 1:
            bad.append(t)
            continue
        spec = (repo, rtype, rev)
        if spec not in specs:
            specs.append(spec)
    return specs, bad
