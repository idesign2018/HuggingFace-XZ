# -*- coding: utf-8 -*-
"""
文件筛选（网页版）：把“文件后缀 / 文件名包含 / 路径·文件夹包含 / 文件大小 / 排除”合并到一个输入框。

写法（空格分隔，可以混写）：
  .safetensors  *.gguf  ext:json        → 文件后缀
  fp16  lora  *q4_k*                    → 文件名包含（可用通配符 * ?）
  dir:vae  path:unet  text_encoder/     → 路径/文件夹包含（带 / 的词也算路径）
  >1GB  <500MB  >=2G  100MB~2GB         → 文件大小（只写数字按 MB）
  -test  -.md  !*demo*                  → 排除（前面加 - 或 !）
  "my model"                            → 词里有空格时用双引号
规则和旧版完全相同：同一类条件之间是“或者”，不同类条件之间是“并且”。
中文关键字也可以用：后缀: 文件名: 路径: 文件夹: 大小: 排除:
"""
import re
import fnmatch

from core.common import split_tokens, normalize_ext, parse_size, human_size

_RAW = re.compile(r'(?:[^\s"“”]+|"[^"]*"?|“[^”]*”?)+')
_KEYED = re.compile(r'^([A-Za-z\u4e00-\u9fff]+)[:：](.*)$', re.S)
_CMP = re.compile(r'^(>=|<=|≥|≤|>|<)\s*(.+)$')
_RANGE = re.compile(r'^(.+?)(?:~|～|\.\.)(.+)$')
_EXT = re.compile(r'^\*?\.[^\s.*?/\\\[\]]+$')
_SEP = re.compile(r'[,，;；、|]+')
_UNIT = re.compile(r'^(?:[KMGT]I?B?|B)$', re.I)
_NUM_END = re.compile(r'(?:^|[<>=≥≤~～:：]|\.\.)\d+(?:\.\d+)?$')
_OP_ONLY = re.compile(r'^(?:(?:size|大小)[:：])?(?:>=|<=|≥|≤|>|<)?$', re.I)
_BARE_SIZE = re.compile(r'^\d+(?:\.\d+)?[KMGT](?:I?B)?$', re.I)     # 10gb、1.5G、500MB（必须带单位）
# 不带点也按“后缀”处理的常见文件类型（如 md、safetensors）；想按文件名包含筛选时写 name:md
KNOWN_EXTS = {
    "safetensors", "gguf", "ckpt", "pt", "pth", "bin", "onnx", "msgpack", "h5", "tflite", "npz", "npy", "pkl",
    "json", "jsonl", "yaml", "yml", "md", "txt", "jinja", "csv", "parquet", "arrow",
    "png", "jpg", "jpeg", "webp", "gif", "zip", "7z", "rar", "tar", "gz",
}

KEYS = {
    "ext": "ext", "type": "ext", "后缀": "ext", "类型": "ext", "扩展名": "ext",
    "name": "name", "文件名": "name", "名称": "name",
    "dir": "path", "path": "path", "folder": "path", "路径": "path", "文件夹": "path", "目录": "path",
    "size": "size", "大小": "size",
    "not": "exclude", "exclude": "exclude", "排除": "exclude",
}


def _unquote(t):
    return t.replace('"', "").replace("“", "").replace("”", "")


def _needs_join(prev, cur):
    """把被空格拆开的写法合并回来：“> 1GB”、“>1 GB”、“1GB ~ 2GB”、“- test”"""
    if prev in ("-", "!", "！"):
        return True
    if _OP_ONLY.match(prev):
        return True
    if prev.endswith(("~", "～", "..")) or cur in ("~", "～", ".."):
        return True
    return bool(_UNIT.match(cur) and _NUM_END.search(prev))


def _tokens(q):
    out = []
    for t in _RAW.findall(q or ""):
        if out and _needs_join(out[-1], t):
            out[-1] += t
        else:
            out.append(t)
    return out


def _guess(p):
    if _EXT.match(p) or p.lower() in KNOWN_EXTS:
        return "ext"
    if "/" in p or "\\" in p:
        return "path"
    return "name"


def _is_size(t, unit):
    if _CMP.match(t) or _BARE_SIZE.match(t):      # 只写 10gb = 至少 10GB
        return True
    m = _RANGE.match(t)
    if m:       # 左边是大小（如 1GB~xx）就按大小处理，右边写错时会提示格式不对
        try:
            return parse_size(m.group(1), unit) is not None
        except ValueError:
            return False
    return False


def _apply_size(r, t, unit):
    m = _CMP.match(t)
    if m:
        v = parse_size(m.group(2), unit)
        if v is None:
            raise ValueError(f"无法识别的大小：{t}")
        if m.group(1) in (">", ">=", "≥"):
            r["min"] = v
        else:
            r["max"] = v
        return
    m = _RANGE.match(t)
    if m:
        a, b = parse_size(m.group(1), unit), parse_size(m.group(2), unit)
        if a is None or b is None:
            raise ValueError(f"无法识别的大小：{t}")
        r["min"], r["max"] = min(a, b), max(a, b)
        return
    v = parse_size(t, unit)          # size:1GB → 至少 1GB
    if v is None:
        raise ValueError(f"无法识别的大小：{t}")
    r["min"] = v


