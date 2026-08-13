# -*- coding: utf-8 -*-
"""
crawler_stock.py — 股票历史数据查询 + 图表（单文件 Web 应用）
===============================================================
启动后自动打开浏览器页面：
    页面上方"查询股票："输入框 → 输入名称/代码 → 选择候选 →
    在同一个页面显示 K线/MA5/BOLL/成交量/涨跌幅 图表（下拉切换）。

技术要点：
- 本地 HTTP 服务（标准库 http.server，无第三方框架），绑定 127.0.0.1 随机端口
- 页面与后端同源：浏览器 fetch /api/search、/api/kline，后端转发东财/新浪接口
  （浏览器直连东财会被 CORS 拦，本地后端中转绕过）
- 图表为原生 Canvas 绘制，单页面内完成查询与结果展示，不刷新跳转
- 数据源：东方财富（主）+ 新浪（备用，东财断连自动切换）
- 关闭浏览器页面即自动停止本地服务（页面卸载时 sendBeacon 通知）

运行方式：
    python3 crawler_stock.py
    浏览器自动打开 http://127.0.0.1:<port>/

依赖：requests（其余全用标准库 + 浏览器自带 Canvas）
"""
import csv
import hashlib
import json
import math
import os
import random
import re
import secrets
import sqlite3
import string
import threading
import time
import urllib.parse
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import db
import requests

# 东方财富接口（token 是公开固定值，官网页面也在用）
# 前端页面（static/index.html、static/login.html），启动时读取一次并缓存
_HTML_PATH = Path(__file__).resolve().parent / "static" / "index.html"
_LOGIN_PATH = Path(__file__).resolve().parent / "static" / "login.html"
_index_html_cache = None
_login_html_cache = None

def load_index_html():
    global _index_html_cache
    if _index_html_cache is None:
        _index_html_cache = _HTML_PATH.read_text(encoding="utf-8")
    return _index_html_cache


def load_login_html():
    global _login_html_cache
    if _login_html_cache is None:
        _login_html_cache = _LOGIN_PATH.read_text(encoding="utf-8")
    return _login_html_cache


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

DATA_DIR = Path(__file__).resolve().parent / "data"

# ---------------------------------------------------------------------------
# 网络请求（带重试）
# ---------------------------------------------------------------------------
_SESSION = requests.Session()  # 复用连接，减少被远端断开的概率


def _parse_json_text(text):
    """解析东财响应：兼容纯 JSON 和 JSONP（jQuery123({...}) 回调包裹）。

    东财 searchapi 对不同来源可能返回 JSONP（服务器/云 IP 段实测返回
    jQuery3510...({...})，本机直连返回纯 JSON），resp.json() 遇 JSONP
    直接抛 ValueError('Expecting value...')——必须剥掉回调外壳再解析。
    """
    t = (text or "").strip()
    if not t:
        raise ValueError("空响应")
    if t[0] in "[{":
        return json.loads(t)
    i = t.find("(")
    j = t.rfind(")")
    if 0 < i < j:
        inner = t[i + 1:j].strip()
        if inner:
            return json.loads(inner)
    raise ValueError(f"无法解析响应: {t[:80]!r}")


def get_json(url, params, retries=4, timeout=10):
    """GET JSON，失败重试。

    WSL 网络间歇抖动（东财接口偶发 RemoteDisconnected），重试 4 次、
    指数退避；总等待控制在约 30 秒内，避免浏览器端等待过久。
    timeout：行情类接口传小值（如 4s）快速失败，尽早切换备用源。
    """
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            resp = _SESSION.get(url, params=params, headers=HEADERS, timeout=timeout)
            resp.raise_for_status()
            return _parse_json_text(resp.text)
        except (requests.RequestException, ValueError) as exc:
            last_err = exc
            print(f"[!] 请求失败(第{attempt}/{retries}次): {exc}")
            time.sleep(1.5 ** attempt)  # 指数退避：2s, 3s, 5s, 8s
    raise ConnectionError(f"接口请求失败: {url} ({last_err})")


# ---------------------------------------------------------------------------
# 搜索与K线（数据层）
# ---------------------------------------------------------------------------
TENCENT_SEARCH_API = "https://smartbox.gtimg.cn/s3/"


def _tencent_search(name):
    """腾讯股票搜索备用源（东财对云服务器 IP 段不返回股票数据时回退）。

    返回与东财 search_stock 相同结构的候选列表；失败返回 None（调用方决定回退）。
    响应格式：v_hint="sh~601088~\\u4e2d\\u56fd\\u795e\\u534e~zgsh~GP-A^sz~000001~..."
    字段顺序：市场~代码~名称(unicode转义)~拼音~类型；多个结果用 ^ 或 ; 分隔
    """
    try:
        resp = _SESSION.get(TENCENT_SEARCH_API,
                            params={"v": "2", "q": name, "t": "all"},
                            headers=HEADERS, timeout=10)
        resp.raise_for_status()
        text = resp.text
        m = re.search(r'v_hint="([^"]*)"', text)
        if not m or not m.group(1):
            return None
        candidates = []
        for item in re.split(r"[;^]", m.group(1)):
            parts = item.split("~")
            if len(parts) < 5:
                continue
            market, code, raw_name, pinyin, ctype = parts[0], parts[1], parts[2], parts[3], parts[4]
            # 只保留沪深 A 股：市场必须 sh/sz 且类型 GP-A（港股 hk/美股 us 也会标 GP，必须排除）
            if market not in ("sh", "sz") or ctype not in ("GP-A", "GP"):
                continue
            if not code.isdigit():  # 美股代码形如 csuay.ps，过滤
                continue
            secid = ("1." if market == "sh" else "0.") + code
            try:
                name_zh = raw_name.encode("utf-8").decode("unicode_escape")
            except Exception:
                name_zh = raw_name
            candidates.append({
                "名称": name_zh,
                "代码": code,
                "市场": "沪A" if market == "sh" else "深A",
                "分类": ctype,
                "secid": secid,
            })
        return candidates or None
    except Exception as exc:
        print(f"[!] 腾讯搜索备用源失败: {exc}")
        return None


def search_stock(name):
    """按名称/代码搜索，返回候选列表：[{名称, 代码, 市场, 分类, secid}, ...]"""
    params = {"input": name, "type": "14", "token": PUBLIC_TOKEN}
    try:
        data = get_json(SEARCH_API, params)
    except ConnectionError as exc:
        print(f"[!] 东财搜索失败，回退腾讯源: {exc}")
        return _tencent_search(name) or []
    rows = (data.get("QuotationCodeTable") or {}).get("Data") or []
    candidates = []
    for r in rows:
        secid = r.get("QuoteID") or ""
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
    # 东财响应异常（云服务器 IP 段返回无 QuotationCodeTable 的空壳）→ 回退腾讯
    if not candidates:
        print("[!] 东财搜索返回空股票结果，回退腾讯源")
        return _tencent_search(name) or []
    seen, uniq = set(), []
    for c in candidates:
        if c["secid"] in seen:
            continue
        seen.add(c["secid"])
        uniq.append(c)
    return uniq


def fetch_kline(secid, start, end, period="day"):
    """下载 [start, end] 区间K线，返回 (股票名, 行列表)。

    周期：day=日K(默认)、week=周K、month=月K。
    优先东方财富；东财接口在 WSL 下偶发断连，失败时自动回退新浪备用源
    （新浪仅支持 A股：secid 以 1./0. 开头；美股/港股保持东财重试；
    周/月K 新浪无周期接口，用日K数据聚合）。
    """
    try:
        return _fetch_kline_em(secid, start, end, period)
    except ConnectionError as exc:
        print(f"[i] 东财K线接口失败，尝试新浪备用源: {exc}")
        name, rows = _fetch_kline_sina(secid, start, end)
        if period in ("week", "month"):
            rows = agg_period(rows, period)
        return name, rows


def agg_period(rows, period):
    """把日K行聚合成周K/月K，保持东财 11 字段结构。"""
    groups = []
    cur_key = None
    for r in rows:
        dt = datetime.strptime(r[0], "%Y-%m-%d")
        key = dt.isocalendar()[:2] if period == "week" else (dt.year, dt.month)
        if key != cur_key:
            groups.append([])
            cur_key = key
        groups[-1].append(r)
    out = []
    for g in groups:
        first, last = g[0], g[-1]
        out.append([
            last[0],
            first[1],
            last[2],
            str(max(float(r[3]) for r in g)),
            str(min(float(r[4]) for r in g)),
            str(sum(float(r[5]) for r in g)),
            str(sum(float(r[6]) for r in g)),
            last[7], last[8], last[9], last[10],
        ])
    return out


def _fetch_kline_em(secid, start, end, period="day"):
    """东方财富 K线。"""
    klt = {"day": "101", "week": "102", "month": "103"}.get(period, "101")
    params = {
        "secid": secid,
        "klt": klt,           # 101=日K, 102=周K, 103=月K
        "fqt": "1",            # 前复权
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


def _fetch_kline_sina(secid, start, end):
    """新浪备用 K线（A股）。返回与东财相同结构的 11 字段行。

    新浪字段少：成交额/换手率无数据置空；涨跌额/涨跌幅/振幅按收盘价推算。
    成交量单位：新浪返回股数，统一换算为手（÷100）与东财一致。
    """
    if not secid.startswith(("1.", "0.")):
        raise ConnectionError("新浪备用源仅支持A股，请稍后重试东财接口")

    market = "sh" if secid.startswith("1.") else "sz"
    code = secid.split(".")[1]
    symbol = f"{market}{code}"
    # 数据量随区间动态调整（接口上限约 1023 条），不足 300 条时取 300
    days = (end - start).days
    datalen = min(1023, max(300, days + 120))
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "CN_MarketData.getKLineData")
    params = {"symbol": symbol, "scale": "240", "ma": "no", "datalen": str(datalen)}
    data = get_json(url, params)
    if not data:
        return None, []

    rows = []
    prev_close = None
    for item in data:
        day = item["day"]
        if not (start.strftime("%Y-%m-%d") <= day <= end.strftime("%Y-%m-%d")):
            prev_close = float(item["close"])
            continue  # 先扫描到区间起点，保留前收
        op, hi, lo, cl = (float(item["open"]), float(item["high"]),
                          float(item["low"]), float(item["close"]))
        vol_hand = int(float(item["volume"]) / 100)          # 股 -> 手
        change = "" if prev_close is None else round(cl - prev_close, 2)
        change_pct = "" if prev_close is None else round((cl / prev_close - 1) * 100, 2)
        amplitude = "" if prev_close is None else round((hi - lo) / prev_close * 100, 2)
        rows.append([day,
                     f"{op:.2f}", f"{cl:.2f}", f"{hi:.2f}", f"{lo:.2f}",
                     str(vol_hand), "",                          # 成交额缺失
                     str(amplitude), str(change_pct), str(change), ""])
        prev_close = cl

    name = _sina_stock_name(symbol)
    return name, rows


