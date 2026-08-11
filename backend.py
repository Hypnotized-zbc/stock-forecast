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
import json
import math
import os
import re
import threading
import time
import urllib.parse
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

# 东方财富接口（token 是公开固定值，官网页面也在用）
# 前端页面（static/index.html），启动时读取一次并缓存
_HTML_PATH = Path(__file__).resolve().parent / "static" / "index.html"
_index_html_cache = None

def load_index_html():
    global _index_html_cache
    if _index_html_cache is None:
        _index_html_cache = _HTML_PATH.read_text(encoding="utf-8")
    return _index_html_cache


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


def get_json(url, params, retries=4):
    """GET JSON，失败重试。

    WSL 网络间歇抖动（东财接口偶发 RemoteDisconnected），重试 4 次、
    指数退避；总等待控制在约 30 秒内，避免浏览器端等待过久。
    """
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            resp = _SESSION.get(url, params=params, headers=HEADERS, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            last_err = exc
            print(f"[!] 请求失败(第{attempt}/{retries}次): {exc}")
            time.sleep(1.5 ** attempt)  # 指数退避：2s, 3s, 5s, 8s
    raise ConnectionError(f"接口请求失败: {url} ({last_err})")


# ---------------------------------------------------------------------------
# 搜索与K线（数据层）
# ---------------------------------------------------------------------------
def search_stock(name):
    """按名称/代码搜索，返回候选列表：[{名称, 代码, 市场, 分类, secid}, ...]"""
    params = {"input": name, "type": "14", "token": PUBLIC_TOKEN}
    data = get_json(SEARCH_API, params)
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
    """从新浪股票接口取名称（尽力而为，失败返回代码）。"""
    try:
        url = ("https://hq.sinajs.cn/list=" + symbol)
        resp = _SESSION.get(url, headers={
            "User-Agent": HEADERS["User-Agent"],
            "Referer": "https://finance.sina.com.cn/",
        }, timeout=10)
        text = resp.text
        # 格式: var hq_str_sh600519="贵州茅台,开盘,昨收,..."
        if "=\"" in text:
            name = text.split("=\"")[1].split(",")[0]
            if name:
                return name
    except Exception:
        pass
    return symbol


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
    data = get_json(url, params, retries=2)  # 行情类接口快速失败，尽早切换备用源
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
                                      "Referer": "https://finance.sina.com.cn"}, timeout=10)
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


def _fill_fundamentals(q):
    """基本面字段（PE/PB/市值）缺失时补齐：先东财单股、再腾讯接口。

    保险机制：东财批量成功但个别字段缺失（数据源偶发缺字段）同样执行，
    避免"一开始没拿到后面一直拿不到"。
    """
    if q is None or None not in (q.get("pe"), q.get("pb"), q.get("mktcap")):
        return q
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


def fetch_quotes(secids):
    """东财 ulist 批量实时行情（含 PE/PB/市值等基本面）；失败回退逐只新浪。"""
    secids = [s.strip() for s in secids if s and s.strip()]
    if not secids:
        return []
    try:
        url = "https://push2.eastmoney.com/api/qt/ulist.np/get"
        params = {
            "secids": ",".join(secids), "fltt": "2", "invt": "2",
            "fields": "f2,f3,f5,f6,f9,f12,f13,f14,f18,f20,f21,f23",
        }
        data = get_json(url, params, retries=3)  # 3 次重试提高基本面字段(PE/PB)命中率
        diff = (data.get("data") or {}).get("diff") or []
        out = []
        for it in diff:
            try:
                price = float(it.get("f2"))
            except (TypeError, ValueError):
                continue
            if not price or price <= 0:  # 无效行情（停牌/接口返回0）不输出，避免前端显示 000
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
                "pe": it.get("f9"),          # PE(动)
                "pb": it.get("f23"),         # PB
                "mktcap": it.get("f20"),     # 总市值(元)
                "float_mktcap": it.get("f21"),  # 流通市值(元)
            })
        if out:
            # 批量成功但个别股票基本面字段缺失 → 补齐（保险机制）
            return [_fill_fundamentals(q) for q in out]
    except Exception as exc:
        print(f"[quotes] 东财批量失败: {exc}")
    # 回退：逐只新浪（新浪无 PE/PB/市值字段）→ 依次用东财单股、腾讯接口
    # 补齐（双备源提高命中率，避免基本面信息缺失）
    fallback = [q for q in (fetch_quote(s) for s in secids) if q]
    return [_fill_fundamentals(q) for q in fallback]