def parse_query(q, default_unit="MB"):
    """把输入框内容拆成各类条件。大小格式不对时抛出 ValueError"""
    r = {"ext": [], "name": [], "path": [], "exclude": [], "min": None, "max": None}
    for raw in _tokens(q):
        t, neg = raw, False
        if len(t) > 1 and t[0] in "-!！":
            t, neg = t[1:], True
        key = None
        m = _KEYED.match(t)
        if m and m.group(1).lower() in KEYS:
            key, t = KEYS[m.group(1).lower()], m.group(2)
        quoted = any(c in t for c in '"“”')
        t = _unquote(t).strip()
        if not t:
            continue
        if key == "size" or (key is None and not quoted and _is_size(t, default_unit)):
            _apply_size(r, t, default_unit)
            continue
        parts = [t] if quoted else [x.strip() for x in _SEP.split(t) if x.strip()]
        for p in parts:
            kind = key or _guess(p)
            if neg or kind == "exclude":
                e = normalize_ext(p) if kind == "ext" else p.lower().replace("\\", "/")
                if e:
                    r["exclude"].append(e)
            elif kind == "ext":
                e = normalize_ext(p)
                if e:
                    r["ext"].append(e)
            elif kind == "path":
                r["path"].append(p.lower().replace("\\", "/"))
            else:
                r["name"].append(p.lower())
    if r["min"] is not None and r["max"] is not None and r["min"] > r["max"]:
        raise ValueError("最小值不能大于最大值")
    return r


def _has_wildcard(t):
    return any(c in t for c in "*?[")


def _kw_hit(token, text):
    if _has_wildcard(token):
        return fnmatch.fnmatchcase(text, token)
    return token in text


def build_matcher(r):
    """生成筛选函数 match(hf路径, 文件大小)；规则和旧版五个输入框完全相同"""
    exts, names, paths, excludes = r["ext"], r["name"], r["path"], r["exclude"]
    mn, mx = r["min"], r["max"]

    def match(hf_path, size):
        p = hf_path.lower()
        name = p.rsplit("/", 1)[-1]
        if exts and not any(name.endswith(e) for e in exts):
            return False
        if names and not any(_kw_hit(t, name) for t in names):
            return False
        if paths and not any(_kw_hit(t, p) for t in paths):
            return False
        if mn is not None and (size is None or size < mn):
            return False
        if mx is not None and (size is None or size > mx):
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

    match.active = bool(exts or names or paths or excludes or mn is not None or mx is not None)
    return match


def describe(r):
    """把条件整理成网页上显示的小标签：[[类别, 内容], ...]"""
    out = []
    if r["ext"]:
        out.append(["后缀", " / ".join(r["ext"])])
    if r["name"]:
        out.append(["文件名含", " / ".join(r["name"])])
    if r["path"]:
        out.append(["路径含", " / ".join(r["path"])])
    mn, mx = r["min"], r["max"]      # 筛选里写的 GB/MB 按 Windows 算法（1 GB = 1024³ 字节）
    w = lambda n: f"{human_size(n)}（HF {human_size(n, 1000)}）"
    if mn is not None and mx is not None:
        out.append(["大小", f"{w(mn)} ~ {w(mx)}"])
    elif mn is not None:
        out.append(["大小", f"≥ {w(mn)}"])
    elif mx is not None:
        out.append(["大小", f"≤ {w(mx)}"])
    if r["exclude"]:
        out.append(["排除", " / ".join(r["exclude"])])
    return out


def _q(tok):
    return f'"{tok}"' if re.search(r"\s", tok) else tok


def legacy_to_query(f):
    """旧版五个输入框的内容 → 合并后的一行写法（读取旧配置时使用）"""
    f = f or {}
    parts = []
    for t in split_tokens(f.get("ext")):
        e = normalize_ext(t)
        if e:
            parts.append(e)
    for t in split_tokens(f.get("name")):
        ambiguous = (_EXT.match(t) or t.lower() in KNOWN_EXTS or "/" in t or "\\" in t
                     or t[0] in "-!！<>≥≤" or _KEYED.match(t) or _is_size(t, "MB"))
        parts.append("name:" + _q(t) if ambiguous else _q(t))
    for t in split_tokens(f.get("path")):
        plain = ("/" in t or "\\" in t) and t[0] not in "-!！"
        parts.append(_q(t) if plain else "dir:" + _q(t))
    for t in split_tokens(f.get("exclude")):
        parts.append("-" + _q(t))
    unit = f.get("unit") or "MB"
    for key, op in (("min", ">="), ("max", "<=")):
        v = str(f.get(key) or "").strip().replace(" ", "")
        if v:
            parts.append(op + (v + unit if re.fullmatch(r"\d+(?:\.\d+)?", v) else v))
    return " ".join(parts)


def query_of(f):
    """网页提交的筛选条件 → 输入框内容（兼容旧版的五个字段）"""
    f = f or {}
    if "q" in f:
        return str(f.get("q") or "")
    return legacy_to_query(f)


def matcher_from_filters(f):
    """根据筛选条件生成匹配函数；大小格式不对抛出 ValueError"""
    return build_matcher(parse_query(query_of(f)))


def inspect_filters(f):
    """返回 (匹配函数, 小标签列表)；大小格式不对抛出 ValueError"""
    r = parse_query(query_of(f))
    return build_matcher(r), describe(r)