def _sina_stock_name(symbol):
    """从新浪股票接口取名称（尽力而为，失败返回空串——调用方用请求参数 name 兜底）。

    注意：绝不返回 symbol（sh601088 这种内部代码）当名称——
    曾导致其他电脑上"当前查询"区域显示 sh601088 而不是股票名。
    """
    try:
        url = ("https://hq.sinajs.cn/list=" + symbol)
        resp = _SESSION.get(url, headers={
            "User-Agent": HEADERS["User-Agent"],
            "Referer": "https://finance.sina.com.cn/",
        }, timeout=4)
        resp.encoding = "gbk"
        text = resp.text
        # 格式: var hq_str_sh600519="贵州茅台,开盘,昨收,..."
        if "=\"" in text:
            name = text.split("=\"")[1].split(",")[0]
            if name:
                return name
    except Exception:
        pass
    return ""


def ma(values, n):
    """简单移动平均；前 n-1 个位置补 None。"""
    out = [None] * len(values)
    for i in range(n - 1, len(values)):
        out[i] = round(sum(values[i - n + 1:i + 1]) / n, 3)
    return out


def boll(values, n=20, k=2.0):
    """布林带：中轨 MA(n)，上下轨 = 中轨 ± k*标准差(总体)。"""
    import statistics
    mid, up, low = [None] * len(values), [None] * len(values), [None] * len(values)
    for i in range(n - 1, len(values)):
        window = values[i - n + 1:i + 1]
        m = sum(window) / n
        sd = statistics.pstdev(window)
        mid[i] = round(m, 3)
        up[i] = round(m + k * sd, 3)
        low[i] = round(m - k * sd, 3)
    return up, mid, low


def ema(values, n):
    """指数移动平均：EMA = alpha*C + (1-alpha)*prev_EMA，alpha = 2/(n+1)。"""
    alpha = 2.0 / (n + 1)
    out = [None] * len(values)
    s = None
    for i, v in enumerate(values):
        s = v if s is None else alpha * v + (1 - alpha) * s
        out[i] = s
    return out


def macd(values, fast=12, slow=26, signal=9):
    """MACD：DIF=EMA12-EMA26，DEA=EMA9(DIF)，柱=2*(DIF-DEA)。"""
    e_fast = ema(values, fast)
    e_slow = ema(values, slow)
    dif = [None if (a is None or b is None) else a - b for a, b in zip(e_fast, e_slow)]
    valid = [v for v in dif if v is not None]
    dea_raw = ema(valid, signal)
    dea = [None] * len(dif)
    k = 0
    for i in range(len(dif)):
        if dif[i] is not None:
            dea[i] = dea_raw[k]
            k += 1
    hist = [None if (a is None or b is None) else 2 * (a - b) for a, b in zip(dif, dea)]
    return dif, dea, hist


def kdj(highs, lows, closes, n=9):
    """KDJ：RSV=(C-L9)/(H9-L9)*100，K/D 用 2/3 递推平滑，J=3K-2D。"""
    k_len = len(closes)
    K = [None] * k_len
    D = [None] * k_len
    J = [None] * k_len
    k = 50.0
    d = 50.0
    for i in range(k_len):
        lo9 = min(lows[max(0, i - n + 1):i + 1])
        hi9 = max(highs[max(0, i - n + 1):i + 1])
        rsv = 50.0 if hi9 == lo9 else (closes[i] - lo9) / (hi9 - lo9) * 100
        k = 2.0 / 3 * k + 1.0 / 3 * rsv
        d = 2.0 / 3 * d + 1.0 / 3 * k
        K[i] = k
        D[i] = d
        J[i] = 3 * k - 2 * d
    return K, D, J


def rsi(values, n=14):
    """RSI：平均涨幅/(平均涨幅+平均跌幅)*100（Wilder 平滑）。"""
    out = [None] * len(values)
    if len(values) <= n:
        return out
    gains = [0.0]
    losses = [0.0]
    for i in range(1, len(values)):
        ch = values[i] - values[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    ag = sum(gains[1:n + 1]) / n
    al = sum(losses[1:n + 1]) / n
    out[n] = 100.0 if (ag + al) == 0 else ag / (ag + al) * 100
    for i in range(n + 1, len(values)):
        ag = (ag * (n - 1) + gains[i]) / n
        al = (al * (n - 1) + losses[i]) / n
        out[i] = 100.0 if (ag + al) == 0 else ag / (ag + al) * 100
    return out


def build_chart_data(rows):
    """把东财行数据转成图表 JSON 结构（含 MA/BOLL/MACD/KDJ/RSI 指标）。"""
    closes = [float(r[2]) for r in rows]
    highs = [float(r[3]) for r in rows]
    lows = [float(r[4]) for r in rows]
    dif, dea, hist = macd(closes)
    kdjk, kjdd, kdjj = kdj(highs, lows, closes)
    return {
        "dates": [r[0] for r in rows],
        "opens": [float(r[1]) for r in rows],
        "closes": closes,
        "highs": [float(r[3]) for r in rows],
        "lows": [float(r[4]) for r in rows],
        "vols": [float(r[5]) for r in rows],
        "changes": [float(r[8]) for r in rows],
        "ma5": ma(closes, 5),
        "ma20": ma(closes, 20),
        "boll_up": boll(closes)[0],
        "boll_mid": boll(closes)[1],
        "boll_low": boll(closes)[2],
        "macd_dif": dif,
        "macd_dea": dea,
        "macd_hist": hist,
        "kdj_k": kdjk,
        "kdj_d": kjdd,
        "kdj_j": kdjj,
        "rsi": rsi(closes),
    }


def _cholesky_solve(A, b):
    """Cholesky 求解对称正定方程组 A·x = b（纯 Python）。

    A: 二维 list，b: list。返回 x: list。
    """
    n = len(A)
    L = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1):
            s = A[i][j] - sum(L[i][k] * L[j][k] for k in range(j))
            if i == j:
                if s <= 1e-15:
                    s = 1e-15
                L[i][j] = s ** 0.5
            else:
                L[i][j] = s / L[j][j]
    y = [0.0] * n
    for i in range(n):
        y[i] = (b[i] - sum(L[i][k] * y[k] for k in range(i))) / L[i][i]
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        x[i] = (y[i] - sum(L[j][i] * x[j] for j in range(i + 1, n))) / L[i][i]
    return x


def _prophet_light(y, horizon):
    """Prophet 轻量版：线性趋势 + 变点（分段斜率）+ 傅里叶周度季节。

    核心思想同 Meta Prophet：y = trend(t) + season(t) + eps，
    最小二乘一次性解出全部系数，预测 = 设计矩阵外推 × 系数。
    """
    n = len(y)
    ts = list(range(n))
    ncp = 3  # 变点数（数据前 80% 内等分）
    cps = [int(0.2 * n + (0.6 * n) * (k + 1) / (ncp + 1)) for k in range(ncp)]

    def design(t):
        row = [1.0, float(t)]
        row += [float(max(0, t - cp)) for cp in cps]
        for k in (1, 2):  # 周度傅里叶（日频数据周季节最明显）
            row.append(math.cos(2 * math.pi * k * t / 7.0))
            row.append(math.sin(2 * math.pi * k * t / 7.0))
        return row

    X = [design(t) for t in ts]
    p = len(X[0])
    Xt = [[X[i][j] for i in range(n)] for j in range(p)]
    A = [[sum(Xt[a][i] * X[i][b] for i in range(n)) for b in range(p)] for a in range(p)]
    rhs = [sum(Xt[a][i] * y[i] for i in range(n)) for a in range(p)]
    beta = _cholesky_solve(A, rhs)

    fitted = [sum(beta[j] * X[i][j] for j in range(p)) for i in range(n)]
    pred_ts = list(range(n, n + horizon))
    Xp = [design(t) for t in pred_ts]
    preds = [sum(beta[j] * Xp[i][j] for j in range(p)) for i in range(horizon)]
    return fitted, preds


def _svr_krr(y, horizon):
    """SVR 轻量版：RBF 核岭回归（核方法，等价 ε-SVR 的平滑近似）。

    K_ij = exp(-γ(t_i-t_j)²)，α = (K+λI)⁻¹y，
    拟合 = K·α，预测 = 核外推·α。
    """
    n = len(y)
    ts = [float(t) / max(1, n - 1) for t in range(n)]  # 归一化
    sigma = 0.25
    gamma = 1.0 / (2 * sigma * sigma)

    K = [[math.exp(-gamma * (ts[i] - ts[j]) ** 2) for j in range(n)] for i in range(n)]
    lam = 0.01
    A = [[K[i][j] + (lam if i == j else 0.0) for j in range(n)] for i in range(n)]
    alpha = _cholesky_solve(A, list(y))

    fitted = [sum(K[i][j] * alpha[j] for j in range(n)) for i in range(n)]
    p_ts = [float(t) / max(1, n - 1) for t in range(n, n + horizon)]
    preds = []
    for pt in p_ts:
        k_row = [math.exp(-gamma * (pt - ts[j]) ** 2) for j in range(n)]
        preds.append(sum(k_row[j] * alpha[j] for j in range(n)))
    return fitted, preds


