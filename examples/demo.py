# -*- coding: utf-8 -*-
"""
示例：用通用爬虫框架抓一个普通 JSON 接口。
本示例与股票无关，只演示 HttpClient 的用法。

运行：
    source ~/venv/bin/activate
    python -m examples.demo

演示内容：
1. get_json 抓取 httpbin.org 返回的 JSON（验证请求/解析链路）
2. 开启缓存后重复请求会命中缓存，不再真正发请求
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crawler.core import HttpClient  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


def main():
    client = HttpClient(interval=1.0, cache_dir="data/cache")

    # 1. 抓取 JSON
    data = client.get_json("https://httpbin.org/json")
    slide = data["slideshow"]
    print("标题:", slide["title"], "| 作者:", slide["author"])
    print("幻灯片张数:", len(slide["slides"]))

    # 2. 重复请求 —— 命中缓存，日志会打印"命中缓存"
    client.get_json("https://httpbin.org/json")
    print("第二次请求已命中缓存，未真正访问网络。")

    # 3. 抓取普通网页文本
    text = client.get_text("https://example.com")
    print("example.com 页面标题:", text.split("<title>")[1].split("</title>")[0].strip())


if __name__ == "__main__":
    main()
