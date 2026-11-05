#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成图片/视频的自托管缓存 —— 把上游 CDN 的字节抓下来,放到自家域名下短期托管。

为什么需要:生成型渠道(clipfly / oreate)返回的是**上游 CDN 的 URL**。这些链接
对调用方并不可靠 —— clipfly 的图带 referer 防盗链,直接 GET 会被拒;oreate 那条
被改写成 https://ai.baipiao.co/media/... 而网关根本没有 /media 路由。于是客户端拿到
一个 200 + 一串打不开的链接,就是「能生成但没返回过来」。

做法:服务端一次性把字节下载到 <MEDIA_DIR>,用随机 token 命名,对外给
<SITE_URL>/media/gen/<token>.<ext>。调用方从我们域名取,不再依赖上游是否防盗链。
文件按 TTL(默认 30 分钟)过期,由主服务的 housekeeping 循环调 sweep() 清理 ——
生成结果是一次性的,拿走就没用了,不留长期存储。

单 worker 进程,磁盘目录 + 内存无状态:token 即文件名,重启后靠 sweep 按 mtime
兜底清理,不依赖任何内存索引。
"""
import os
import http.client
import ipaddress
import secrets
import socket
import time
import urllib.request
from urllib.parse import urlsplit

import config

# 上游允许的图片/视频类型 → 落盘扩展名。白名单也是安全边界:不把上游随便给的
# Content-Type 当扩展名写盘,避免落一个 .html/.svg 之类可被当页面加载的东西。
_EXT_BY_MIME = {
    "image/png": "png", "image/jpeg": "jpg", "image/webp": "webp",
    "image/gif": "gif", "video/mp4": "mp4", "video/webm": "webm",
    "video/quicktime": "mov",
}
_ALLOWED_EXT = set(_EXT_BY_MIME.values())

_MIME_BY_EXT = {v: k for k, v in _EXT_BY_MIME.items()}

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")

_MAX_BYTES = 64 * 1024 * 1024   # 单文件上限:挡住上游异常返回一个超大响应打爆磁盘


def _public_socket(host, port, timeout):
    """解析一次并连接到已校验的数值地址，避免二次 DNS 查询产生重绑定。"""
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    for _family, _kind, _proto, _canon, sockaddr in addresses:
        ip = ipaddress.ip_address(sockaddr[0])
        ip = ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped else ip
        if not ip.is_global or ip.is_multicast:
            raise ValueError("media URL must resolve to public addresses")
    error = None
    for _family, _kind, _proto, _canon, sockaddr in addresses:
        try:
            return socket.create_connection((sockaddr[0], port), timeout)
        except OSError as exc:
            error = exc
    raise error or ValueError("media host has no address")


class _PublicHTTPConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = _public_socket(self.host, self.port, self.timeout)


class _PublicHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        sock = _public_socket(self.host, self.port, self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


class _PublicHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_PublicHTTPConnection, req)


class _PublicHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_PublicHTTPSConnection, req)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, _fp, code, msg, headers, _newurl):
        # 签名由 urllib 的基类定死(它按位置调),用不到的参数加下划线标明是刻意不用
        raise ValueError("media redirects are not allowed")


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}),
    _PublicHTTPHandler(), _PublicHTTPSHandler(), _NoRedirect())


def resolve_dir():
    """当前生效的媒体缓存目录。留空则放库文件旁的 media/(跟着数据走,不跟代码走,
    deploy 覆盖代码不会清掉它)。"""
    return config.MEDIA_DIR or os.path.join(
        os.path.dirname(os.path.abspath(config.DB_PATH)), "media")


def _ext_from(url, content_type):
    """定扩展名:优先 Content-Type,回落 URL 后缀,都不认就当 png(图片是主路径)。"""
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in _EXT_BY_MIME:
        return _EXT_BY_MIME[ct]
    tail = url.rsplit(".", 1)[-1].split("?")[0].lower() if "." in url else ""
    if tail in _ALLOWED_EXT:
        return tail
    return "png"


def fetch_and_store(url, referer=None, timeout=60):
    """下载 url 的字节,落盘,返回 (token_filename, abs_path, mime)。失败抛异常。

    referer:上游若做 referer 防盗链(clipfly),传对应站点根;None 则不带。
    """
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("invalid media URL")
    headers = {"User-Agent": _UA, "Accept": "*/*"}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with _OPENER.open(req, timeout=timeout) as r:
        ct = r.headers.get("Content-Type", "")
        data = r.read(_MAX_BYTES + 1)
    if len(data) > _MAX_BYTES:
        raise RuntimeError("media too large: > %d bytes" % _MAX_BYTES)
    if not data:
        raise RuntimeError("media empty from upstream")
    ext = _ext_from(url, ct)
    name = secrets.token_urlsafe(16) + "." + ext
    dest = resolve_dir()
    os.makedirs(dest, exist_ok=True)
    path = os.path.join(dest, name)
    # 先写临时名再原子改名:sweep 或并发读不会看到半截文件。
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return name, path, _MIME_BY_EXT.get(ext, "application/octet-stream")


def public_url(name):
    """token 文件名 → 对外 URL。"""
    return config.SITE_URL.rstrip("/") + "/media/gen/" + name


def read(name):
    """按 token 取文件,返回 (abs_path, mime) 或 None。token 即文件名,校验它不含
    路径分隔符(只认单层文件名),挡目录穿越。"""
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in _ALLOWED_EXT:
        return None
    path = os.path.join(resolve_dir(), name)
    if not os.path.isfile(path):
        return None
    try:
        if time.time() - os.path.getmtime(path) > config.MEDIA_TTL:
            return None
    except OSError:
        return None
    return path, _MIME_BY_EXT.get(ext, "application/octet-stream")


def sweep(now=None):
    """删掉超过 TTL 的文件,返回删除数。只动本模块认得的扩展名 + .part 残留。"""
    now = now or time.time()
    dest = resolve_dir()
    if not os.path.isdir(dest):
        return 0
    ttl = config.MEDIA_TTL
    removed = 0
    for name in os.listdir(dest):
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in _ALLOWED_EXT and not name.endswith(".part"):
            continue
        path = os.path.join(dest, name)
        try:
            if now - os.path.getmtime(path) > ttl:
                os.remove(path)
                removed += 1
        except OSError:
            continue
    return removed