def _rf_light(y, horizon, n_trees=30, max_depth=8, seed=7):
    """随机森林轻量版：决策树回归 + bagging（手写，无 sklearn）。

    特征：归一化时间 t ∈ [0,1]。单特征递归分裂（最小化 MSE），
    每棵树用随机 60% 子样本训练；预测 = 树输出均值。
    树无法外推：预测超出训练范围时取末端叶子均值（趋势趋平）。
    """
    import random
    rng = random.Random(seed)
    n = len(y)
    ts = [float(t) / max(1, n - 1) for t in range(n)]

    def build_tree(idx, depth):
        # idx: 样本下标列表
        if depth >= max_depth or len(idx) < 5:
            return ("leaf", sum(y[i] for i in idx) / len(idx))
        best = None
        cand = sorted(set(ts[i] for i in idx))
        if len(cand) < 2:
            return ("leaf", sum(y[i] for i in idx) / len(idx))
        for t0 in cand[:-1]:
            left = [i for i in idx if ts[i] <= t0]
            right = [i for i in idx if ts[i] > t0]
            if not left or not right:
                continue
            ml = sum(y[i] for i in left) / len(left)
            mr = sum(y[i] for i in right) / len(right)
            loss = sum((y[i] - ml) ** 2 for i in left) + sum((y[i] - mr) ** 2 for i in right)
            if best is None or loss < best[0]:
                best = (loss, t0, left, right)
        if best is None:
            return ("leaf", sum(y[i] for i in idx) / len(idx))
        _, t0, left, right = best
        return ("node", t0, build_tree(left, depth + 1), build_tree(right, depth + 1))

    def predict_tree(tree, t):
        while tree[0] == "node":
            tree = tree[2] if t <= tree[1] else tree[3]
        return tree[1]

    trees = []
    for _ in range(n_trees):
        sample = [i for i in range(n) if rng.random() < 0.6]
        if len(sample) < 10:
            sample = list(range(n))
        trees.append(build_tree(sample, 0))

    fitted = [sum(predict_tree(t, ts[i]) for t in trees) / n_trees for i in range(n)]
    preds = [sum(predict_tree(t, float(x) / max(1, n - 1)) for t in trees) / n_trees
             for x in range(n, n + horizon)]
    return fitted, preds


def _quote_eastmoney(secid):
    """东财实时行情 + 基本面（最新价/涨跌幅/量额/PE/PB/市值/股本等）。"""
    url = "https://push2.eastmoney.com/api/qt/stock/get"
    params = {
        "secid": secid, "invt": "2", "fltt": "2",
        "fields": "f43,f47,f48,f57,f58,f60,f84,f85,f92,f162,f167,f170,f183",
    }
    data = get_json(url, params, retries=1, timeout=4)  # 行情类接口快速失败，尽早切换备用源
    d = data.get("data") or {}
    try:
        price = float(d.get("f43"))
    except (TypeError, ValueError):
        return None
    if not price or price <= 0:  # 无效行情返回 None，前端走备用源/不显示
        return None
    return {
        "secid": secid,
        "name": d.get("f58"),
        "code": d.get("f57"),
        "price": price,
        "prev_close": d.get("f60"),
        "change_pct": d.get("f170"),
        "volume": d.get("f47"),
        "amount": d.get("f48"),
        "pe": d.get("f162"),        # PE(动)
        "pb": d.get("f167"),        # PB
        "mktcap": d.get("f183"),    # 总市值(元)
        "total_shares": d.get("f84"),   # 总股本(股)
        "float_shares": d.get("f85"),   # 流通股本(股)
        "bps": d.get("f92"),        # 每股净资产
    }


def _quote_sina(secid):
    """新浪实时行情备用源（仅价格/量额，无基本面；字段缺失置 None）。"""
    prefix, code = secid.split(".")
    sym = ("sh" if prefix == "1" else "sz") + code
    url = f"https://hq.sinajs.cn/list={sym}"
    resp = _SESSION.get(url, headers={**HEADERS,
                                      "Referer": "https://finance.sina.com.cn"}, timeout=4)
    resp.raise_for_status()
    resp.encoding = "gbk"
    m = re.search(r'="([^"]*)"', resp.text)
    if not m:
        return None
    parts = m.group(1).split(",")
    if len(parts) < 32:
        return None
    try:
        prev = float(parts[2])
        price = float(parts[3])
    except (ValueError, IndexError):
        return None
    if not price or price <= 0:
        return None
    return {
        "secid": secid,
        "name": parts[0],
        "code": code,
        "price": price,
        "prev_close": prev,
        "change_pct": (price - prev) / prev * 100 if prev else 0.0,
        "volume": float(parts[8]) if parts[8] else None,   # 股
        "amount": float(parts[9]) if parts[9] else None,   # 元
        "pe": None, "pb": None, "mktcap": None,
        "total_shares": None, "float_shares": None, "bps": None,
    }


def _fill_fundamentals(q, skip_eastmoney=False):
    """基本面字段（PE/PB/市值）缺失时补齐：先东财单股、再腾讯接口。

    保险机制：东财批量成功但个别字段缺失（数据源偶发缺字段）同样执行，
    避免"一开始没拿到后面一直拿不到"。东财单股也走短超时（4s），
    防止批量失败后的补齐链路再次长时间等待。
    skip_eastmoney=True：东财批量已失败（走新浪回退）时跳过东财单股，
    直接腾讯补齐——避免批量失败后每只股票再等东财 2 次重试+退避。
    """
    if q is None or None not in (q.get("pe"), q.get("pb"), q.get("mktcap")):
        return q
    em = None
    if not skip_eastmoney:
        try:
            em = _quote_eastmoney(q["secid"])
        except Exception:
            em = None
    if not em or all(v is None for v in (em.get("pe"), em.get("pb"), em.get("mktcap"))):
        em = _quote_tencent(q["secid"])
    if em:
        for k in ("pe", "pb", "mktcap", "float_mktcap"):
            if q.get(k) is None and em.get(k) is not None:
                q[k] = em.get(k)
    return q


def _sina_quotes_batch(secids):
    """新浪批量实时行情（一次请求全部股票，替代逐只串行回退）。

    新浪 hq.sinajs.cn/list=sh601088,sz000001 一次返回多行，速度快数倍。
    仅价格/量额，无基本面（字段置 None）；失败返回 None。
    """
    if not secids:
        return []
    syms = []
    for s in secids:
        prefix, code = s.split(".")
        syms.append(("sh" if prefix == "1" else "sz") + code)
    url = "https://hq.sinajs.cn/list=" + ",".join(syms)
    try:
        resp = _SESSION.get(url, headers={**HEADERS,
                                          "Referer": "https://finance.sina.com.cn"},
                            timeout=4)
        resp.raise_for_status()
        resp.encoding = "gbk"
    except Exception as exc:
        print(f"[quotes] 新浪批量失败: {exc}")
        return None
    out = []
    for line in resp.text.strip().splitlines():
        m = re.search(r'hq_str_([a-z]{2}\d{6})="([^"]*)"', line)
        if not m:
            continue
        sym, payload = m.group(1), m.group(2)
        parts = payload.split(",")
        if len(parts) < 32:
            continue
        try:
            prev = float(parts[2])
            price = float(parts[3])
        except (ValueError, IndexError):
            continue
        if not price or price <= 0:
            continue
        secid = ("1." if sym.startswith("sh") else "0.") + sym[2:]
        out.append({
            "secid": secid,
            "name": parts[0],
            "code": sym[2:],
            "price": price,
            "prev_close": prev,
            "change_pct": (price - prev) / prev * 100 if prev else 0.0,
            "volume": float(parts[8]) if parts[8] else None,
            "amount": float(parts[9]) if parts[9] else None,
            "pe": None, "pb": None, "mktcap": None,
            "total_shares": None, "float_shares": None, "bps": None,
        })
    return out or None


def _tencent_quotes_batch(secids):
    """腾讯批量实时行情（第二备用源，含 PE/PB/市值等基本面）。

    新浪没有基本面字段，腾讯 qt.gtimg.cn 批量接口一次返回全部股票且含
    PE/PB/总市值/流通市值（字段 39/46/45/44，单位亿），作为新浪之后、
    东财之前的第二备用源。失败返回 None。
    """
    if not secids:
        return []
    syms = []
    for s in secids:
        try:
            prefix, code = s.split(".")
        except ValueError:
            continue
        syms.append(("sh" if prefix == "1" else "sz") + code)
    if not syms:
        return None
    url = "https://qt.gtimg.cn/q=" + ",".join(syms)
    try:
        resp = _SESSION.get(url, headers=HEADERS, timeout=4)
        resp.raise_for_status()
        resp.encoding = "gbk"
    except Exception as exc:
        print(f"[quotes] 腾讯批量失败: {exc}")
        return None
    out = []
    for line in resp.text.strip().splitlines():
        m = re.search(r'v_([a-z]{2}\d{6})="([^"]*)"', line)
        if not m:
            continue
        sym, payload = m.group(1), m.group(2)
        f = payload.split("~")
        if len(f) < 46:
            continue
        try:
            price = float(f[3])
            prev = float(f[4]) if f[4] else 0.0
        except (ValueError, IndexError):
            continue
        if not price or price <= 0:
            continue
        secid = ("1." if sym.startswith("sh") else "0.") + sym[2:]
        def _f(idx):
            try:
                return float(f[idx]) if f[idx] not in (None, "") else None
            except (ValueError, IndexError):
                return None
        out.append({
            "secid": secid,
            "name": f[1] if len(f) > 1 else sym,
            "code": sym[2:],
            "price": price,
            "prev_close": prev,
            "change_pct": _f(32) if len(f) > 32 else None,  # 涨跌幅%
            "volume": _f(6) if len(f) > 6 else None,          # 成交量(手)
            "amount": None,
            "pe": _f(39) if len(f) > 39 else None,            # PE(TTM)
            "pb": _f(46) if len(f) > 46 else None,            # PB
            "mktcap": (_f(45) * 1e8) if len(f) > 45 and _f(45) else None,  # 总市值(亿→元)
            "float_mktcap": (_f(44) * 1e8) if len(f) > 44 and _f(44) else None,
            "total_shares": None, "float_shares": None, "bps": None,
        })
    return out or None