def _tencent_code(secid):
    """东财 secid(1.601088) → 腾讯代码(sh601088)。"""
    prefix, code = secid.split(".")
    return ("sh" if prefix == "1" else "sz") + code


def _quote_tencent(secid):
    """腾讯行情接口 qt.gtimg.cn：取 PE/PB/市值（字段 39=PE、46=PB、44=总市值亿、
    45=流通市值亿，市值转元）。失败返回 None。"""
    try:
        resp = _SESSION.get(
            f"https://qt.gtimg.cn/q={_tencent_code(secid)}",
            headers={"Referer": "http://finance.qq.com"}, timeout=8,
        )
        resp.raise_for_status()
        raw = resp.content.decode("gbk", errors="replace")
        m = re.search(r'="([^"]+)"', raw)
        if not m:
            return None
        f = m.group(1).split("~")
        if len(f) < 47:
            return None
        def num(i):
            try:
                v = float(f[i])
                return v if v > 0 else None
            except (TypeError, ValueError, IndexError):
                return None
        return {
            "pe": num(39),
            "pb": num(46),
            "mktcap": num(44) * 1e8 if num(44) else None,       # 亿 → 元
            "float_mktcap": num(45) * 1e8 if num(45) else None,  # 亿 → 元
        }
    except Exception:
        return None


def fetch_quote(secid):
    """东财实时行情（主）→ 新浪（备）；双源都失败返回 None。"""
    try:
        q = _quote_eastmoney(secid)
        if q:
            return q
    except Exception as exc:
        print(f"[quote] 东财失败: {exc}")
    try:
        q = _quote_sina(secid)
        if q:
            return q
        print("[quote] 新浪返回空")
    except Exception as exc:
        print(f"[quote] 新浪失败: {exc}")
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


def _cancel_pending_shutdown():
    global _pending_shutdown_ts
    _pending_shutdown_ts = None


class Handler(BaseHTTPRequestHandler):
    """查询页 + JSON API。浏览器与后端同源，后端中转东财/新浪。"""

    def log_message(self, fmt, *args):  # 静默访问日志
        pass

    def _send(self, code, content_type, body):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
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
        _cancel_pending_shutdown()  # 任何请求都说明页面仍在用，取消待执行退出
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        try:
            if path == "/":
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
                name, rows = fetch_kline(secid, s, e, period)
                if not rows:
                    self._send_json({"error": "未获取到K线数据"}, 404)
                    return
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
        """支持页面卸载时 sendBeacon 发来的停止请求（关闭网页即停止服务）。"""
        _cancel_pending_shutdown()
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/shutdown":
            global _pending_shutdown_ts
            _pending_shutdown_ts = time.time()  # 标记关闭，30 秒内无新请求才退出
            self._send_json({"ok": True})
        else:
            self._send_json({"error": "404 Not Found"}, 404)


def open_browser(url):
    """用 Windows 默认浏览器打开本地地址。"""
    try:
        import subprocess
        subprocess.Popen(["explorer.exe", url])
    except Exception as exc:
        print(f"[!] 自动打开浏览器失败，请手动访问: {url} ({exc})")


def main():
    print("=" * 56)
    print("股票历史数据查询（浏览器界面 · 东方财富/新浪数据源）")
    print("=" * 56)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True  # 请求线程守护化，客户端断开不拖垮进程
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    print(f"[i] 服务已启动: {url}")
    print(f"[i] 浏览器已自动打开，页面上方输入股票名称查询")
    print(f"[i] 关闭浏览器页面 30 秒后自动停止服务（刷新/切走不误停）")
    open_browser(url)
    try:
        server.timeout = 1
        while True:
            server.handle_request()
            if _pending_shutdown_ts and time.time() - _pending_shutdown_ts > 30:
                print("[i] 页面已关闭，服务自动停止（30 秒无访问）")
                break
    except KeyboardInterrupt:
        print("\n[i] 服务已停止")


if __name__ == "__main__":
    main()
