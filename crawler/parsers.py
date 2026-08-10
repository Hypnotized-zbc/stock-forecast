# -*- coding: utf-8 -*-
"""
crawler.parsers — 通用解析小工具
=================================
从抓到的响应里提取结构化数据。不绑定任何网站。

函数清单：
    parse_json_text(text)     宽松 JSON 解析（容忍注释/尾逗号）
    html_table_to_rows(soup)  HTML 表格 -> 二维列表
    rows_to_dicts(rows)       带表头的二维列表 -> 字典列表
"""
import json
import re

from bs4 import BeautifulSoup


def parse_json_text(text):
    """宽松 JSON 解析：去掉 // 注释和尾逗号后再解析。"""
    cleaned = re.sub(r",\s*([}\]])", r"\1", text)   # 去掉尾逗号
    cleaned = re.sub(r"//[^\n]*", "", cleaned)      # 去掉 // 注释
    return json.loads(cleaned)


def html_table_to_rows(soup, table_index=0):
    """从 BeautifulSoup 中取出第 table_index 个表格，返回二维列表。"""
    table = soup.find_all("table")[table_index]
    rows = []
    for tr in table.find_all("tr"):
        cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
        if cells:
            rows.append(cells)
    return rows


def rows_to_dicts(rows, header_row=0):
    """把带表头的二维列表转成字典列表：[{列名: 值, ...}, ...]"""
    if not rows:
        return []
    headers = rows[header_row]
    return [dict(zip(headers, row)) for row in rows[header_row + 1:]]
