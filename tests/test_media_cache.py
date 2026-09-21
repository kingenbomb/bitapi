"""生成媒体自托管缓存 —— 抓字节、按 token 服务、TTL 清理、目录穿越防护。

生图/生视频渠道返回的是上游 CDN 链接(带防盗链、可能 404)。网关把字节抓到自家
/media/gen 下短期托管,客户端从自家域名取。这组测试锁住:token 服务只认单层文件名、
只认白名单扩展名、sweep 按 TTL 删、response_format 决定 url 还是 b64。
"""
import os
import tempfile
import time
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ["BITAPI_DB"] = os.path.join(_TMP, "media.db")
os.environ["BITAPI_MEDIA_DIR"] = os.path.join(_TMP, "media")
os.environ["BITAPI_JWT_SECRET"] = "media-secret-xxxx"
os.environ["BITAPI_API_KEY"] = "sk-media-master"

import config  # noqa: E402
from core import media_cache  # noqa: E402


class StoreReadTest(unittest.TestCase):
    def setUp(self):
        # 每个用例一个干净目录,互不干扰。
        config.MEDIA_DIR = os.path.join(_TMP, "media-" + str(id(self)))
        config.MEDIA_TTL = 1800

    def _fake_download(self, data=b"\x89PNG\r\n\x1a\n", ct="image/png"):
        """拦住真实网络:urlopen 返回一个给定字节 + Content-Type 的假响应。"""
        class _Resp:
            headers = {"Content-Type": ct}

            def read(self, _n=None):
                return data

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return mock.patch("urllib.request.urlopen", return_value=_Resp())

    def test_fetch_stores_and_serves_by_token(self):
        with self._fake_download():
            name, path, mime = media_cache.fetch_and_store("https://cdn/x.png")
        self.assertTrue(name.endswith(".png"))
        self.assertEqual(mime, "image/png")
        self.assertTrue(os.path.isfile(path))
        hit = media_cache.read(name)
        self.assertIsNotNone(hit)
        self.assertEqual(hit[0], path)
        self.assertEqual(hit[1], "image/png")

    def test_public_url_uses_site_root(self):
        config.SITE_URL = "https://ai.example.co/"
        self.assertEqual(media_cache.public_url("abc.png"),
                         "https://ai.example.co/media/gen/abc.png")

    def test_content_type_wins_over_url_suffix(self):
        # URL 后缀说 .txt,但上游 Content-Type 是 jpeg —— 以 mime 为准,不落 .txt。
        with self._fake_download(ct="image/jpeg"):
            name, _p, mime = media_cache.fetch_and_store("https://cdn/a.txt")
        self.assertTrue(name.endswith(".jpg"))
        self.assertEqual(mime, "image/jpeg")

    def test_read_rejects_path_traversal(self):
        self.assertIsNone(media_cache.read("../config.py"))
        self.assertIsNone(media_cache.read("a/b.png"))
        self.assertIsNone(media_cache.read("..\\x.png"))
        self.assertIsNone(media_cache.read(".hidden.png"))

    def test_read_rejects_non_whitelisted_ext(self):
        # 即便文件真存在,非白名单扩展名也不服务(挡住把 .html 当页面加载)。
        os.makedirs(config.MEDIA_DIR, exist_ok=True)
        with open(os.path.join(config.MEDIA_DIR, "evil.html"), "w") as f:
            f.write("<h1>hi</h1>")
        self.assertIsNone(media_cache.read("evil.html"))

    def test_read_missing_is_none(self):
        self.assertIsNone(media_cache.read("nope-does-not-exist.png"))

    def test_empty_upstream_raises(self):
        with self._fake_download(data=b""):
            with self.assertRaises(RuntimeError):
                media_cache.fetch_and_store("https://cdn/x.png")

    def test_sweep_deletes_only_expired(self):
        with self._fake_download():
            fresh, _p, _m = media_cache.fetch_and_store("https://cdn/fresh.png")
            old, old_path, _m = media_cache.fetch_and_store("https://cdn/old.png")
        # 把一个文件的 mtime 拨到 TTL 之前。
        past = time.time() - config.MEDIA_TTL - 10
        os.utime(old_path, (past, past))
        removed = media_cache.sweep()
        self.assertEqual(removed, 1)
        self.assertIsNone(media_cache.read(old))
        self.assertIsNotNone(media_cache.read(fresh))

    def test_sweep_empty_dir_is_zero(self):
        config.MEDIA_DIR = os.path.join(_TMP, "never-created")
        self.assertEqual(media_cache.sweep(), 0)


if __name__ == "__main__":
    unittest.main()