def fetch_quotes(secids):
    """实时行情三重保护：新浪批量（主）→ 腾讯批量（备1）→ 东财批量（备2，含基本面）→ 逐只兜底。

    用户实测云服务器上东财接口长期失败，故不再把东财放第一位——
    新浪/腾讯稳定且支持批量一次返回，东财仅在两者都失败时兜底。
    每层都短超时（4s）快速失败，保证最坏情况也在几秒内返回。
    """
    secids = [s.strip() for s in secids if s and s.strip()]
    if not secids:
        return []
    # 第一源：新浪批量（一次全部，速度快）
    sina = _sina_quotes_batch(secids)
    if sina:
        return [_fill_fundamentals(q, skip_eastmoney=True) for q in sina]
    # 第二源：腾讯批量（含 PE/PB/市值基本面，无需补齐）
    tencent = _tencent_quotes_batch(secids)
    if tencent:
        return tencent
    # 第三源：东财批量（含基本面；失败或空则落逐只兜底）
    try:
        url = "https://push2.eastmoney.com/api/qt/ulist.np/get"
        params = {
            "secids": ",".join(secids), "fltt": "2", "invt": "2",
            "fields": "f2,f3,f5,f6,f9,f12,f13,f14,f18,f20,f21,f23",
        }
        data = get_json(url, params, retries=1, timeout=4)
        diff = (data.get("data") or {}).get("diff") or []
        out = []
        for it in diff:
            try:
                price = float(it.get("f2"))
            except (TypeError, ValueError):
                continue
            if not price or price <= 0:
                continue
            out.append({
                "secid": ("1." if it.get("f13") == 1 else "0.") + str(it.get("f12")),
                "name": it.get("f14"),
                "code": str(it.get("f12")),
                "price": price,
                "prev_close": it.get("f18"),
                "change_pct": it.get("f3"),
                "volume": it.get("f5"),
                "amount": it.get("f6"),
                "pe": it.get("f9"),
                "pb": it.get("f23"),
                "mktcap": it.get("f20"),
                "float_mktcap": it.get("f21"),
            })
        if out:
            return [_fill_fundamentals(q) for q in out]
    except Exception as exc:
        print(f"[quotes] 东财批量失败: {exc}")
    # 最后兜底：逐只新浪→腾讯→东财
    fallback = [q for q in (fetch_quote(s) for s in secids) if q]
    return [_fill_fundamentals(q, skip_eastmoney=True) for q in fallback]


def _tencent_code(secid):
    """东财 secid(1.601088) → 腾讯代码(sh601088)。"""
    prefix, code = secid.split(".")
    return ("sh" if prefix == "1" else "sz") + code


def _quote_tencent(secid):
    """腾讯行情接口 qt.gtimg.cn（完整单只行情 + PE/PB/市值）。

    字段：1名称 3现价 4昨收 5今开 6成交量(手) 32涨跌幅% 39PE 44流通市值亿
    45总市值亿 46PB。失败返回 None。
    """
    try:
        resp = _SESSION.get(
            f"https://qt.gtimg.cn/q={_tencent_code(secid)}",
            headers={"Referer": "http://finance.qq.com"}, timeout=4,
        )
        resp.raise_for_status()
        raw = resp.content.decode("gbk", errors="replace")
        m = re.search(r'v_([a-z]{2}\d{6})="([^"]*)"', raw)
        if not m:
            return None
        f = m.group(2).split("~")
        if len(f) < 47:
            return None
        def num(i, allow_neg=False):
            try:
                v = float(f[i])
                return v if (v != 0 or allow_neg) else None
            except (TypeError, ValueError, IndexError):
                return None
        price = num(3)
        if not price:
            return None
        code = m.group(1)[2:]
        return {
            "secid": secid,
            "name": f[1] if len(f) > 1 else code,
            "code": code,
            "price": price,
            "prev_close": num(4),
            "change_pct": num(32, allow_neg=True),  # 涨跌幅可为负
            "volume": num(6),
            "amount": None,
            "pe": num(39),
            "pb": num(46),
            "mktcap": num(45) * 1e8 if num(45) else None,       # 亿 → 元
            "float_mktcap": num(44) * 1e8 if num(44) else None,  # 亿 → 元
            "total_shares": None, "float_shares": None, "bps": None,
        }
    except Exception:
        return None


def fetch_quote(secid):
    """单只实时行情三重保护：新浪（主）→ 腾讯（备）→ 东财（兜底）；全失败返回 None。"""
    try:
        q = _quote_sina(secid)
        if q:
            return q
        print("[quote] 新浪返回空")
    except Exception as exc:
        print(f"[quote] 新浪失败: {exc}")
    try:
        q = _quote_tencent(secid)
        if q:
            return q
        print("[quote] 腾讯返回空")
    except Exception as exc:
        print(f"[quote] 腾讯失败: {exc}")
    try:
        q = _quote_eastmoney(secid)
        if q:
            return q
        print("[quote] 东财返回空")
    except Exception as exc:
        print(f"[quote] 东财失败: {exc}")
    return None


def compute_fits(closes, dates=None, horizon=10):
    """对收盘价序列做三种模型拟合（in-sample）+ 未来 horizon 日预测。

    返回结构：
        {"linear": {"name", "values": [...], "mae", "rmse", "r2"}, ...}
        每个模型含 "predict": [horizon 个预测值], "predict_dates": [horizon 个日期]
    纯 Python 实现，零第三方依赖（只需 requests），任何环境可跑：
    - 线性回归：最小二乘直线（手写公式）
    - ARIMA(1,1,0)：一阶差分 + AR(1) 系数最小二乘估计（数学上等价）
    - ETS：Holt 线性趋势指数平滑（α/β 网格搜索最小化 SSE）
    数据不足或异常时返回 None（前端提示不可用，不影响行情功能）。
    """
    # 过滤 None/非法值，并同步日期
    raw = list(zip(closes, dates)) if dates and len(dates) == len(closes) \
        else [(v, None) for v in closes]
    pairs = []
    for v, d in raw:
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        pairs.append((f, d))
    y = [p[0] for p in pairs]
    dates = [p[1] for p in pairs]
    n = len(y)
    if n < 10:
        return None

    # 未来 horizon 个预测日期：一律从今天起算（数据源滞后时避免出现过去日期）
    try:
        today = datetime.now()
        pred_dates = [(today + timedelta(days=i + 1)).strftime("%Y-%m-%d")
                      for i in range(horizon)]
    except Exception:
        pred_dates = [f"D+{i + 1}" for i in range(horizon)]

    def _clean(vals):
        """None/非数值 → None，其余保留 4 位小数（json.dumps 输出 NaN 非法 JSON）。"""
        out = []
        for v in vals:
            try:
                f = float(v)
            except (TypeError, ValueError):
                out.append(None)
                continue
            if math.isnan(f) or math.isinf(f):
                out.append(None)
            else:
                out.append(round(f, 4))
        return out

    results = {}

    # ---- 线性回归：y = a*t + b（最小二乘手写公式）----
    try:
        ts = list(range(n))
        mt = sum(ts) / n
        my = sum(y) / n
        sxy = sum((t - mt) * (v - my) for t, v in zip(ts, y))
        sxx = sum((t - mt) ** 2 for t in ts)
        slope = sxy / sxx if sxx > 1e-12 else 0.0
        intercept = my - slope * mt
        results["linear"] = {"name": "线性回归",
                             "values": _clean(slope * t + intercept for t in ts)}
    except Exception:
        pass

    # ---- ARIMA(1,1,0)+drift：diff[t] = c + φ·diff[t-1] ----
    # 股票日涨跌无自相关时 φ≈0，若无常数项预测会平线；
    # 加漂移 c（平均日涨跌）让预测反映历史趋势斜率。
    try:
        diff = [y[i] - y[i - 1] for i in range(1, n)]
        c = sum(diff) / len(diff)  # 漂移 = 平均日涨跌
        d0 = diff[:-1]
        d1 = diff[1:]
        m0 = sum(d0) / len(d0)
        m1 = sum(d1) / len(d1)
        num = sum((a - m0) * (b - m1) for a, b in zip(d0, d1))
        den = sum((a - m0) ** 2 for a in d0)
        phi = num / den if den > 1e-12 else 0.0
        phi = max(-0.99, min(0.99, phi))  # 保证平稳
        fitted = [None] * n
        for i in range(2, n):
            fitted[i] = y[i - 1] + c + phi * (y[i - 1] - y[i - 2])
        # 未来 horizon 日预测：带漂移的差分 AR(1) 递推外推
        last, prev = y[-1], y[-2]
        preds = []
        for _ in range(horizon):
            nxt = last + c + phi * (last - prev)
            preds.append(nxt)
            prev, last = last, nxt
        results["arima"] = {"name": "ARIMA(1,1,0)+drift",
                            "values": _clean(fitted),
                            "predict": _clean(preds),
                            "predict_dates": pred_dates}
    except Exception:
        pass

    # ---- ETS：Holt 线性趋势指数平滑（α/β 网格搜索最小化 SSE）----
    try:
        def _holt(alpha, beta):
            level = y[0]
            trend = y[1] - y[0] if n > 1 else 0.0
            out = [None] * n
            out[0] = y[0]
            for i in range(1, n):
                out[i] = level + trend                     # 一步拟合
                new_level = alpha * y[i] + (1 - alpha) * (level + trend)
                new_trend = beta * (new_level - level) + (1 - beta) * trend
                level, trend = new_level, new_trend
            return out

        best_a, best_b, best_sse, best_fit = 0.3, 0.1, float("inf"), None
        alphas = [round(0.05 + 0.1 * i, 2) for i in range(10)]   # 0.05~0.95
        betas = [round(0.01 + 0.05 * i, 2) for i in range(7)]    # 0.01~0.31
        for alpha in alphas:
            for beta in betas:
                f = _holt(alpha, beta)
                sse = sum((y[i] - f[i]) ** 2 for i in range(1, n))
                if sse < best_sse:
                    best_a, best_b, best_sse, best_fit = alpha, beta, sse, f
        # 未来 horizon 日预测：用最优 α/β 递推至末尾状态，水平+趋势外推
        level, trend = y[0], y[1] - y[0] if n > 1 else 0.0
        for i in range(1, n):
            new_level = best_a * y[i] + (1 - best_a) * (level + trend)
            new_trend = best_b * (new_level - level) + (1 - best_b) * trend
            level, trend = new_level, new_trend
        preds = [level + (k + 1) * trend for k in range(horizon)]
        results["ets"] = {"name": f"ETS 指数平滑(α={best_a:.2f})",
                          "values": _clean(best_fit),
                          "predict": _clean(preds),
                          "predict_dates": pred_dates}
    except Exception:
        pass

    # ---- Prophet 轻量版：趋势 + 变点 + 傅里叶季节 ----
    try:
        pf, pp = _prophet_light(y, horizon)
        results["prophet"] = {"name": "Prophet(轻量)",
                              "values": _clean(pf),
                              "predict": _clean(pp),
                              "predict_dates": pred_dates}
    except Exception:
        pass

    # ---- SVR 轻量版：RBF 核岭回归 ----
    try:
        sf, sp = _svr_krr(y, horizon)
        results["svr"] = {"name": "SVR(核岭回归)",
                          "values": _clean(sf),
                          "predict": _clean(sp),
                          "predict_dates": pred_dates}
    except Exception:
        pass

    # ---- 随机森林轻量版：决策树 bagging ----
    try:
        rf, rp = _rf_light(y, horizon)
        results["rf"] = {"name": "随机森林(轻量)",
                         "values": _clean(rf),
                         "predict": _clean(rp),
                         "predict_dates": pred_dates}
    except Exception:
        pass

    if not results:
        return None

    # 误差指标：MAE / RMSE / R²（跳过 None 的前缀段）
    y_mean = sum(y) / n
    for key, r in results.items():
        vals = r["values"]
        idx = [i for i, v in enumerate(vals) if v is not None]
        if len(idx) < 5:
            continue
        errs = [y[i] - vals[i] for i in idx]
        mae = sum(abs(e) for e in errs) / len(errs)
        rmse = (sum(e * e for e in errs) / len(errs)) ** 0.5
        ss_res = sum(e * e for e in errs)
        ss_tot = sum((y[i] - y_mean) ** 2 for i in idx)
        r2 = 1 - ss_res / ss_tot if ss_tot > 1e-12 else 0.0
        r["mae"] = round(mae, 4)
        r["rmse"] = max(round(rmse, 4), 1e-9)  # 保底，避免 0 导致前端逆加权除零
        r["r2"] = round(r2, 4)

    return results


