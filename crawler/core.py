# -*- coding: utf-8 -*-
"""
crawler.core — 通用爬虫核心
=============================
职责：请求发送、重试、限速、UA 伪装、本地缓存、日志。
不关心具体网站，任何爬虫任务都可以复用这个 HttpClient。

用法：
    from crawler.core import HttpClient
    client = HttpClient(interval=1.0, cache_dir="data/cache")
    data = client.get_json("https://api.example.com/data")

要点：
- 所有请求带随机 UA，降低被识别为脚本的概率
- 失败自动重试（默认 3 次，指数退避）
- interval 控制两次请求的最小间隔，避免打爆对方服务器
- cache_dir 开启后按 URL 哈希缓存响应体，重复请求不重新下载
"""
import hashlib
import json
import logging
import random
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger("crawler")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]


class HttpClient:
    """带重试/限速/缓存的 HTTP 客户端。"""

    def __init__(self, interval=0.5, retries=3, timeout=15,
                 cache_dir=None, headers=None, proxies=None):
        self.interval = interval      # 请求最小间隔（秒）
        self.retries = retries        # 失败重试次数
        self.timeout = timeout        # 单次请求超时（秒）
        self.proxies = proxies        # 可选代理
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.headers = headers or {}
        self._last_request = 0.0

    # ---------- 对外接口 ----------

    def get(self, url, params=None, encoding=None):
        """GET 请求，返回 Response。带重试与限速。"""
        payload = self._cache_lookup(url, params)
        if payload is not None:
            return payload

        resp = None
        for attempt in range(1, self.retries + 1):
            try:
                self._throttle()
                resp = requests.get(
                    url, params=params,
                    headers=self._build_headers(),
                    timeout=self.timeout,
                    proxies=self.proxies,
                )
                resp.raise_for_status()
                break
            except requests.RequestException as exc:
                logger.warning("请求失败(url=%s, 第%d/%d次): %s", url, attempt, self.retries, exc)
                if attempt < self.retries:
                    time.sleep(min(2 ** attempt, 10))
                resp = None

        if resp is None:
            raise ConnectionError(f"请求失败且重试耗尽: {url}")

        if encoding:
            resp.encoding = encoding
        else:
            resp.encoding = resp.apparent_encoding or resp.encoding

        self._cache_store(url, params, resp)
        return resp

    def get_json(self, url, params=None):
        """GET 并解析 JSON。"""
        resp = self.get(url, params=params)
        return resp.json()

    def get_text(self, url, params=None, encoding=None):
        """GET 并返回文本。"""
        resp = self.get(url, params=params, encoding=encoding)
        return resp.text

    def get_soup(self, url, params=None):
        """GET 并解析为 BeautifulSoup 对象。"""
        resp = self.get(url, params=params)
        return BeautifulSoup(resp.text, "html.parser")

    # ---------- 内部实现 ----------

    def _build_headers(self):
        headers = dict(self.headers)
        headers.setdefault("User-Agent", random.choice(USER_AGENTS))
        return headers

    def _throttle(self):
        """保证两次请求之间至少间隔 self.interval 秒。"""
        elapsed = time.time() - self._last_request
        if elapsed < self.interval:
            time.sleep(self.interval - elapsed)
        self._last_request = time.time()

    # ---------- 缓存 ----------

    def _cache_key(self, url, params):
        raw = url + "?" + json.dumps(params or {}, sort_keys=True)
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    def _cache_lookup(self, url, params):
        if not self.cache_dir:
            return None
        path = self.cache_dir / (self._cache_key(url, params) + ".txt")
        if not path.exists():
            return None
        text = path.read_text(encoding="utf-8")
        logger.info("命中缓存: %s", path.name)
        return self._resp_from_text(text)

    def _cache_store(self, url, params, resp):
        if not self.cache_dir:
            return
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self.cache_dir / (self._cache_key(url, params) + ".txt")
        path.write_text(f"URL: {url}\nSTATUS: {resp.status_code}\n\n{resp.text}", encoding="utf-8")
        logger.info("写入缓存: %s", path.name)

    @staticmethod
    def _resp_from_text(text):
        """把缓存文本重新包装成 Response 对象，供调用方透明使用。"""
        _, _, body = text.partition("\n\n")
        resp = requests.Response()
        resp.status_code = 200
        resp._content = body.encode("utf-8")
        resp.encoding = "utf-8"
        return resp
