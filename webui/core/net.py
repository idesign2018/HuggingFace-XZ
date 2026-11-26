# -*- coding: utf-8 -*-
"""
网络出口：下载源（国内镜像 / 官方）和代理，两个页面共用（左侧导航栏的“源”按钮）。

规则：
  - 国内镜像 hf-mirror.com：直连，不走任何代理（系统代理开着也不走）
  - 官方 huggingface.co：自动走代理（国内直连官方经常超时）
按“请求从哪个网站开始”判断：从 huggingface.co / hf.co 开始的请求，连同它跳转到的下载服务器都走代理；
其余一律直连。

代理从哪来（按顺序，找到能连上的就用）：
  1. Windows 系统代理（代理软件打开了“系统代理”）
  2. 环境变量 HTTPS_PROXY / HTTP_PROXY
  3. 系统代理设置里登记过、但现在没打开的地址（Clash / v2rayN 关掉“系统代理”后地址还留在设置里）
都连不上就直连，并在日志 / 提示里说明。
"""
import os
import socket
import threading
import time
import urllib.request
from urllib.parse import urlparse

OFFICIAL_HOSTS = ("huggingface.co", "hf.co")
MIRROR_HOST = "hf-mirror.com"
CACHE_SEC = 30                      # 代理检测结果缓存这么多秒（每个请求都查一遍太慢）
PROXY_ENV = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")
_lock = threading.Lock()
_cache = {"t": 0.0, "info": None}


def _host(url):
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def is_official(url):
    h = _host(url)
    return any(h == d or h.endswith("." + d) for d in OFFICIAL_HOSTS)


def is_mirror(url):
    h = _host(url)
    return h == MIRROR_HOST or h.endswith("." + MIRROR_HOST)


def _registry():
    """Windows 系统代理设置：(开关是否打开, 代理地址原文)"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as k:
            def q(name):
                try:
                    return winreg.QueryValueEx(k, name)[0]
                except OSError:
                    return None
            return bool(q("ProxyEnable")), str(q("ProxyServer") or "").strip()
    except Exception:
        return False, ""


def _http_addr(server):
    """ProxyServer 可能是 “host:port”，也可能是 “http=…;https=…;socks=…”：取 HTTP(S) 代理"""
    if not server:
        return ""
    addr = server
    if "=" in server:
        parts = {}
        for p in server.split(";"):
            if "=" in p:
                a, b = p.split("=", 1)
                parts[a.strip().lower()] = b.strip()
        addr = parts.get("https") or parts.get("http") or ""
    addr = addr.strip()
    if not addr:
        return ""
    return addr if "://" in addr else "http://" + addr


def _reachable(proxy, timeout=1.0):
    """代理端口能不能连上（1 秒）：代理软件没开时别让下载干等超时"""
    try:
        u = urlparse(proxy)
        with socket.create_connection((u.hostname, u.port or 80), timeout=timeout):
            return True
    except Exception:
        return False


def official_proxy(refresh=False):
    """官方源用的代理：{"url": 代理地址（找不到时为空）, "text": 一句话说明}"""
    with _lock:
        if not refresh and _cache["info"] and time.time() - _cache["t"] < CACHE_SEC:
            return dict(_cache["info"])
    enabled, server = _registry()
    env = next((os.environ[k] for k in PROXY_ENV[:4] if os.environ.get(k)), "")
    cands = []
    if enabled:
        cands.append((_http_addr(server), "系统代理"))
    if env:
        cands.append((env if "://" in env else "http://" + env, "环境变量"))
    if not enabled:
        cands.append((_http_addr(server), "系统代理设置里登记的地址，系统代理开关没开"))
    tried, info = [], None
    for url, src in cands:
        if not url or url in tried:
            continue
        tried.append(url)
        if _reachable(url):
            info = {"url": url, "text": f"官方源走代理 {url}（{src}）"}
            break
    if info is None:
        info = {"url": "", "text": (f"官方源：代理 {'、'.join(tried)} 连不上（代理软件可能没开），只能直连，可能会超时"
                                    if tried else "官方源：没有找到代理（系统代理没开，也没登记代理地址），只能直连，可能会超时")}
    with _lock:
        _cache.update(t=time.time(), info=dict(info))
    return info


def describe(source):
    """给界面 / 日志的一句话：当前下载源怎么连"""
    if source == "official":
        return official_proxy(refresh=True)["text"]
    return "国内镜像 hf-mirror.com：直连，不走代理"


def proxies_for(url):
    """这个请求用的代理（给 urllib 的 ProxyHandler）。空字典 = 直连"""
    if is_official(url):
        p = official_proxy()["url"]
        if p:
            return {"http": p, "https": p}
    return {}


def opener_for(url, *handlers):
    """按请求的起点网站建 opener：官方走代理，其余直连（不受系统代理开关影响）；跳转也用同一个 opener"""
    return urllib.request.build_opener(urllib.request.ProxyHandler(proxies_for(url)), *handlers)


def apply_env(official):
    """下载子进程启动时调用（必须在导入 huggingface_hub / hf_xet 之前）：它们读环境变量里的代理，
    这样它们也遵守同样的规则。返回说明文字"""
    info = official_proxy(refresh=True) if official else None    # 先检测（会读到原来的环境变量），再改
    for k in PROXY_ENV + ("NO_PROXY", "no_proxy"):
        os.environ.pop(k, None)
    if not official:
        os.environ["NO_PROXY"] = "*"
        return describe("mirror")
    if info["url"]:
        os.environ["HTTPS_PROXY"] = os.environ["HTTP_PROXY"] = info["url"]
        os.environ["NO_PROXY"] = "localhost"
    return info["text"]