_AI_KEY = None

# DeepSeek 接入配置（OpenAI 兼容格式）：模型与 base_url 可在此调整
LLM_MODEL = "deepseek-v4-flash"
LLM_BASE_URL = "https://api.deepseek.com/v1"


def _get_ai_key():
    """DeepSeek API Key：环境变量 DEEPSEEK_API_KEY 或项目目录 llm_key.txt。"""
    global _AI_KEY
    if _AI_KEY is not None:
        return _AI_KEY
    _AI_KEY = os.environ.get("DEEPSEEK_API_KEY") or ""
    if not _AI_KEY:
        kf = Path(__file__).resolve().parent / "llm_key.txt"
        if kf.exists():
            _AI_KEY = kf.read_text(encoding="utf-8").strip()
    return _AI_KEY


def ai_insight(secid, name, recent):
    """基于最近 N 日行情数据调用 DeepSeek 生成简短中文技术面解读。"""
    key = _get_ai_key()
    if not key:
        return {"error": "未配置 DeepSeek API Key（设置环境变量 DEEPSEEK_API_KEY 或在项目目录 llm_key.txt 中填写）"}
    lines = []
    for r in recent:
        lines.append(
            f"{r.get('d', '')} 开{r.get('o', '')} 收{r.get('c', '')} "
            f"高{r.get('h', '')} 低{r.get('l', '')} 涨跌{r.get('ch', '')}%"
        )
    prompt = (
        "你是一名 A 股技术分析师。以下是一支股票最近 20 个交易日的行情数据"
        f"（{name}，代码 {secid}）：\n" + "\n".join(lines) +
        "\n请用 3~5 句简洁中文给出技术面解读：趋势方向、关键支撑/压力位、"
        "量价特征、短期风险提示。不要推荐买卖，不要用表格。"
    )
    try:
        last_err = None
        for attempt in range(1, 4):  # 最多 3 次，指数退避，提高成功率
            try:
                resp = _SESSION.post(
                    f"{LLM_BASE_URL.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={"model": LLM_MODEL,
                          "messages": [{"role": "user", "content": prompt}],
                          "temperature": 0.6, "max_tokens": 1200},  # 1200 大幅减小截断（断句）概率
                    timeout=20,
                )
                resp.raise_for_status()
                data = resp.json()
                choice = data["choices"][0]
                text = choice["message"]["content"].strip()
                finish = choice.get("finish_reason")
                if not text:
                    raise ValueError("模型返回空内容，重试")  # DeepSeek 偶发空响应，走重试
                if finish == "length":
                    # 输出达到 max_tokens 上限被截断（断句），走重试拿完整结果
                    raise ValueError("输出被截断，重试")
                return {"text": text}
            except Exception as exc:
                last_err = exc
                print(f"[!] AI 请求失败(第{attempt}/3次): {exc}")
                if attempt < 3:
                    time.sleep(1.5 ** attempt)  # 2s, 3s
        return {"error": f"AI 解读失败: {last_err}"}
    except Exception as exc:
        return {"error": f"AI 解读失败: {exc}"}


