# -*- coding: utf-8 -*-
"""
crawler_stock.py — 股票历史数据下载器（单文件）
=================================================
输入股票名称 → 搜索候选 → 下载近一年历史数据 → 保存 CSV 到桌面。

数据源：东方财富公开接口（无需 token、无 Cloudflare 防护）
    - 搜索:  https://searchapi.eastmoney.com/api/suggest/get
    - K线:   https://push2his.eastmoney.com/api/qt/stock/kline/get
    覆盖 A股 / 美股 / 港股（按名称或代码搜索均可）。

运行方式：
    python3 crawler_stock.py
    程序先问"要下载哪个股票？"，再自动完成搜索、下载、存桌面。

依赖：requests（标准库之外仅此一个；已写入 requirements.txt）
"""
import csv
import json
import os
import sys
import time
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

import requests

# 东方财富接口（token 是公开固定值，官网页面也在用）
SEARCH_API = "https://searchapi.eastmoney.com/api/suggest/get"
KLINE_API = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
PUBLIC_TOKEN = "D43BF722C8E33BDC906FB84D85E326E8"

# K线字段（fields2 顺序固定）：
# f51日期 f52开盘 f53收盘 f54最高 f55最低 f56成交量(手) f57成交额(元)
# f58振幅 f59涨跌幅 f60涨跌额 f61换手率
KLINE_FIELDS = ("f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61")
CSV_COLUMNS = ["日期", "开盘", "收盘", "最高", "最低",
               "成交量(手)", "成交额(元)", "振幅%", "涨跌幅%", "涨跌额", "换手率%"]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
}


# ---------------------------------------------------------------------------
# 网络请求（带重试）
# ---------------------------------------------------------------------------
def get_json(url, params, retries=3):
    """GET JSON，失败重试（东财接口偶发超时，重试即可）。"""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            last_err = exc
            print(f"[!] 请求失败(第{attempt}/{retries}次): {exc}")
            time.sleep(2 * attempt)
    raise ConnectionError(f"接口请求失败: {url} ({last_err})")


# ---------------------------------------------------------------------------
# 搜索与选择
# ---------------------------------------------------------------------------
def search_stock(name):
    """按名称/代码搜索，返回候选列表：[{名称, 代码, 市场, 分类, secid}, ...]"""
    params = {"input": name, "type": "14", "token": PUBLIC_TOKEN}
    data = get_json(SEARCH_API, params)
    rows = (data.get("QuotationCodeTable") or {}).get("Data") or []
    candidates = []
    for r in rows:
        secid = r.get("QuoteID") or ""
        # 只要股票（过滤债券/指数/基金）：Classify 含 Stock
        classify = r.get("Classify") or ""
        if "Stock" not in classify and not r.get("SecurityTypeName"):
            continue
        candidates.append({
            "名称": r.get("Name", ""),
            "代码": r.get("Code", ""),
            "市场": r.get("SecurityTypeName", ""),
            "分类": classify,
            "secid": secid,
        })
    # 去重（同名不同市场保留，同 secid 去掉）
    seen, uniq = set(), []
    for c in candidates:
        if c["secid"] in seen:
            continue
        seen.add(c["secid"])
        uniq.append(c)
    return uniq


def ask_stock_name():
    """下载前询问股票名称。"""
    name = input("要下载哪个股票的历史数据？请输入名称或代码（如：贵州茅台 / 600519 / AAPL）：").strip()
    while not name:
        name = input("输入不能为空，请输入股票名称或代码：").strip()
    return name


def choose_candidate(candidates):
    """多候选时让用户选择，返回 secid。"""
    if not candidates:
        return None
    if len(candidates) == 1:
        c = candidates[0]
        print(f"[i] 找到唯一匹配：{c['名称']}（{c['代码']} {c['市场']}）")
        return c
    print("[i] 找到多个匹配，请选择：")
    for i, c in enumerate(candidates[:10], 1):
        print(f"    {i}. {c['名称']}  {c['代码']}  {c['市场']}  {c['分类']}")
    while True:
        try:
            idx = int(input(f"输入序号（1-{min(10, len(candidates))}，回车默认第 1 个）：").strip() or "1")
            if 1 <= idx <= min(10, len(candidates)):
                return candidates[idx - 1]
        except ValueError:
            pass
        print("序号无效，重新输入。")


# ---------------------------------------------------------------------------
# K线下载
# ---------------------------------------------------------------------------
def fetch_kline(secid, start, end):
    """下载 [start, end] 区间日K，返回 (股票名, [行, ...])。

    每行与 CSV_COLUMNS 对应；数值保留原字符串，便于 CSV 直接打开。
    """
    params = {
        "secid": secid,
        "klt": "101",          # 101=日K
        "fqt": "1",            # 前复权（默认，贴近真实走势）
        "beg": start.strftime("%Y%m%d"),
        "end": end.strftime("%Y%m%d"),
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": KLINE_FIELDS,
    }
    data = get_json(KLINE_API, params).get("data")
    if not data or not data.get("klines"):
        return None, []
    name = data.get("name", "")
    rows = [line.split(",") for line in data["klines"]]
    return name, rows


# ---------------------------------------------------------------------------
# CSV 保存
# ---------------------------------------------------------------------------
def get_desktop():
    """定位 Windows 桌面；找不到则回退当前目录。"""
    candidates = [
        Path("/mnt/c/Users/Jerry Zhao/Desktop"),          # 本机 WSL 挂载
        Path(os.environ.get("USERPROFILE", "")) / "Desktop",  # 通用 Windows 侧
        Path.home() / "Desktop",
    ]
    for p in candidates:
        if p.is_dir():
            return p
    return Path.cwd()


def save_csv(name, rows, out_dir):
    """写 CSV，返回文件路径。文件名带股票名和日期区间。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{name}_近一年_{datetime.now():%Y%m%d_%H%M}.csv"
    path = out_dir / fname
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_COLUMNS)
        writer.writerows(rows)
    return path


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    print("=" * 56)
    print("股票历史数据下载器（近一年 · 东方财富数据源）")
    print("=" * 56)

    name = ask_stock_name()

    # 1. 搜索
    print(f"[i] 正在搜索: {name} ...")
    candidates = search_stock(name)
    if not candidates:
        print(f"[x] 未找到股票「{name}」，请检查名称/代码后重试。")
        sys.exit(1)

    # 2. 选择目标
    chosen = choose_candidate(candidates)
    if not chosen:
        sys.exit(1)
    print(f"[i] 已选择：{chosen['名称']}（{chosen['代码']}）")

    # 3. 下载近一年日K
    end = datetime.now()
    start = end - timedelta(days=365)
    print(f"[i] 下载区间: {start:%Y-%m-%d} ~ {end:%Y-%m-%d} ...")
    stock_name, rows = fetch_kline(chosen["secid"], start, end)
    if not rows:
        print("[x] 未获取到K线数据（股票可能已退市或无交易记录）。")
        sys.exit(1)
    print(f"[i] 获取 {len(rows)} 条日K，最新: {rows[-1][0]} 收盘 {rows[-1][2]}")

    # 4. 保存到桌面
    desktop = get_desktop()
    path = save_csv(stock_name or chosen["名称"], rows, desktop)
    print(f"\n[ok] CSV 已保存：{path}")
    print(f"     共 {len(rows)} 条记录（{rows[0][0]} ~ {rows[-1][0]}）")


if __name__ == "__main__":
    main()