def save_csv(rows):
    """K线数据留档到 data/ 目录（每次查询自动保存一份）。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"kline_{datetime.now():%Y%m%d_%H%M%S}.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_COLUMNS)
        writer.writerows(rows)
    return path


# ---------------------------------------------------------------------------
# 页面模板（查询 + 结果，同一个 HTML）
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# 本地 HTTP 服务
# ---------------------------------------------------------------------------
_pending_shutdown_ts = None  # 页面关闭标记：30 秒内无新请求才退出（防刷新/拖拽误杀）


# ---------------- 用户认证（密码哈希 / 会话 / 图形验证码） ----------------
# 会话与验证码存内存：服务重启后需重新登录（可接受，数据本身在 SQLite 持久化）
_SESSIONS = {}       # token -> (user_id, expire_ts)
_SESSION_TTL = 7 * 24 * 3600  # 会话有效期 7 天
_CAPTCHAS = {}       # captcha_id -> (answer, expire_ts)
_SLIDERS = {}        # slider_id -> dict(gap_x, gap_y, colors, expire_ts) 滑块拼图缺口位置

# ---- 简易 IP 限速（防爆破/防刷验证码）----
_MAX_BODY = 1_000_000  # 请求体上限 1MB
_RATE = {}            # ip -> {kind: [timestamps], login_fail: n, lock_until: ts}
_RATE_LIMIT = {"login": 10, "register": 5, "captcha": 20, "slider": 20, "fail": 5}
_RATE_WINDOW = 60     # 60 秒窗口
_RATE_LOCK = 600      # 连续失败锁定 10 分钟
_MAX_CONCURRENCY = 50  # 并发请求上限（ThreadingHTTPServer 每请求一线程，防线程无限增长）
_conc_sem = threading.BoundedSemaphore(_MAX_CONCURRENCY)


def _client_ip(handler):
    return handler.client_address[0]


def _rate_check(ip, kind, limit=None):
    """窗口内限速。超限返回 False；连续失败达阈值则锁定。"""
    now = time.time()
    r = _RATE.setdefault(ip, {})
    if r.get("lock_until", 0) > now:
        return False
    lst = [t for t in r.get(kind, []) if now - t < _RATE_WINDOW]
    r[kind] = lst
    if len(lst) >= (limit or _RATE_LIMIT.get(kind, 20)):
        return False
    lst.append(now)
    return True


def _rate_fail(ip):
    """登录失败计数，达阈值锁定 IP。"""
    now = time.time()
    r = _RATE.setdefault(ip, {})
    r["login_fail"] = r.get("login_fail", 0) + 1
    if r["login_fail"] >= _RATE_LIMIT["fail"]:
        r["lock_until"] = now + _RATE_LOCK
        r["login_fail"] = 0


def _rate_clear(ip):
    r = _RATE.get(ip)
    if r:
        r["login_fail"] = 0
        r["lock_until"] = 0


def _cleanup_rate():
    """定期清理限速表，防止内存膨胀。"""
    now = time.time()
    for ip in [k for k, v in _RATE.items()
               if v.get("lock_until", 0) < now - _RATE_WINDOW
               and all(not v.get(kind) for kind in ("login", "register", "captcha", "slider"))]:
        del _RATE[ip]

# 滑块拼图：7x4 网格色块背景 + 缺口碎片（零依赖 SVG）
_SLIDER_W, _SLIDER_H, _SLIDER_CELL = 280, 160, 40  # 7x4 网格，每格 40x40
_SLIDER_COLORS = ["#f87171", "#fb923c", "#facc15", "#4ade80", "#2dd4bf", "#60a5fa",
                  "#a78bfa", "#f472b6", "#fbbf24", "#34d399", "#38bdf8", "#c084fc"]


def _gen_slider():
    """生成滑块拼图：随机色块网格背景 + 缺口位置。返回 (slider_id, bg_svg, piece_svg)。"""
    import urllib.parse as _up
    cols, rows = _SLIDER_W // _SLIDER_CELL, _SLIDER_H // _SLIDER_CELL
    # 随机色块矩阵
    grid = [[random.choice(_SLIDER_COLORS) for _ in range(cols)] for _ in range(rows)]
    gx = random.randint(0, cols - 1)
    gy = random.randint(0, rows - 1)
    gap_px = gx * _SLIDER_CELL
    gap_py = gy * _SLIDER_CELL
    piece_color = grid[gy][gx]
    sid = secrets.token_hex(8)
    _SLIDERS[sid] = {"gap_x": gap_px + _SLIDER_CELL // 2,  # 缺口中心 x
                     "gap_y": gap_py + _SLIDER_CELL // 2,
                     "expire_ts": time.time() + 120}
    now = time.time()
    for k in [k for k, v in _SLIDERS.items() if v["expire_ts"] < now]:
        del _SLIDERS[k]
    # 背景：全部色块，缺口处画凹槽（深色 + 虚线边框）
    parts = [f"<svg xmlns='http://www.w3.org/2000/svg' width='{_SLIDER_W}' height='{_SLIDER_H}' "
             f"viewBox='0 0 {_SLIDER_W} {_SLIDER_H}'>"]
    for ry in range(rows):
        for rx in range(cols):
            x, y = rx * _SLIDER_CELL, ry * _SLIDER_CELL
            if rx == gx and ry == gy:
                parts.append(f"<rect x='{x}' y='{y}' width='{_SLIDER_CELL}' height='{_SLIDER_CELL}' "
                             f"fill='#64748b' stroke='#1e293b' stroke-width='2' stroke-dasharray='5,4'/>")
            else:
                parts.append(f"<rect x='{x}' y='{y}' width='{_SLIDER_CELL}' height='{_SLIDER_CELL}' "
                             f"fill='{grid[ry][rx]}' stroke='#ffffff' stroke-width='1'/>")
    parts.append("</svg>")
    bg_svg = "data:image/svg+xml;utf8," + _up.quote("".join(parts))
    # 碎片：缺口格子的色块（带边框阴影）
    piece = (f"<svg xmlns='http://www.w3.org/2000/svg' width='{_SLIDER_CELL}' height='{_SLIDER_CELL}' "
             f"viewBox='0 0 {_SLIDER_CELL} {_SLIDER_CELL}'>"
             f"<rect x='0' y='0' width='{_SLIDER_CELL}' height='{_SLIDER_CELL}' fill='{piece_color}' "
             f"stroke='#1e293b' stroke-width='2'/>"
             f"<rect x='4' y='4' width='{_SLIDER_CELL - 8}' height='{_SLIDER_CELL - 8}' fill='none' "
             f"stroke='rgba(255,255,255,.55)' stroke-width='1'/></svg>")
    piece_svg = "data:image/svg+xml;utf8," + _up.quote(piece)
    return sid, bg_svg, piece_svg


def _verify_slider(slider_id, x, duration_ms, samples):
    """校验滑块拼图：位置误差 <= 12px、拖动时长 >= 300ms、轨迹点数 >= 3。通过后一次性删除。"""
    item = _SLIDERS.get(slider_id)
    if not item or item["expire_ts"] < time.time():
        return False
    try:
        x = float(x)
        duration_ms = float(duration_ms or 0)
        samples = int(samples or 0)
    except (TypeError, ValueError):
        return False
    ok = (abs(x - item["gap_x"]) <= 12 and duration_ms >= 300 and samples >= 3)
    if ok:
        del _SLIDERS[slider_id]  # 一次性
    return ok

PWD_ITER = 100_000  # PBKDF2 迭代次数（密码哈希强度）


def hash_password(password, salt=None):
    """PBKDF2-SHA256 加盐哈希。未提供盐时生成 16 字节随机盐。返回 (hash, salt)。"""
    if salt is None:
        salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), PWD_ITER)
    return dk.hex(), salt


def verify_password(password, password_hash, salt):
    dk, _ = hash_password(password, salt)
    return secrets.compare_digest(dk, password_hash)


def _auth_user(headers):
    """从 Authorization: Bearer *** 解析用户。返回 (user_id, username) 或 (None, None)。
    会话持久化：内存 miss 时回退查询 SQLite sessions 表（服务重启后仍保持登录）。"""
    auth = headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:].strip()
        item = _SESSIONS.get(token)
        if item:
            uid, exp = item
            if exp >= time.time():
                u = db.user_get(uid)
                if u:
                    return uid, u["username"]
            else:
                _SESSIONS.pop(token, None)  # 过期会话清理
        else:
            # 内存 miss → 查持久化会话（重启恢复）
            rec = db.session_get(token)
            if rec and rec["expire_ts"] >= time.time():
                _SESSIONS[token] = (rec["user_id"], rec["expire_ts"])
                u = db.user_get(rec["user_id"])
                if u:
                    return rec["user_id"], u["username"]
            elif rec:
                db.session_delete(token)  # 过期会话清库
    return None, None


def _send_auth_error(handler):
    handler._send_json({"error": "未登录或登录已过期，请重新登录"}, 401)


def _gen_captcha():
    """生成 4 位字符图形验证码（SVG，零依赖）。返回 (captcha_id, svg)。"""
    chars = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # 去掉易混淆的 0/O/1/I/L
    code = "".join(random.choices(chars, k=4))
    cid = secrets.token_hex(8)
    _CAPTCHAS[cid] = (code, time.time() + 120)  # 2 分钟有效
    # 清理过期验证码（防止内存膨胀）
    now = time.time()
    for k in [k for k, v in _CAPTCHAS.items() if v[1] < now]:
        del _CAPTCHAS[k]
    # 生成 SVG：随机旋转字符 + 干扰线，data URI 直接给前端
    W, H = 120, 44
    parts = [f"<svg xmlns='http://www.w3.org/2000/svg' width='{W}' height='{H}' viewBox='0 0 {W} {H}'>"]
    parts.append("<rect width='100%' height='100%' fill='#f3f4f6'/>")
    for _ in range(4):
        x1, y1 = random.randint(0, W), random.randint(0, H)
        x2, y2 = random.randint(0, W), random.randint(0, H)
        parts.append(f"<line x1='{x1}' y1='{y1}' x2='{x2}' y2='{y2}' stroke='#9ca3af' stroke-width='1'/>")
    for i, ch in enumerate(code):
        x = 20 + i * 24
        y = random.randint(26, 34)
        rot = random.randint(-25, 25)
        color = "#%02x%02x%02x" % (random.randint(30, 200), random.randint(30, 200), random.randint(30, 200))
        parts.append(f"<text x='{x}' y='{y}' font-size='26' font-family='monospace' font-weight='bold' "
                     f"fill='{color}' transform='rotate({rot} {x} {y})'>{ch}</text>")
    parts.append("</svg>")
    import urllib.parse as _up
    return cid, "data:image/svg+xml;utf8," + _up.quote("".join(parts))


def _cancel_pending_shutdown():
    global _pending_shutdown_ts
    _pending_shutdown_ts = None


class Handler(BaseHTTPRequestHandler):
    """查询页 + JSON API。浏览器与后端同源，后端中转东财/新浪。"""

    def log_message(self, fmt, *args):  # 默认静默；STOCK_LOG=1 时打印访问日志
        if os.environ.get("STOCK_LOG"):
            super().log_message(fmt, *args)

    def _send(self, code, content_type, body):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # 安全响应头：CSP 防 XSS 注入、X-Frame-Options 防点击劫持、nosniff 防 MIME 嗅探
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; "
                         "style-src 'self' 'unsafe-inline'; "
                         "script-src 'self' 'unsafe-inline'; connect-src 'self'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # 客户端提前断开（如刷新/关页），静默忽略

    def _send_json(self, obj, code=200):
        self._send(code, "application/json; charset=utf-8",
                   json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _send_html(self, text):
        self._send(200, "text/html; charset=utf-8", text.encode("utf-8"))

    def do_GET(self):
        if not _conc_sem.acquire(blocking=False):
            self._send_json({"error": "服务器繁忙，请稍后再试"}, 503)
            return
        try:
            self._do_GET()
        finally:
            _conc_sem.release()

    def _do_GET(self):
        _cancel_pending_shutdown()  # 任何请求都说明页面仍在用，取消待执行退出
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        try:
            if path == "/":
                # 登录/注册页（未登录入口）；功能页在 /app
                self._send_html(load_login_html())
            elif path == "/app" or path == "/index.html":
                # 功能页（需登录，前端未登录自动跳回 /）
                self._send_html(load_index_html())
            elif path == "/api/search":
                q = (params.get("q") or [""])[0].strip()
                if not q:
                    self._send_json({"error": "缺少参数 q"}, 400)
                    return
                self._send_json(search_stock(q))
            elif path == "/api/kline":
                secid = (params.get("secid") or [""])[0].strip()
                start = (params.get("start") or [""])[0].strip()
                end = (params.get("end") or [""])[0].strip()
                period = (params.get("period") or ["day"])[0].strip() or "day"
                req_name = (params.get("name") or [""])[0].strip()
                if period not in ("day", "week", "month"):
                    period = "day"
                if not secid or not start or not end:
                    self._send_json({"error": "缺少参数 secid/start/end"}, 400)
                    return
                try:
                    s = datetime.strptime(start, "%Y-%m-%d")
                    e = datetime.strptime(end, "%Y-%m-%d")
                except ValueError:
                    self._send_json({"error": "日期格式错误，应为 YYYY-MM-DD"}, 400)
                    return
                if s > e:
                    self._send_json({"error": "开始日期不能晚于结束日期"}, 400)
                    return
                if e > datetime.now():
                    self._send_json({"error": "结束日期不能晚于今天"}, 400)
                    return
                name, rows = fetch_kline(secid, s, e, period)
                if not rows:
                    self._send_json({"error": "未获取到K线数据"}, 404)
                    return
                # 名称兜底：接口没取到名称/取到内部代码（sh601088 格式）时，
                # 用前端搜索候选传来的 name（用户点选时的正确股票名）
                if not name or re.match(r"^[a-z]{2}\d{6}$", name):
                    name = req_name or name
                data = build_chart_data(rows)
                data["name"] = name
                # 三种模型拟合 + 未来10日预测（纯 Python，零第三方依赖）
                data["fit"] = compute_fits([float(r[2]) for r in rows],
                                           [r[0] for r in rows])
                try:
                    save_csv(rows)  # 留档，失败不影响响应
                except Exception:
                    pass
                self._send_json(data)
            elif path == "/api/quotes":
                secids = (params.get("secids") or [""])[0].split(",")
                if not secids or not secids[0]:
                    self._send_json({"error": "缺少 secids 参数"}, 400)
                    return
                self._send_json(fetch_quotes(secids))
            elif path == "/api/quote":
                secid = (params.get("secid") or [""])[0].strip()
                if not secid:
                    self._send_json({"error": "缺少 secid 参数"}, 400)
                    return
                q = fetch_quote(secid)
                if not q:
                    self._send_json({"error": "未获取到实时行情"}, 404)
                    return
                self._send_json(q)
            elif path == "/api/insight":
                secid = (params.get("secid") or [""])[0].strip()
                name = (params.get("name") or [""])[0].strip()
                try:
                    recent = json.loads((params.get("recent") or ["[]"])[0])
                except (ValueError, TypeError):
                    recent = []
                if not secid or not recent:
                    self._send_json({"error": "缺少参数 secid/recent"}, 400)
                    return
                self._send_json(ai_insight(secid, name, recent))
            elif path == "/api/watchlist":
                uid, uname = _auth_user(self.headers)
                if uid is None:
                    _send_auth_error(self)
                    return
                self._send_json({"items": db.watchlist_get(uid)})
            elif path == "/api/ai-cache":
                uid, uname = _auth_user(self.headers)
                if uid is None:
                    _send_auth_error(self)
                    return
                secid = (params.get("secid") or [""])[0].strip()
                period = (params.get("period") or ["day"])[0].strip() or "day"
                if not secid:
                    self._send_json({"error": "缺少参数 secid"}, 400)
                    return
                hit = db.ai_cache_get(uid, secid, period)
                self._send_json({"hit": hit is not None, "text": (hit or {}).get("text"), "ts": (hit or {}).get("ts")})
            elif path == "/api/history":
                uid, uname = _auth_user(self.headers)
                if uid is None:
                    _send_auth_error(self)
                    return
                limit = int((params.get("limit") or ["50"])[0])
                self._send_json({"items": db.history_get(uid, limit=limit)})
            elif path == "/api/captcha":
                if not _rate_check(_client_ip(self), "captcha"):
                    self._send_json({"error": "操作过于频繁，请稍后再试"}, 429)
                    return
                cid, svg = _gen_captcha()
                self._send_json({"captcha_id": cid, "svg": svg})
            elif path == "/api/slider":
                if not _rate_check(_client_ip(self), "slider"):
                    self._send_json({"error": "操作过于频繁，请稍后再试"}, 429)
                    return
                sid, bg, piece = _gen_slider()
                item = _SLIDERS.get(sid)
                self._send_json({"slider_id": sid, "bg": bg, "piece": piece,
                                 "track_w": _SLIDER_W, "track_h": _SLIDER_H,
                                 "cell": _SLIDER_CELL,
                                 "gap_y": item["gap_y"] if item else _SLIDER_CELL // 2})
            elif path == "/api/me":
                uid, uname = _auth_user(self.headers)
                if uid is None:
                    self._send_json({"logged_in": False})
                else:
                    self._send_json({"logged_in": True, "user_id": uid, "username": uname})
            elif path == "/api/shutdown":
                global _pending_shutdown_ts
                _pending_shutdown_ts = time.time()  # 标记关闭，30 秒内无新请求才退出
                self._send_json({"ok": True})
            else:
                self._send_json({"error": "404 Not Found"}, 404)
        except ConnectionError as exc:
            self._send_json({"error": f"数据源请求失败: {exc}"}, 502)
        except Exception as exc:
            self._send_json({"error": f"服务器错误: {exc}"}, 500)

    def do_POST(self):
        if not _conc_sem.acquire(blocking=False):
            self._send_json({"error": "服务器繁忙，请稍后再试"}, 503)
            return
        try:
            self._do_POST()
        finally:
            _conc_sem.release()

    def _do_POST(self):
        """用户数据写接口：自选股增删 / AI 缓存保存 / 查询历史记录。"""
        _cancel_pending_shutdown()
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/shutdown":
            global _pending_shutdown_ts
            _pending_shutdown_ts = time.time()  # 标记关闭，30 秒内无新请求才退出
            self._send_json({"ok": True})
            return

        # 读取 JSON body（前端统一 application/json 提交）
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                self._send_json({"error": "缺少请求体"}, 400)
                return
            if length > _MAX_BODY:
                self._send_json({"error": "请求体过大"}, 413)
                return
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (ValueError, TypeError):
            self._send_json({"error": "请求体不是合法 JSON"}, 400)
            return
        _cleanup_rate()
        uid, uname = _auth_user(self.headers)
        try:
            if path == "/api/register":
                if not _rate_check(_client_ip(self), "register"):
                    self._send_json({"error": "操作过于频繁，请稍后再试"}, 429)
                    return
                username = (body.get("username") or "").strip()
                password = body.get("password") or ""
                email = (body.get("email") or "").strip().lower()
                captcha_id = (body.get("captcha_id") or "").strip()
                captcha = (body.get("captcha") or "").strip().upper()
                # 滑块人机验证（防止机器人批量注册）：位置/时长/轨迹三重校验
                if not _verify_slider(body.get("slider_id"),
                                      body.get("slider_x"),
                                      body.get("slider_duration_ms"),
                                      body.get("slider_samples")):
                    self._send_json({"error": "滑块验证失败，请重试"}, 400)
                    return
                # 用户名：仅字母/数字/下划线，长度 3-20
                if not re.fullmatch(r"[A-Za-z0-9_]{3,20}", username):
                    self._send_json({"error": "用户名仅限字母/数字/下划线，长度 3-20 个字符"}, 400)
                    return
                # 用户名不能只用下划线（纯 _ 串无实际辨识度）
                if re.fullmatch(r"_+", username):
                    self._send_json({"error": "用户名不能只用下划线"}, 400)
                    return
                # 密保邮箱：必填 + 格式校验（简单规则：含 @ 且点号在 @ 后、无空格）
                if not re.fullmatch(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", email):
                    self._send_json({"error": "邮箱格式不正确"}, 400)
                    return
                if len(email) > 100:
                    self._send_json({"error": "邮箱过长"}, 400)
                    return
                # 密码：仅字母/数字（无特殊字符），长度 8-20，且必须含大写+小写+数字
                if not re.fullmatch(r"[A-Za-z0-9]{8,20}", password):
                    self._send_json({"error": "密码仅限字母/数字，长度 8-20 个字符"}, 400)
                    return
                if not (re.search(r"[A-Z]", password) and re.search(r"[a-z]", password) and re.search(r"[0-9]", password)):
                    self._send_json({"error": "密码必须同时包含大写字母、小写字母和数字"}, 400)
                    return
                item = _CAPTCHAS.get(captcha_id)
                if not item or item[1] < time.time():
                    self._send_json({"error": "验证码已过期，请刷新"}, 400)
                    return
                if item[0] != captcha:
                    self._send_json({"error": "验证码错误"}, 400)
                    return
                del _CAPTCHAS[captcha_id]  # 验证码一次性
                ph, salt = hash_password(password)
                try:
                    uid_new = db.user_create(username, ph, salt, email)
                except sqlite3.IntegrityError:
                    # 唯一约束冲突 → 用户名或邮箱已存在（分别提示）
                    if db.user_get_by_username(username):
                        self._send_json({"error": "用户名已存在，请换一个"}, 400)
                    elif db.user_get_by_email(email):
                        self._send_json({"error": "该邮箱已被使用，请换一个"}, 400)
                    else:
                        self._send_json({"error": "用户名或邮箱已存在"}, 400)
                    return
                except Exception as exc:
                    # 其他错误（如库表缺失/磁盘问题）→ 500，不误导为用户名冲突
                    print(f"[register] 创建用户失败: {exc}")
                    self._send_json({"error": "服务器错误，请稍后重试"}, 500)
                    return
                # 注册即登录（token 同时落库，重启后仍保持登录）
                token = secrets.token_hex(24)
                exp = time.time() + _SESSION_TTL
                _SESSIONS[token] = (uid_new, exp)
                db.session_set(token, uid_new, exp)
                self._send_json({"ok": True, "token": token, "user_id": uid_new, "username": username})
            elif path == "/api/login":
                ip = _client_ip(self)
                if not _rate_check(ip, "login"):
                    self._send_json({"error": "尝试过于频繁，请稍后再试"}, 429)
                    return
                username = (body.get("username") or "").strip()
                password = body.get("password") or ""
                # 滑块人机验证（防止机器人批量撞库）
                if not _verify_slider(body.get("slider_id"),
                                      body.get("slider_x"),
                                      body.get("slider_duration_ms"),
                                      body.get("slider_samples")):
                    self._send_json({"error": "滑块验证失败，请重试"}, 400)
                    return
                u = db.user_get_by_username(username)
                if not u or not verify_password(password, u["password_hash"], u["salt"]):
                    _rate_fail(ip)  # 失败计数，达阈值锁定
                    self._send_json({"error": "用户名或密码错误"}, 401)
                    return
                _rate_clear(ip)
                token = secrets.token_hex(24)
                exp = time.time() + _SESSION_TTL
                _SESSIONS[token] = (u["user_id"], exp)
                db.session_set(token, u["user_id"], exp)
                self._send_json({"ok": True, "token": token, "user_id": u["user_id"], "username": u["username"]})
            elif path == "/api/logout":
                auth = self.headers.get("Authorization", "")
                if auth.startswith("Bearer "):
                    _SESSIONS.pop(auth[7:].strip(), None)
                    db.session_delete(auth[7:].strip())
                self._send_json({"ok": True})
            elif path == "/api/change-password":
                # 修改密码：需登录 + 密保邮箱验证（不再要求原密码）+ 验证码 + 滑块人机验证
                if uid is None:
                    _send_auth_error(self)
                    return
                email = (body.get("email") or "").strip().lower()
                new_pw = body.get("new_password") or ""
                captcha_id = (body.get("captcha_id") or "").strip()
                captcha = (body.get("captcha") or "").strip().upper()
                if not (re.fullmatch(r"[A-Za-z0-9]{8,20}", new_pw)
                        and re.search(r"[A-Z]", new_pw)
                        and re.search(r"[a-z]", new_pw)
                        and re.search(r"[0-9]", new_pw)):
                    self._send_json({"error": "新密码需 8-20 位，含大小写字母和数字"}, 400)
                    return
                # 滑块人机验证（防止机器人批量改密）
                if not _verify_slider(body.get("slider_id"),
                                      body.get("slider_x"),
                                      body.get("slider_duration_ms"),
                                      body.get("slider_samples")):
                    self._send_json({"error": "滑块验证失败，请重试"}, 400)
                    return
                # 图形验证码
                item = _CAPTCHAS.get(captcha_id)
                if not item or item[1] < time.time():
                    self._send_json({"error": "验证码已过期，请刷新"}, 400)
                    return
                if item[0] != captcha:
                    self._send_json({"error": "验证码错误"}, 400)
                    return
                del _CAPTCHAS[captcha_id]  # 验证码一次性
                u = db.user_get_by_username(uname)
                if not u:
                    self._send_json({"error": "用户不存在"}, 400)
                    return
                if not u.get("email"):
                    self._send_json({"error": "该账号未设置密保邮箱，无法修改密码"}, 400)
                    return
                if u["email"] != email:
                    self._send_json({"error": "密保邮箱不正确"}, 400)
                    return
                ph, salt = hash_password(new_pw)
                db.user_update_password(uid, ph, salt)
                # 改密后使该用户所有旧会话失效（除当前）
                cur_token = ""
                auth = self.headers.get("Authorization", "")
                if auth.startswith("Bearer "):
                    cur_token = auth[7:].strip()
                for tk in [tk for tk, (uid2, _) in _SESSIONS.items() if uid2 == uid and tk != cur_token]:
                    _SESSIONS.pop(tk, None)
                db.session_delete_user(uid, keep_token=cur_token or None)
                self._send_json({"ok": True, "message": "密码已修改"})
            elif path == "/api/delete-account":
                # 注销账号：需登录 + 密保邮箱验证 + 验证码 + 滑块，删除该用户全部数据
                if uid is None:
                    _send_auth_error(self)
                    return
                email = (body.get("email") or "").strip().lower()
                captcha_id = (body.get("captcha_id") or "").strip()
                captcha = (body.get("captcha") or "").strip().upper()
                if not _verify_slider(body.get("slider_id"),
                                      body.get("slider_x"),
                                      body.get("slider_duration_ms"),
                                      body.get("slider_samples")):
                    self._send_json({"error": "滑块验证失败，请重试"}, 400)
                    return
                item = _CAPTCHAS.get(captcha_id)
                if not item or item[1] < time.time():
                    self._send_json({"error": "验证码已过期，请刷新"}, 400)
                    return
                if item[0] != captcha:
                    self._send_json({"error": "验证码错误"}, 400)
                    return
                del _CAPTCHAS[captcha_id]
                u = db.user_get_by_username(uname)
                if not u or not u.get("email"):
                    self._send_json({"error": "该账号未设置密保邮箱，无法注销"}, 400)
                    return
                if u["email"] != email:
                    self._send_json({"error": "密保邮箱不正确"}, 400)
                    return
                # 清内存会话 + 删库 + 删用户数据
                for tk in [tk for tk, (uid2, _) in _SESSIONS.items() if uid2 == uid]:
                    _SESSIONS.pop(tk, None)
                db.user_delete(uid)
                self._send_json({"ok": True, "message": "账号已注销"})
            elif path == "/api/reset-password":
                # 忘记密码：无需登录，用 账号 + 密保邮箱 + 验证码 + 滑块 重置
                username = (body.get("username") or "").strip()
                email = (body.get("email") or "").strip().lower()
                new_pw = body.get("new_password") or ""
                captcha_id = (body.get("captcha_id") or "").strip()
                captcha = (body.get("captcha") or "").strip().upper()
                if not (re.fullmatch(r"[A-Za-z0-9]{8,20}", new_pw)
                        and re.search(r"[A-Z]", new_pw)
                        and re.search(r"[a-z]", new_pw)
                        and re.search(r"[0-9]", new_pw)):
                    self._send_json({"error": "新密码需 8-20 位，含大小写字母和数字"}, 400)
                    return
                if not _verify_slider(body.get("slider_id"),
                                      body.get("slider_x"),
                                      body.get("slider_duration_ms"),
                                      body.get("slider_samples")):
                    self._send_json({"error": "滑块验证失败，请重试"}, 400)
                    return
                item = _CAPTCHAS.get(captcha_id)
                if not item or item[1] < time.time():
                    self._send_json({"error": "验证码已过期，请刷新"}, 400)
                    return
                if item[0] != captcha:
                    self._send_json({"error": "验证码错误"}, 400)
                    return
                del _CAPTCHAS[captcha_id]
                u = db.user_get_by_username(username)
                if not u or not u.get("email"):
                    self._send_json({"error": "账号不存在或未设置密保邮箱"}, 400)
                    return
                if u["email"] != email:
                    self._send_json({"error": "密保邮箱不正确"}, 400)
                    return
                ph, salt = hash_password(new_pw)
                db.user_update_password(u["user_id"], ph, salt)
                # 重置后该用户全部会话失效
                for tk in [tk for tk, (uid2, _) in _SESSIONS.items() if uid2 == u["user_id"]]:
                    _SESSIONS.pop(tk, None)
                db.session_delete_user(u["user_id"])
                self._send_json({"ok": True, "message": "密码已重置，请用新密码登录"})
            elif path == "/api/watchlist":
                if uid is None:
                    _send_auth_error(self)
                    return
                action = (body.get("action") or "").strip()
                secid = (body.get("secid") or "").strip()
                name = (body.get("name") or "").strip()
                if not secid:
                    self._send_json({"error": "缺少 secid"}, 400)
                    return
                if action == "add":
                    db.watchlist_add(uid, secid, name or secid)
                elif action == "remove":
                    db.watchlist_remove(uid, secid)
                else:
                    self._send_json({"error": "action 应为 add 或 remove"}, 400)
                    return
                self._send_json({"ok": True, "items": db.watchlist_get(uid)})
            elif path == "/api/ai-cache":
                if uid is None:
                    _send_auth_error(self)
                    return
                secid = (body.get("secid") or "").strip()
                period = (body.get("period") or "day").strip() or "day"
                text = (body.get("text") or "").strip()
                if not secid or not text:
                    self._send_json({"error": "缺少 secid 或 text"}, 400)
                    return
                db.ai_cache_set(uid, secid, period, text)
                self._send_json({"ok": True})
            elif path == "/api/history":
                if uid is None:
                    _send_auth_error(self)
                    return
                secid = (body.get("secid") or "").strip()
                name = (body.get("name") or "").strip()
                if not secid:
                    self._send_json({"error": "缺少 secid"}, 400)
                    return
                db.history_add(uid, secid, name or secid)
                self._send_json({"ok": True})
            else:
                self._send_json({"error": "404 Not Found"}, 404)
        except Exception as exc:
            self._send_json({"error": f"服务器错误: {exc}"}, 500)


def open_browser(url):
    """自动打开默认浏览器（WSL 下经 explorer.exe 调 Windows 浏览器）。"""
    import os
    import shutil
    import subprocess
    import sys

    # 1) Windows 原生：os.startfile 最稳
    if sys.platform == "win32":
        try:
            os.startfile(url)
            print(f"[i] 已自动打开浏览器: {url}")
            return
        except Exception as exc:
            print(f"[!] 自动打开浏览器失败，请手动访问: {url} ({exc})")
            return

    # 2) WSL/Linux：explorer.exe → Windows 默认浏览器
    exe = shutil.which("explorer.exe")
    if exe:
        try:
            subprocess.Popen([exe, url])
            print(f"[i] 已自动打开浏览器: {url}")
            return
        except OSError as exc:
            if exc.errno == 8:  # Exec format error：WSL interop 注册丢失
                print("[!] 自动打开浏览器失败：WSL interop 失效（Windows 程序无法执行）")
                print("    修复（一条命令，需 sudo）：")
                print("    sudo sh -c 'echo :WSLInterop:M::MZ::/init:PF > /proc/sys/fs/binfmt_misc/register'")
                print(f"    或直接手动访问: {url}")
                return
            print(f"[!] 自动打开浏览器失败，请手动访问: {url} ({exc})")
            return

    print(f"[!] 未找到 explorer.exe，请手动访问: {url}")


def _backup_db_on_start():
    """启动时自动备份数据库到 backups/db/<时间戳>/（保留最近 30 份）。
    防止服务器更新代码时数据库被覆盖/误删导致用户数据丢失。"""
    try:
        db_path = db.db_info()["db_path"]
        if not os.path.exists(db_path):
            return
        ts = time.strftime("%Y%m%d_%H%M%S")
        bak_dir = os.path.join(os.path.dirname(os.path.abspath(db_path)), "backups", "db", ts)
        os.makedirs(bak_dir, exist_ok=True)
        # 用 sqlite3 在线备份（WAL 模式下直接拷贝可能漏最新提交）
        import sqlite3 as _sqlite3
        src = _sqlite3.connect(db_path)
        dst = _sqlite3.connect(os.path.join(bak_dir, "stock_forecast.db"))
        try:
            src.backup(dst)
        finally:
            src.close(); dst.close()
        # 保留最近 30 份
        parent = os.path.dirname(bak_dir)
        try:
            dirs = sorted(d for d in os.listdir(parent) if os.path.isdir(os.path.join(parent, d)))
            for old in dirs[:-30]:
                import shutil
                shutil.rmtree(os.path.join(parent, old), ignore_errors=True)
        except OSError:
            pass
        print(f"[i] 数据库已自动备份: backups/db/{ts}")
    except Exception as exc:
        print(f"[!] 数据库自动备份失败（不影响启动）: {exc}")


def main():
    print("=" * 56)
    print("股票历史数据查询（浏览器界面 · 东方财富/新浪数据源）")
    print("=" * 56)

    db.init_db()  # 建表（幂等），数据库文件见 db.db_info()
    db.session_cleanup()  # 清理过期会话（防 sessions 表无限增长）
    print(f"[i] 数据库: {db.db_info()['db_path']}")
    _backup_db_on_start()  # 启动时自动备份数据库（防更新覆盖/误删）

    # 监听地址/端口：环境变量可覆盖（云部署用 STOCK_HOST=0.0.0.0 STOCK_PORT=8000）
    host = os.environ.get("STOCK_HOST", "127.0.0.1")
    port = int(os.environ.get("STOCK_PORT", "0"))
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True  # 请求线程守护化，客户端断开不拖垮进程
    # HTTPS：配置 STOCK_SSL_CERT / STOCK_SSL_KEY（证书 + 私钥路径）后自动启用 TLS。
    # 生产环境更推荐用 Caddy/Nginx 反向代理终结 TLS（自动证书续期更省心）。
    cert = os.environ.get("STOCK_SSL_CERT")
    key = os.environ.get("STOCK_SSL_KEY")
    if cert and key:
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        print("[i] HTTPS 已启用（STOCK_SSL_CERT/STOCK_SSL_KEY）")
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    print(f"[i] 服务已启动: {url} (监听 {host}:{port})")
    print(f"[i] 浏览器已自动打开，页面上方输入股票名称查询")
    if host == "127.0.0.1":
        print(f"[i] 关闭浏览器页面 30 秒后自动停止服务（刷新/切走不误停）")
        open_browser(url)
    else:
        print(f"[i] 公网部署模式：请确保云安全组放行 {port} 端口")
        print(f"[i] 按 Ctrl+C 停止服务（页面关闭不再自动停止）")
    try:
        server.timeout = 1
        while True:
            server.handle_request()
            # 本地模式：页面关闭 30 秒无请求自动退出；公网模式常驻
            if host == "127.0.0.1" and _pending_shutdown_ts and time.time() - _pending_shutdown_ts > 30:
                print("[i] 页面已关闭，服务自动停止（30 秒无访问）")
                break
    except KeyboardInterrupt:
        print("\n[i] 服务已停止")


if __name__ == "__main__":
    main()
