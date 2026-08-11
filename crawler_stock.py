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
import re
import threading
import time
import urllib.parse
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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


def fetch_kline(secid, start, end):
    """下载 [start, end] 区间日K，返回 (股票名, 行列表)。

    优先东方财富；东财接口在 WSL 下偶发断连，失败时自动回退新浪备用源
    （新浪仅支持 A股：secid 以 1./0. 开头；美股/港股保持东财重试）。
    """
    try:
        return _fetch_kline_em(secid, start, end)
    except ConnectionError as exc:
        print(f"[i] 东财K线接口失败，尝试新浪备用源: {exc}")
        return _fetch_kline_sina(secid, start, end)


def _fetch_kline_em(secid, start, end):
    """东方财富 K线。"""
    params = {
        "secid": secid,
        "klt": "101",          # 101=日K
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


def build_chart_data(rows):
    """把东财行数据转成图表 JSON 结构（含 MA5/MA20/BOLL 指标）。"""
    closes = [float(r[2]) for r in rows]
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
        data = get_json(url, params, retries=2)  # 行情类接口快速失败，尽早切换备用源
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
            return out
    except Exception as exc:
        print(f"[quotes] 东财批量失败: {exc}")
    # 回退：逐只新浪（无基本面字段）
    return [q for q in (fetch_quote(s) for s in secids) if q]


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
INDEX_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>股票历史数据查询</title>
<style>
  body { font-family: "Microsoft YaHei", sans-serif; margin: 20px; background: #f7f8fa; }
  .header { max-width: 1180px; margin: 0 auto 12px; }
  h2 { margin: 0 0 10px; font-size: 20px; color: #222; }
  .query-row { display: flex; flex-wrap: wrap; align-items: center; gap: 10px;
               background: #fff; border-radius: 8px; padding: 12px 14px;
               box-shadow: 0 2px 8px rgba(0,0,0,.06); }
  .query-row label { font-size: 15px; color: #333; }
  .query-row input[type=text] { width: 260px; padding: 7px 10px; font-size: 14px;
               border: 1px solid #ccc; border-radius: 6px; }
  .query-row input[type=date] { padding: 6px 8px; font-size: 13px;
               border: 1px solid #ccc; border-radius: 6px; }
  .query-row button { padding: 8px 18px; font-size: 14px; border: none;
               border-radius: 6px; background: #2563eb; color: #fff; cursor: pointer; }
  .query-row button:hover { background: #1d4ed8; }
  .query-row button:disabled { background: #9ca3af; cursor: not-allowed; }
  .tabs { max-width: 1180px; margin: 0 auto 10px; }
  .tab { padding: 7px 18px; font-size: 14px; border: 1px solid #d1d5db; background: #fff;
         border-radius: 6px 6px 0 0; cursor: pointer; margin-right: 4px; }
  .tab.active { background: #2563eb; color: #fff; border-color: #2563eb; }
  .f-title { font-size: 14px; font-weight: 600; margin-bottom: 8px; }
  .f-row { display: flex; justify-content: space-between; align-items: center; padding: 5px 0; }
  .f-row .f-name { display: flex; align-items: center; gap: 8px; font-size: 13px; }
  .swatch { display: inline-block; width: 20px; height: 10px; border-radius: 3px; flex: none; }
  .f-metrics { font-size: 13px; color: #666; margin-top: 6px; }
  .f-metrics div { padding: 2px 0; }
  .f-metrics b { font-weight: 600; }
  .f-pred { display: flex; justify-content: space-between; align-items: center; font-size: 12px; color: #555; padding: 2px 0; }
  .f-pred .val { font-weight: 700; }
  .f-dir { font-size: 13px; font-weight: 600; padding: 3px 0; }
  .f-pred-title { font-size: 12px; color: #888; margin: 8px 0 2px; }
  .f-final { font-size: 14px; font-weight: 700; padding: 4px 0; }
  .tip-box { position: fixed; z-index: 1002; background: #fff; border: 1px solid #ddd;
             border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,.15);
             padding: 10px 12px; font-size: 13px; min-width: 220px; display: none;
             font-family: "Microsoft YaHei", sans-serif; }
  #pinTip { position: absolute; }  /* 固定窗口相对页面定位：滚动时随图表移动，不遮挡其他信息 */
  .tip-box .ft-date { font-size: 14px; font-weight: 600; margin-bottom: 6px; color: #222; }
  .tip-box .ft-row { display: flex; justify-content: space-between; gap: 12px; padding: 2px 0; }
  .tip-box .lbl { color: #888; }
  .tip-box .val { font-weight: 600; }
  #candidateBox { margin-top: 8px; }
  .cand { display: inline-block; margin: 4px 6px 0 0; padding: 6px 12px; font-size: 13px;
          border: 1px solid #d1d5db; border-radius: 6px; background: #fff; cursor: pointer; }
  .cand:hover { border-color: #2563eb; color: #2563eb; }
  .wl-add { margin: 4px 10px 0 0; padding: 6px 10px; font-size: 13px; line-height: 1;
            border: 1px solid #c7d2fe; border-radius: 6px; background: #eef2ff;
            color: #3730a3; cursor: pointer; }
  .wl-add:hover { background: #e0e7ff; }
  .wl-bar { max-width: 1180px; margin: 8px auto 0; background: #fff; border-radius: 10px;
            box-shadow: 0 2px 8px rgba(0,0,0,.06); padding: 10px 14px; }
  .wl-head { font-size: 13px; color: #999; margin-bottom: 6px; }
  .wl-head-row { color: #999; font-size: 12px; cursor: default; padding-top: 4px; padding-bottom: 4px; }
  .wl-head-row:hover { background: transparent; }
  .wl-row { display: flex; align-items: center; gap: 14px; padding: 7px 4px;
            border-top: 1px solid #f0f0f0; cursor: pointer; font-size: 13px; }
  .wl-row:first-of-type { border-top: none; }
  .wl-row:hover { background: #f8fafc; }
  .wl-name { width: 150px; font-weight: 600; color: #111827; }
  .wl-name b { color: #999; font-weight: 400; margin-left: 4px; }
  .wl-price { min-width: 72px; font-weight: 700; font-size: 15px; }
  .wl-chg { width: 72px; }
  .wl-prev { width: 62px; color: #555; }
  .wl-pe { width: 62px; color: #555; }
  .wl-pb { width: 62px; color: #555; }
  .wl-cap { width: 92px; color: #555; }
  .wl-x { margin-left: auto; color: #a5b4fc; font-weight: 700; padding: 0 6px; }
  .wl-x:hover { color: #dc2626; }
  .wl-empty { color: #bbb; font-size: 13px; padding: 4px 2px; }
  #status { font-size: 13px; color: #888; margin-left: 6px; }
  .row2 { max-width: 1180px; margin: 10px auto 0; display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
  select { padding: 7px 12px; font-size: 14px; border: 1px solid #ccc; border-radius: 6px; background: #fff; }
  .sub { color: #888; font-size: 13px; }
  .cur-stock-line { max-width: 1180px; margin: 10px auto 0; }
  .cur-stock { font-size: 22px; font-weight: 700; color: #111827; }
  .cur-stock .code { font-size: 14px; font-weight: 400; color: #999; margin-left: 6px; }
  .cur-stock .chg { font-size: 15px; font-weight: 600; margin-left: 12px; }
  .cur-stock .cur-none { font-size: 14px; font-weight: 400; color: #bbb; }
  .wrap { max-width: 1180px; margin: 10px auto 0; background: #fff; border-radius: 10px;
          box-shadow: 0 2px 8px rgba(0,0,0,.06); padding: 12px;
          display: flex; gap: 14px; align-items: flex-start; }
  .chart-col { flex: 1; min-width: 0; }
  canvas { display: block; cursor: crosshair; max-width: 100%; height: auto; }
  .legend { font-size: 12px; color: #666; margin-top: 6px; }
  .legend span { margin-right: 14px; }
  .up { color: #e03434; } .down { color: #089981; }
  .empty { color: #bbb; text-align: center; padding: 60px 0; font-size: 14px; }
  #zoomOverlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.62);
                 z-index: 1000; align-items: center; justify-content: center; }
  #zoomClose { position: fixed; top: 14px; right: 18px; width: 46px; height: 46px;
               font-size: 24px; line-height: 1; border: none; border-radius: 50%;
               background: rgba(255,255,255,.92); color: #333; cursor: pointer; z-index: 1001; }
  #zoomClose:hover { background: #fff; color: #dc2626; }
  #zoomCanvas { max-width: calc(100vw - 60px); max-height: calc(100vh - 60px);
                background: #fff; border-radius: 10px;
                box-shadow: 0 10px 40px rgba(0,0,0,.5); }
</style>
</head>
<body>
<div class="header">
  <h2>股票历史数据查询</h2>
  <div class="query-row">
    <label for="stockInput">查询股票：</label>
    <input type="text" id="stockInput" placeholder="名称或代码，如：601088 / 中国神华 / 600519" autofocus>
    <input type="date" id="startDate">
    <span style="color:#999">~</span>
    <input type="date" id="endDate">
    <button id="searchBtn">查询</button>
    <span id="status"></span>
  </div>
  <div id="candidateBox"></div>
  <div id="watchlist" class="wl-bar"></div>
</div>
<div class="cur-stock-line">
  <span class="cur-stock" id="curStock"><span class="cur-none">尚未查询股票</span></span>
</div>
<div class="tabs">
  <button class="tab active" id="tabChart">行情图表</button>
  <button class="tab" id="tabFit">模型拟合</button>
  <button class="tab" id="tabFuture">未来预测</button>
</div>
<div class="row2" id="chartControls">
  <select id="chartType">
    <option value="kline">K线图（蜡烛图）</option>
    <option value="ma5">5日均线图</option>
    <option value="boll">布林带 BOLL 图</option>
    <option value="vol">成交量 VOL 图</option>
    <option value="change">涨跌幅柱状图</option>
  </select>
  <span class="sub" id="rangeInfo"></span>
</div>
<div class="wrap">
  <div class="chart-col">
    <canvas id="chart"></canvas>
    <canvas id="futureChart" style="display:none"></canvas>
    <div class="legend" id="legend"></div>
  </div>
  <div id="fitPanel" style="display:none">
    <div class="f-title">模型拟合结果</div>
    <div id="fitBody"></div>
  </div>
  <div id="futurePanel" style="display:none">
    <div class="f-title">未来10日预测</div>
    <div id="futureBody"></div>
  </div>
</div>
<div id="chartTip" class="tip-box"></div>
<div id="fitTip" class="tip-box"></div>
<div id="futureTip" class="tip-box"></div>
<div id="pinTip" class="tip-box"></div>
<div id="zoomOverlay">
  <button id="zoomClose" title="关闭放大">✕</button>
  <canvas id="zoomCanvas"></canvas>
</div>
<script>
"use strict";
const UP = "#e03434", DOWN = "#089981";
const W = 1140, H = 620, PAD = {L:64, R:20, T:24, B:42};

const cv = document.getElementById("chart");
let ctx = cv.getContext("2d");
const dpr = window.devicePixelRatio || 1;
cv.width = W * dpr; cv.height = H * dpr;
cv.style.width = W + "px"; cv.style.height = H + "px";
ctx.scale(dpr, dpr);
ctx.lineWidth = 1;

let D = null;   // 图表数据，查询成功后赋值
let n = 0;
let currentType = "kline";

const fmt = (v, d=2) => (v==null || isNaN(v)) ? "-" : Number(v).toFixed(d);

function priceMinMax() {
  // 纵轴范围保持全局固定（放大只缩放横轴，纵轴不随可见范围重算，避免抖动）
  const lo = Math.min(...D.lows), hi = Math.max(...D.highs);
  const pad = (hi-lo)*0.06 || 1; return [lo-pad, hi+pad];
}
function seriesMinMax(arr, padRatio) {
  const vals = arr.filter(v=>v!=null);
  if (!vals.length) return [0,1];
  let lo = Math.min(...vals), hi = Math.max(...vals);
  const pad = (hi-lo)*(padRatio||0.08) || 1; return [lo-pad, hi+pad];
}
// ---- 全屏放大状态（双击图表进入；滚轮缩放横轴、左键拖拽平移）----
let ZOOM = null;       // null=普通模式；{i0, i1}=放大模式可见天数索引范围
let _zoomDrag = null;  // 拖拽平移状态 {startX, i0, i1, moved}
let _zoomDragEnded = false;  // 拖拽结束后标记：松手产生的 click 不判定为"点击图表外"
let _clickTimer = null;  // 普通模式单击/双击区分：双击到达时取消挂起的单击锁定
let _pinTipManual = null;  // pinTip 手动拖动后的相对图表偏移 {dx, dy}（放大模式生效）

// 可见天数索引范围（普通模式返回全年）
function zRange() {
  if (!ZOOM) return [0, Math.max(0, n - 1)];
  return [Math.max(0, ZOOM.i0 | 0), Math.min(Math.max(0, n - 1), ZOOM.i1 | 0)];
}
// 放大模式下可显示的最大索引（future 视图固定 10 天）
function zoomTotalIdx() { return view === "future" ? 9 : Math.max(0, n - 1); }

function xOf(i) {
  if (!ZOOM) return PAD.L + i * (W-PAD.L-PAD.R) / Math.max(1, n - 1);
  const span = Math.max(1, ZOOM.i1 - ZOOM.i0);
  return PAD.L + (i - ZOOM.i0) * (W-PAD.L-PAD.R) / span;
}
function yOf(v, mn, mx) { return PAD.T + (mx-v) * (H-PAD.T-PAD.B) / (mx-mn); }

function drawAxes(mn, mx, ticks) {
  ctx.strokeStyle = "#f0f0f0"; ctx.fillStyle = "#888"; ctx.font = "12px sans-serif"; ctx.lineWidth = 1;
  for (let t=0; t<=ticks; t++) {
    const v = mn + (mx-mn)*t/ticks;
    const y = yOf(v, mn, mx);
    ctx.beginPath(); ctx.moveTo(PAD.L, y); ctx.lineTo(W-PAD.R, y); ctx.stroke();
  }
  drawAxisLabels(mn, mx, ticks);
}
// 坐标轴标签（刻度数值 + 日期）。放大模式在 clip 外单独调用，保证坐标轴始终可见
function drawAxisLabels(mn, mx, ticks) {
  ctx.fillStyle = "#888"; ctx.font = "12px sans-serif";
  for (let t=0; t<=ticks; t++) {
    const v = mn + (mx-mn)*t/ticks;
    const y = yOf(v, mn, mx);
    ctx.textAlign = "right"; ctx.fillText(fmt(v), PAD.L-8, y+4);
  }
  ctx.textAlign = "center";
  const [a0, a1] = zRange();
  const step = Math.max(1, Math.ceil((a1 - a0) / 8));
  for (let i = a0; i <= a1; i += step) {
    ctx.fillText(D.dates[i], xOf(i), H-PAD.B+18);
  }
}

// ---- K线（纯蜡烛，不叠加指标） ----
function drawKline() {
  const [mn, mx] = priceMinMax();
  drawAxes(mn, mx, 5);
  const [b0, b1] = zRange();
  const bw = Math.max(1.5, (W-PAD.L-PAD.R)/(b1-b0+1)*0.68);
  for (let i=0; i<n; i++) {
    const up = D.closes[i] >= D.opens[i];
    ctx.strokeStyle = ctx.fillStyle = up ? UP : DOWN;
    const x = xOf(i);
    ctx.lineWidth = 1; ctx.beginPath();
    ctx.moveTo(x, yOf(D.highs[i],mn,mx)); ctx.lineTo(x, yOf(D.lows[i],mn,mx)); ctx.stroke();
    const yO = yOf(D.opens[i],mn,mx), yC = yOf(D.closes[i],mn,mx);
    const y1 = Math.min(yO,yC), h1 = Math.max(1, Math.abs(yC-yO));
    ctx.fillRect(x-bw/2, y1, bw, h1);
  }
  legend("<span><i style='color:"+UP+"'>■</i> 涨</span>"+
         "<span><i style='color:"+DOWN+"'>■</i> 跌</span>");
}

// ---- 5日均线图 ----
function drawMA5() {
  const arrs = [["收盘", D.closes, "#6b7280", 1.0], ["MA5", D.ma5, "#3b82f6", 2.0]];
  const [mn, mx] = seriesMinMax([...D.closes, ...D.ma5.filter(v=>v!=null)], 0.06);
  drawAxes(mn, mx, 5);
  for (const [nm, arr, color, lw] of arrs) {
    ctx.strokeStyle = color; ctx.lineWidth = lw; ctx.beginPath();
    let started = false;
    for (let i=0; i<n; i++) {
      if (arr[i]==null) { started = false; continue; }
      const x = xOf(i), y = yOf(arr[i], mn, mx);
      started ? ctx.lineTo(x,y) : ctx.moveTo(x,y);
      started = true;
    }
    ctx.stroke();
  }
  legend("<span><i style='color:#6b7280'>—</i> 收盘价</span><span><i style='color:#3b82f6'>—</i> MA5</span>");
}

// ---- BOLL 布林带 ----
function drawBOLL() {
  const vals = [...D.boll_up.filter(v=>v!=null), ...D.boll_low.filter(v=>v!=null)];
  const [mn, mx] = seriesMinMax(vals, 0.05);
  drawAxes(mn, mx, 5);
  ctx.fillStyle = "rgba(59,130,246,0.10)"; ctx.beginPath();
  let started = false;
  for (let i=0; i<n; i++) {
    if (D.boll_up[i]==null) { started=false; continue; }
    const x=xOf(i), y=yOf(D.boll_up[i],mn,mx);
    started ? ctx.lineTo(x,y) : ctx.moveTo(x,y); started=true;
  }
  for (let i=n-1; i>=0; i--) {
    if (D.boll_low[i]==null) continue;
    ctx.lineTo(xOf(i), yOf(D.boll_low[i],mn,mx));
  }
  ctx.closePath(); ctx.fill();
  const lines = [["BOLL上", D.boll_up, "#e03434"], ["BOLL中", D.boll_mid, "#9ca3af"],
                 ["BOLL下", D.boll_low, "#089981"]];
  for (const [nm, arr, color] of lines) {
    ctx.strokeStyle = color; ctx.lineWidth = 1.2; ctx.beginPath();
    let started=false;
    for (let i=0; i<n; i++) {
      if (arr[i]==null) { started=false; continue; }
      const x=xOf(i), y=yOf(arr[i],mn,mx);
      started ? ctx.lineTo(x,y) : ctx.moveTo(x,y); started=true;
    }
    ctx.stroke();
  }
  legend("<span><i style='color:#e03434'>—</i> 上轨</span><span><i style='color:#9ca3af'>—</i> 中轨(MA20)</span><span><i style='color:#089981'>—</i> 下轨</span>");
}

// ---- 成交量 ----
function drawVol() {
  const [mn, mx] = seriesMinMax(D.vols, 0.05);
  drawAxes(mn, mx, 4);
  const [b0, b1] = zRange();
  const bw = Math.max(1.2, (W-PAD.L-PAD.R)/(b1-b0+1)*0.6);
  for (let i=0; i<n; i++) {
    const up = D.closes[i] >= D.opens[i];
    ctx.fillStyle = up ? UP : DOWN;
    const y = yOf(D.vols[i], mn, mx);
    ctx.fillRect(xOf(i)-bw/2, y, bw, Math.max(1, H-PAD.B-y));
  }
  legend("<span><i style='color:"+UP+"'>■</i> 阳线量</span><span><i style='color:"+DOWN+"'>■</i> 阴线量</span><span>单位：手</span>");
}

// ---- 涨跌幅 ----
function drawChange() {
  const [mn, mx] = seriesMinMax(D.changes, 0.15);
  const lo = Math.min(mn, 0), hi = Math.max(mx, 0);
  drawAxes(lo, hi, 5);
  const y0 = yOf(0, lo, hi);
  ctx.strokeStyle = "#999"; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(PAD.L, y0); ctx.lineTo(W-PAD.R, y0); ctx.stroke();
  const [b0, b1] = zRange();
  const bw = Math.max(1.2, (W-PAD.L-PAD.R)/(b1-b0+1)*0.6);
  for (let i=0; i<n; i++) {
    ctx.fillStyle = D.changes[i] >= 0 ? UP : DOWN;
    const y = yOf(D.changes[i], lo, hi);
    const y1 = Math.min(y, y0), h1 = Math.max(1, Math.abs(y-y0));
    ctx.fillRect(xOf(i)-bw/2, y1, bw, h1);
  }
  legend("<span><i style='color:"+UP+"'>■</i> 上涨</span><span><i style='color:"+DOWN+"'>■</i> 下跌</span><span>单位：%</span>");
}

// ---- 模型拟合图（真实收盘 + 5 个模型）----
const FIT_COLORS = {arima: "#dc2626", ets: "#16a34a", prophet: "#f59e0b",
                    svr: "#8b5cf6", rf: "#06b6d4"};
const FIT_ORDER = ["arima", "ets", "prophet", "svr", "rf"];

function drawFit() {
  ctx.clearRect(0, 0, W, H);  // 先清空画布，避免残留上一张图（如 K 线）
  if (!D || !n) { paint(currentType); return; }
  const series = [["真实收盘", D.closes, "#111827", 2.0]];
  let all = [...D.closes];
  if (D.fit) {
    for (const k of FIT_ORDER) {
      if (D.fit[k]) {
        series.push([D.fit[k].name, D.fit[k].values, FIT_COLORS[k], 1.5]);
        all = all.concat(D.fit[k].values.filter(v=>v!=null));
      }
    }
  }
  const [mn, mx] = seriesMinMax(all, 0.06);
  drawAxes(mn, mx, 5);
  for (const [nm, arr, color, lw] of series) {
    ctx.strokeStyle = color; ctx.lineWidth = lw; ctx.beginPath();
    let started = false;
    for (let i=0; i<n; i++) {
      if (arr[i]==null) { started = false; continue; }
      const x = xOf(i), y = yOf(arr[i], mn, mx);
      started ? ctx.lineTo(x,y) : ctx.moveTo(x,y);
      started = true;
    }
    ctx.stroke();
  }
  let lg = "<span><i style='color:#111827'>—</i> 真实收盘</span>";
  if (D.fit) {
    for (const k of FIT_ORDER) {
      if (D.fit[k]) lg += "<span><i style='color:"+FIT_COLORS[k]+"'>—</i> "+D.fit[k].name+"</span>";
    }
  }
  legend(lg);
  drawPinLine("fit");
}

function renderFitPanel() {
  const body = document.getElementById("fitBody");
  if (!D || !n) { body.innerHTML = "<div style='color:#bbb'>请先查询股票</div>"; return; }
  if (!D.fit || !Object.keys(D.fit).length) {
    body.innerHTML = "<div style='color:#999'>模型拟合不可用（数据不足或拟合失败）</div>";
    return;
  }
  const order = [["arima", "ARIMA 拟合"], ["ets", "ETS 指数平滑"],
                 ["prophet", "Prophet 拟合"], ["svr", "SVR 拟合"],
                 ["rf", "随机森林拟合"]];
  // 图例：颜色对应上图各拟合线
  let html = "<div class='f-pred-title'>图例（颜色对应上图拟合线）</div>";
  html += "<div class='f-row'><span class='f-name'><span class='swatch' style='background:#111827'></span>真实收盘</span></div>";
  for (const [k, title] of order) {
    const m = D.fit[k];
    if (!m) continue;
    html +=
      "<div class='f-row'><span class='f-name'><span class='swatch' style='background:"+FIT_COLORS[k]+"'></span>"+m.name+"</span></div>" +
      "<div class='f-metrics'>" +
      "<div>MAE 平均绝对误差：<b>"+m.mae+"</b></div>" +
      "<div>RMSE 均方根误差：<b>"+m.rmse+"</b></div>" +
      "<div>R² 拟合度：<b>"+m.r2+"</b></div>" +
      "</div>";
    // 未来预测已移至独立「未来预测」视图，这里不再重复展示
  }
  body.innerHTML = html;
}

// ---- 未来预测画布（独立，画得醒目）----
const fcv = document.getElementById("futureChart");
let fctx = fcv.getContext("2d");
fcv.width = W * dpr; fcv.height = H * dpr;
fcv.style.width = W + "px"; fcv.style.height = H + "px";
fctx.scale(dpr, dpr);

function drawFuture() {
  fctx.clearRect(0, 0, W, H);
  if (!D || !D.fit || !D.fit.arima || !D.fit.arima.predict) return;
  const lastClose = D.closes[D.closes.length-1];
  const models = [["ARIMA(1,1,0)", D.fit.arima, "#dc2626"],
                  ["ETS 指数平滑", D.fit.ets, "#16a34a"],
                  ["Prophet(轻量)", D.fit.prophet, "#f59e0b"],
                  ["SVR(核岭)", D.fit.svr, "#8b5cf6"],
                  ["随机森林", D.fit.rf, "#06b6d4"]];
  const m = 10;  // 未来 10 日
  const dates = D.fit.arima.predict_dates || [];
  const padL = 70, padR = 24, padT = 60, padB = 46;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const xF = i => {
    if (!ZOOM) return padL + i * plotW / Math.max(1, m - 1);
    const span = Math.max(1, ZOOM.i1 - ZOOM.i0);
    return padL + (i - ZOOM.i0) * plotW / span;
  };

  // 大标题（醒目）+ 副标题（今日与预测区间）
  fctx.fillStyle = "#111827"; fctx.font = "bold 20px sans-serif"; fctx.textAlign = "center";
  fctx.fillText("未来 10 日预测", W / 2, 26);
  const todayStr = new Date().toISOString().slice(0, 10);
  const p0 = dates[0] || "D+1";
  const p9 = dates[dates.length-1] || "D+10";
  fctx.font = "13px sans-serif"; fctx.fillStyle = "#888";
  fctx.fillText("今日 " + todayStr + " ｜ 预测区间 " + p0 + " ~ " + p9, W / 2, 44);

  // y 范围：现价 + 所有预测（纵轴保持全局，不随可见范围重算）
  let lo = lastClose, hi = lastClose;
  for (const [nm, mo, col] of models) {
    if (!mo || !mo.predict) continue;
    for (const v of mo.predict) {
      if (v == null) continue;
      lo = Math.min(lo, v); hi = Math.max(hi, v);
    }
  }
  const pad = (hi - lo) * 0.15 || 1;
  lo -= pad; hi += pad;
  const yOf = v => padT + (hi - v) * plotH / (hi - lo);

  // 未来区背景（淡黄，醒目）
  fctx.fillStyle = "rgba(255, 235, 160, 0.25)";
  fctx.fillRect(padL, padT, plotW, plotH);

  // 网格线（标签由 drawFutureAxisLabels 单独画，放大模式在 clip 外保证可见）
  fctx.strokeStyle = "#e8e8e8"; fctx.fillStyle = "#888"; fctx.font = "12px sans-serif";
  for (let t = 0; t <= 5; t++) {
    const v = lo + (hi - lo) * t / 5;
    const y = yOf(v);
    fctx.beginPath(); fctx.moveTo(padL, y); fctx.lineTo(W - padR, y); fctx.stroke();
  }
  drawFutureAxisLabels(lo, hi);

  // 现价基准线（黑色虚线，不标文字避免遮挡）
  fctx.strokeStyle = "#111827"; fctx.lineWidth = 1.6; fctx.setLineDash([6, 4]);
  fctx.beginPath(); fctx.moveTo(padL, yOf(lastClose)); fctx.lineTo(W - padR, yOf(lastClose)); fctx.stroke();
  fctx.setLineDash([]);

  // 预测线（模型线：细线 + 小圆点，无数值标注避免拥挤）
  for (const [nm, mo, col] of models) {
    if (!mo || !mo.predict) continue;
    fctx.strokeStyle = col; fctx.lineWidth = 2; fctx.beginPath();
    let started = false;
    for (let i = 0; i < m; i++) {
      if (mo.predict[i] == null) { started = false; continue; }
      const x = xF(i), y = yOf(mo.predict[i]);
      started ? fctx.lineTo(x, y) : fctx.moveTo(x, y);
      started = true;
    }
    fctx.stroke();
    fctx.fillStyle = col;
    for (let i = 0; i < m; i++) {
      if (mo.predict[i] == null) continue;
      const x = xF(i), y = yOf(mo.predict[i]);
      fctx.beginPath(); fctx.arc(x, y, 3.5, 0, Math.PI * 2); fctx.fill();
    }
  }

  // 最终预测线（RMSE 逆加权平均，黑色粗线 + 大圆点 + 数值，醒目）
  const wp = weightedPredict();
  fctx.strokeStyle = "#111827"; fctx.lineWidth = 2.8; fctx.beginPath();
  let wstarted = false;
  for (let i = 0; i < m; i++) {
    if (wp[i] == null) { wstarted = false; continue; }
    const x = xF(i), y = yOf(wp[i]);
    wstarted ? fctx.lineTo(x, y) : fctx.moveTo(x, y);
    wstarted = true;
  }
  fctx.stroke();
  fctx.font = "bold 12px sans-serif";
  for (let i = 0; i < m; i++) {
    if (wp[i] == null) continue;
    const x = xF(i), y = yOf(wp[i]);
    fctx.fillStyle = "#111827";
    fctx.beginPath(); fctx.arc(x, y, 5, 0, Math.PI * 2); fctx.fill();
    fctx.strokeStyle = "#fff"; fctx.lineWidth = 1.5; fctx.stroke();
    fctx.fillStyle = "#111827"; fctx.textAlign = "center";
    fctx.fillText(wp[i].toFixed(2), x, y - 12);
  }
  drawPinLine("future");
}

// 未来预测视图坐标轴标签（放大模式在 clip 外调用，保证刻度/日期可见）
function drawFutureAxisLabels(lo, hi) {
  const padL = 70, padR = 24, padT = 60, padB = 46, m = 10;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const dates = D.fit.arima.predict_dates || [];
  const xF = i => {
    if (!ZOOM) return padL + i * plotW / Math.max(1, m - 1);
    const span = Math.max(1, ZOOM.i1 - ZOOM.i0);
    return padL + (i - ZOOM.i0) * plotW / span;
  };
  const yOfF = v => padT + (hi - v) * plotH / (hi - lo);
  fctx.fillStyle = "#888"; fctx.font = "12px sans-serif";
  for (let t = 0; t <= 5; t++) {
    const v = lo + (hi - lo) * t / 5;
    const y = yOfF(v);
    fctx.textAlign = "right"; fctx.fillText(v.toFixed(2), padL - 8, y + 4);
  }
  fctx.textAlign = "center";
  const fz = ZOOM ? [ZOOM.i0, ZOOM.i1] : [0, m - 1];
  const fstep = Math.max(1, Math.ceil((fz[1] - fz[0]) / 8));
  for (let i = fz[0]; i <= fz[1]; i += fstep) {
    const dt = dates[i] ? dates[i].slice(5) : ("+" + (i + 1));
    fctx.fillText(dt, xF(i), H - padB + 18);
  }
}

// 固定参考线（单击设置，最多一条；仅当前视图匹配时绘制）
function drawPinLine(v) {
  const pin = window._pin;
  if (!pin || pin.view !== v || pin.i == null) return;
  const i = pin.i;
  if (v === "future") {
    const padL = 70, padR = 24, padT = 60, padB = 46, plotW = W - padL - padR, m = 10;
    const x = ZOOM ? padL + (i - ZOOM.i0) * plotW / Math.max(1, ZOOM.i1 - ZOOM.i0)
                   : padL + i * plotW / Math.max(1, m - 1);
    fctx.strokeStyle = "rgba(17,24,39,0.5)"; fctx.setLineDash([4,4]); fctx.lineWidth = 1.2;
    fctx.beginPath(); fctx.moveTo(x, padT); fctx.lineTo(x, H - padB); fctx.stroke();
    fctx.setLineDash([]);
    return;
  }
  if (i < 0 || i >= n) return;
  ctx.strokeStyle = "rgba(17,24,39,0.5)"; ctx.setLineDash([4,4]); ctx.lineWidth = 1.2;
  ctx.beginPath(); ctx.moveTo(xOf(i), PAD.T); ctx.lineTo(xOf(i), H-PAD.B); ctx.stroke();
  ctx.setLineDash([]);
}

// 未来预测图悬停：十字线 + 浮窗显示各预测线当日值
function drawFutureTooltip() {
  fcv.onmousemove = e => {
    const tip = document.getElementById("futureTip");
    if (!D || !D.fit || !D.fit.arima || !D.fit.arima.predict) return;
    const rect = fcv.getBoundingClientRect();
    const px = (e.clientX - rect.left) * W / rect.width;
    const padL = 70, padR = 24, padT = 60, padB = 46;
    const plotW = W - padL - padR;
    const m = 10;
    const xF = j => {
      if (!ZOOM) return padL + j * plotW / Math.max(1, m - 1);
      const span = Math.max(1, ZOOM.i1 - ZOOM.i0);
      return padL + (j - ZOOM.i0) * plotW / span;
    };
    const i = Math.round((px - padL) / (plotW / Math.max(1, m - 1)));
    if (i < 0 || i >= m) { tip.style.display = "none"; return; }
    drawFuture();
    // 十字线（垂直虚线 + 该日点高亮圈）
    fctx.strokeStyle = "rgba(0,0,0,0.35)"; fctx.setLineDash([4,4]); fctx.lineWidth = 1;
    fctx.beginPath(); fctx.moveTo(xF(i), padT); fctx.lineTo(xF(i), H - padB); fctx.stroke();
    fctx.setLineDash([]);
    // 浮窗：真实收盘 + 5 模型预测 + 最终加权
    tip.innerHTML = tipContentFor("future", i);
    tip.style.display = "block";
    let tx = e.clientX + 14, ty = e.clientY - 10;
    if (tx + 260 > window.innerWidth) tx = e.clientX - 270;
    if (ty + 200 > window.innerHeight) ty = window.innerHeight - 210;
    tip.style.left = tx + "px"; tip.style.top = ty + "px";
  };
  fcv.onmouseleave = () => { document.getElementById("futureTip").style.display = "none"; };
  // 单击固定一条参考线——延时执行区分双击（双击进入放大时取消，避免冲突）
  fcv.addEventListener("click", e => {
    if (!D || !D.fit || !D.fit.arima || !D.fit.arima.predict) return;
    clearTimeout(_clickTimer);
    _clickTimer = setTimeout(() => {
      const rect = fcv.getBoundingClientRect();
      const px = (e.clientX - rect.left) * W / rect.width;
      const padL = 70, padR = 24, plotW = W - padL - padR, m = 10;
      const i = Math.round((px - padL) / (plotW / Math.max(1, m - 1)));
      if (i < 0 || i >= m) return;
      window._pin = {view: "future", i: i};
      _pinTipManual = null;  // 重新锁定后清除手动位置
      drawFuture();
      showPinTip("future");
    }, 250);
  });
}
drawFutureTooltip();

// 按 RMSE 逆加权：误差小的模型权重大
function weightedPredict() {
  const ws = {};
  let wsum = 0;
  for (const k of FIT_ORDER) {
    if (D.fit[k] && D.fit[k].predict && D.fit[k].rmse && D.fit[k].rmse > 0) {
      ws[k] = 1 / D.fit[k].rmse;
      wsum += ws[k];
    }
  }
  const pred = [];
  for (let i = 0; i < 10; i++) {
    let s = 0;
    for (const k in ws) s += ws[k] * D.fit[k].predict[i];
    pred.push(wsum > 0 ? s / wsum : null);
  }
  return pred;
}

function renderFuturePanel() {
  const body = document.getElementById("futureBody");
  if (!D || !n) { body.innerHTML = "<div style='color:#bbb'>请先查询股票</div>"; return; }
  if (!D.fit || !D.fit.arima || !D.fit.arima.predict) {
    body.innerHTML = "<div style='color:#999'>模型拟合不可用（数据不足或拟合失败）</div>";
    return;
  }
  const lastClose = D.closes[D.closes.length-1];
  // 图例：色块 + 模型名（同模型拟合板块样式），颜色对应上图预测线
  let html = "<div class='f-pred-title'>图例（颜色对应上图预测线）</div>";
  html += "<div class='f-row'><span class='f-name'><span style='display:inline-block;width:18px;height:0;border-top:3px dashed #111827;vertical-align:middle'></span>真实收盘（现价 "+lastClose.toFixed(2)+"，虚线）</span></div>";
  const models = [["arima", "ARIMA 预测", "#dc2626"],
                  ["ets", "ETS 指数平滑预测", "#16a34a"],
                  ["prophet", "Prophet 预测", "#f59e0b"],
                  ["svr", "SVR 预测", "#8b5cf6"],
                  ["rf", "随机森林预测", "#06b6d4"]];
  for (const [k, title, col] of models) {
    if (D.fit[k] && D.fit[k].predict) {
      html += "<div class='f-row'><span class='f-name'><span class='swatch' style='background:"+col+"'></span>"+title+"</span></div>";
    }
  }
  html += "<div class='f-row'><span class='f-name'><span style='display:inline-block;width:22px;height:6px;background:#111827;vertical-align:middle'></span>最终预测（加权平均，粗线）</span></div>";
  // 最终预测（RMSE 逆加权平均）
  const wp = weightedPredict();
  const lastPred = wp[wp.length-1];
  const up = lastPred >= lastClose;
  const chg = ((lastPred - lastClose) / lastClose * 100).toFixed(2);
  const p0 = D.fit.arima.predict_dates[0] || "D+1";
  const p9 = D.fit.arima.predict_dates[D.fit.arima.predict_dates.length-1] || "D+10";
  html += "<div class='f-pred-title'>最终预测（RMSE 逆加权平均）</div>";
  html += "<div class='f-final' style='color:"+(up?UP:DOWN)+"'>"+(up?"上涨 ↑":"下跌 ↓")+" ｜ 10日后 "+lastPred.toFixed(2)+"（"+chg+"%）</div>";
  html += "<div class='f-pred-title'>逐日预测（"+p0.slice(5)+" ~ "+p9.slice(5)+"）</div>";
  for (let i = 0; i < wp.length; i++) {
    const dt = D.fit.arima.predict_dates[i] ? D.fit.arima.predict_dates[i].slice(5) : ("D+"+(i+1));
    const dv = ((wp[i] - lastClose) / lastClose * 100);
    const cls = dv >= 0 ? "up" : "down";
    html += "<div class='f-pred'><span>"+dt+"</span><span class='val'>"+fmt(wp[i])+
            " <span class='"+cls+"'>("+dv.toFixed(2)+"%)</span></span></div>";
  }
  body.innerHTML = html;
}

// ---- 视图切换 ----
let view = "chart";
function switchView(v) {
  view = v;
  document.getElementById("tabChart").className = v==="chart" ? "tab active" : "tab";
  document.getElementById("tabFit").className = v==="fit" ? "tab active" : "tab";
  document.getElementById("tabFuture").className = v==="future" ? "tab active" : "tab";
  document.getElementById("chartControls").style.display = v==="chart" ? "" : "none";
  document.getElementById("legend").style.display = v==="chart" || v==="fit" ? "" : "none";
  if (v !== "chart") document.getElementById("chartTip").style.display = "none";
  document.getElementById("chart").style.display = (v==="chart" || v==="fit") ? "" : "none";
  document.getElementById("futureChart").style.display = v==="future" ? "" : "none";
  document.getElementById("fitPanel").style.display = v==="fit" ? "" : "none";
  document.getElementById("futurePanel").style.display = v==="future" ? "" : "none";
  if (v==="fit") { renderFitPanel(); drawFit(); }
  else if (v==="future") { renderFuturePanel(); drawFuture(); }
  else paint(currentType);
  showPinTip(v);
}
document.getElementById("tabChart").addEventListener("click", () => switchView("chart"));
document.getElementById("tabFit").addEventListener("click", () => switchView("fit"));
document.getElementById("tabFuture").addEventListener("click", () => switchView("future"));

function legend(html) { document.getElementById("legend").innerHTML = html; }

function paint(type) {
  ctx.clearRect(0, 0, W, H);
  if (!D || !n) {
    ctx.fillStyle = "#bbb"; ctx.font = "14px sans-serif"; ctx.textAlign = "center";
    ctx.fillText("请先在上方输入股票名称查询", W/2, H/2);
    return;
  }
  if (type=="kline") drawKline();
  else if (type=="ma5") drawMA5();
  else if (type=="boll") drawBOLL();
  else if (type=="vol") drawVol();
  else drawChange();
  drawPinLine("chart");
}
function drawCrosshair(i) {
  const x = xOf(i);
  ctx.strokeStyle = "rgba(0,0,0,0.22)"; ctx.lineWidth = 1;
  ctx.setLineDash([4, 4]);
  ctx.beginPath(); ctx.moveTo(x, PAD.T); ctx.lineTo(x, H-PAD.B); ctx.stroke();
  ctx.setLineDash([]);
}
// 统一浮窗内容：view = chart / fit / future，i = 日期索引
function tipContentFor(view, i) {
  if (view === "fit") {
    const aV = D.fit && D.fit.arima ? D.fit.arima.values[i] : null;
    const eV = D.fit && D.fit.ets ? D.fit.ets.values[i] : null;
    const pV = D.fit && D.fit.prophet ? D.fit.prophet.values[i] : null;
    const sV = D.fit && D.fit.svr ? D.fit.svr.values[i] : null;
    const rV = D.fit && D.fit.rf ? D.fit.rf.values[i] : null;
    const row = (l, v, col) => "<div class='ft-row'><span class='lbl'>"+(col?'<i style="color:'+col+'">■</i> ':'')+l+"</span><span class='val'>"+v+"</span></div>";
    let h = "<div class='ft-date'>"+D.dates[i]+"</div>";
    h += row("真实收盘", fmt(D.closes[i]));
    h += row("ARIMA 拟合", aV!=null?fmt(aV):"—", "#dc2626");
    h += row("ETS 拟合", eV!=null?fmt(eV):"—", "#16a34a");
    h += row("Prophet 拟合", pV!=null?fmt(pV):"—", "#f59e0b");
    h += row("SVR 拟合", sV!=null?fmt(sV):"—", "#8b5cf6");
    h += row("随机森林拟合", rV!=null?fmt(rV):"—", "#06b6d4");
    return h;
  }
  if (view === "future") {
    const lastClose = D.closes[D.closes.length-1];
    const dt = D.fit.arima.predict_dates[i] || ("D+"+(i+1));
    const wp = weightedPredict();
    const models = [["ARIMA", D.fit.arima.predict[i], "#dc2626"],
                    ["ETS", D.fit.ets ? D.fit.ets.predict[i] : null, "#16a34a"],
                    ["Prophet", D.fit.prophet ? D.fit.prophet.predict[i] : null, "#f59e0b"],
                    ["SVR", D.fit.svr ? D.fit.svr.predict[i] : null, "#8b5cf6"],
                    ["随机森林", D.fit.rf ? D.fit.rf.predict[i] : null, "#06b6d4"]];
    let h = "<div class='ft-date'>"+dt+"</div>";
    h += "<div class='ft-row'><span class='lbl'>真实收盘(现价)</span><span class='val'>"+lastClose.toFixed(2)+"</span></div>";
    for (const [nm, v, col] of models) {
      h += "<div class='ft-row'><span class='lbl'><i style='color:"+col+";font-style:normal'>■</i> "+nm+"</span><span class='val'>"+(v!=null?v.toFixed(2):"—")+"</span></div>";
    }
    h += "<div class='ft-row'><span class='lbl'><i style='color:#111827;font-style:normal'>■</i> 最终(加权)</span><span class='val'>"+(wp[i]!=null?wp[i].toFixed(2):"—")+"</span></div>";
    return h;
  }
  // chart
  const ch = D.changes[i];
  const cls = ch>=0 ? 'up' : 'down';
  const row2 = (l, v) => "<div class='r'><span class='lbl'>"+l+"</span><span class='val'>"+v+"</span></div>";
  return row2("日期", "<b>"+D.dates[i]+"</b>") +
    row2("开盘", fmt(D.opens[i])) +
    row2("收盘", fmt(D.closes[i])) +
    row2("最高", fmt(D.highs[i])) +
    row2("最低", fmt(D.lows[i])) +
    row2("成交量", fmt(D.vols[i],0)+" 手") +
    row2("涨跌幅", "<span class='"+cls+"'>"+fmt(ch)+"%</span>") +
    row2("MA5", fmt(D.ma5[i])) +
    row2("BOLL上", fmt(D.boll_up[i])) +
    row2("BOLL中", fmt(D.boll_mid[i])) +
    row2("BOLL下", fmt(D.boll_low[i]));
}

// 固定参考线（单击设置，最多一条；再点重选）。浮窗固定在参考线旁。
function showPinTip(v) {
  const tip = document.getElementById("pinTip");
  const pin = window._pin;
  if (!pin || pin.view !== v || !D || !n || pin.i == null) { tip.style.display = "none"; return; }
  const i = pin.i;
  const zooming = document.getElementById("zoomOverlay").style.display === "flex";
  tip.innerHTML = tipContentFor(v, i);
  if (zooming) {
    const zc = document.getElementById("zoomCanvas");
    const r = zc.getBoundingClientRect();
    // 手动拖动过：窗口保持相对图表的偏移，随图表移动但不吸附固定点
    if (_pinTipManual) {
      tip.style.position = "fixed";
      tip.style.left = (r.left + _pinTipManual.dx) + "px";
      tip.style.top = (r.top + _pinTipManual.dy) + "px";
      tip.style.display = "block";
      return;
    }
    const padL = v === "future" ? 70 : PAD.L;
    const padR = v === "future" ? 24 : PAD.R;
    const plotW = W - padL - padR;
    let x;
    if (v === "future") {
      x = ZOOM ? padL + (i - ZOOM.i0) * plotW / Math.max(1, ZOOM.i1 - ZOOM.i0)
               : padL + i * plotW / Math.max(1, 10 - 1);
    } else {
      x = xOf(i);
    }
    // 固定点移出可见范围时窗口停在边界（不消失）
    x = Math.max(padL, Math.min(W - padR, x));
    const sx = r.left + x * r.width / W;
    let tx = sx + 14;
    if (tx + 260 > window.innerWidth) tx = sx - 270;
    tip.style.position = "fixed";
    tip.style.left = tx + "px";
    tip.style.top = (r.top + 30) + "px";
    tip.style.display = "block";
    return;
  }
  // 普通模式：固定窗口相对页面（文档坐标）定位——吸附固定点，
  // 页面滚动时随图表一起移动，不遮挡滚动上来的其他信息
  const cvs = v === "future" ? fcv : cv;
  const rect = cvs.getBoundingClientRect();
  let x;
  if (v === "future") {
    const padL = 70, padR = 24, plotW = W - padL - padR, m = 10;
    x = padL + i * plotW / Math.max(1, m - 1);
  } else {
    x = xOf(i);
  }
  const sx = rect.left + x * rect.width / W;
  let tx = sx + 14;
  if (tx + 260 > window.innerWidth) tx = sx - 270;
  tip.style.position = "absolute";
  tip.style.left = (tx + window.scrollX) + "px";
  tip.style.top = (rect.top + 30 + window.scrollY) + "px";
  tip.style.display = "block";
}

function drawTooltip() {
  cv.onmousemove = e => {
    if (!D || !n) return;
    const rect = cv.getBoundingClientRect();
    const px = (e.clientX-rect.left) * W / rect.width;
    const i = Math.round((px-PAD.L) * Math.max(1,n-1) / (W-PAD.L-PAD.R));
    if (i<0 || i>=n) return;
    // 按当前视图重绘：行情视图画当前图类型，拟合视图重画拟合图（不变成K线）
    if (view === "fit") { drawFit(); }
    else { paint(currentType); }
    drawCrosshair(i);
    if (view === "fit") {
      // 拟合视图：浮动窗口显示 当日真实收盘 + 各模型拟合值
      const tip = document.getElementById("fitTip");
      tip.innerHTML = tipContentFor("fit", i);
      tip.style.display = "block";
      let tx = e.clientX + 14, ty = e.clientY - 10;
      if (tx + 260 > window.innerWidth) tx = e.clientX - 270;
      if (ty + 200 > window.innerHeight) ty = window.innerHeight - 210;
      tip.style.left = tx + "px"; tip.style.top = ty + "px";
      return;
    }
    // 行情视图：浮动窗口跟随鼠标显示每日数据（右侧固定面板已移除）
    const tip = document.getElementById("chartTip");
    tip.innerHTML = tipContentFor("chart", i);
    tip.style.display = "block";
    let tx = e.clientX + 14, ty = e.clientY - 10;
    if (tx + 260 > window.innerWidth) tx = e.clientX - 270;
    if (ty + 300 > window.innerHeight) ty = window.innerHeight - 310;
    tip.style.left = tx + "px"; tip.style.top = ty + "px";
  };
  cv.onmouseleave = () => {
    document.getElementById("fitTip").style.display = "none";
    document.getElementById("chartTip").style.display = "none";
  };
  // 单击固定一条参考线——延时执行区分双击（双击进入放大时取消，避免冲突）
  cv.addEventListener("click", e => {
    if (!D || !n) return;
    clearTimeout(_clickTimer);
    _clickTimer = setTimeout(() => {
      const rect = cv.getBoundingClientRect();
      const px = (e.clientX-rect.left) * W / rect.width;
      const i = Math.round((px-PAD.L) * Math.max(1,n-1) / (W-PAD.L-PAD.R));
      if (i<0 || i>=n) return;
      window._pin = {view: view, i: i};
      _pinTipManual = null;  // 重新锁定后清除手动位置
      if (view === "fit") { drawFit(); } else { paint(currentType); }
      showPinTip(view);
    }, 250);
  });
}

// ---- 全屏放大（双击图表进入；滚轮缩放横轴、左键拖拽平移、单击锁定）----
function enterZoom() {
  if (!D || !n) return;
  const total = zoomTotalIdx();
  ZOOM = {i0: 0, i1: total};
  _pinTipManual = null;  // 进入放大清除手动位置
  document.getElementById("zoomOverlay").style.display = "flex";
  drawZoomCanvas();
  setStatus("放大模式：滚轮缩放横轴，左键拖拽平移，单击锁定参考线，右上角 ✕ 关闭");
}

function exitZoom() {
  ZOOM = null;
  _zoomDrag = null;
  window._pin = null;  // 退出放大同时清除锁定参考线
  _pinTipManual = null;
  document.getElementById("zoomOverlay").style.display = "none";
  document.getElementById("pinTip").style.display = "none";
  document.getElementById("zoomCanvas").style.cursor = "";
}

// 放大模式当前视图的 y 轴范围（与各绘制函数内部一致，用于 clip 外画坐标轴标签）
function zoomYRange() {
  if (view === "future") {
    const lastClose = D.closes[D.closes.length - 1];
    let lo = lastClose, hi = lastClose;
    for (const k of FIT_ORDER) {
      const mo = D.fit && D.fit[k];
      if (!mo || !mo.predict) continue;
      for (const v of mo.predict) {
        if (v == null) continue;
        lo = Math.min(lo, v); hi = Math.max(hi, v);
      }
    }
    const pad = (hi - lo) * 0.15 || 1;
    return [lo - pad, hi + pad];
  }
  if (view === "fit") {
    let all = [...D.closes];
    if (D.fit) for (const k of FIT_ORDER) if (D.fit[k]) all = all.concat(D.fit[k].values.filter(v => v != null));
    return seriesMinMax(all, 0.06);
  }
  if (currentType === "kline") return priceMinMax();
  if (currentType === "ma5") return seriesMinMax([...D.closes, ...D.ma5.filter(v => v != null)], 0.06);
  if (currentType === "boll") {
    const vals = [...D.boll_up.filter(v => v != null), ...D.boll_low.filter(v => v != null)];
    return seriesMinMax(vals, 0.05);
  }
  if (currentType === "vol") return seriesMinMax(D.vols, 0.05);
  const [mn, mx] = seriesMinMax(D.changes, 0.15);
  return [Math.min(mn, 0), Math.max(mx, 0)];
}

function drawZoomCanvas() {
  const zc = document.getElementById("zoomCanvas");
  const zctx = zc.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const fw = Math.max(400, window.innerWidth - 60);
  const fh = Math.max(300, window.innerHeight - 60);
  zc.width = Math.round(fw * dpr);
  zc.height = Math.round(fh * dpr);
  zc.style.width = fw + "px";
  zc.style.height = fh + "px";
  const rw = zc.getBoundingClientRect().width || fw;
  const rh = zc.getBoundingClientRect().height || fh;
  zctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  zctx.clearRect(0, 0, fw, fh);
  zctx.scale(rw / W, rh / H);
  // 临时把全局绘制目标切到放大画布（坐标系已缩放为 W×H），复用原绘制函数
  const saveCtx = ctx, saveFctx = fctx;
  ctx = zctx; fctx = zctx;
  // 分层：clip 外先画坐标轴标签（保证放大后刻度/日期始终可见），clip 内只画数据
  const [ymn, ymx] = zoomYRange();
  if (view === "future") drawFutureAxisLabels(ymn, ymx);
  else drawAxisLabels(ymn, ymx, 5);
  zctx.save();
  // 坐标轴边界：裁剪到绘图区，放大/拖拽时图像不超出坐标轴
  if (view === "future") {
    // future 顶部保留大标题（y=0 起不裁），x/底部按绘图区
    zctx.beginPath();
    zctx.rect(70, 0, W - 70 - 24, H - 46);
    zctx.clip();
  } else {
    zctx.beginPath();
    zctx.rect(PAD.L, PAD.T, W - PAD.L - PAD.R, H - PAD.T - PAD.B);
    zctx.clip();
  }
  try {
    if (view === "future") drawFuture();
    else if (view === "fit") drawFit();
    else paint(currentType);
    drawPinLine(view);
  } finally {
    zctx.restore();
    ctx = saveCtx; fctx = saveFctx;
  }
}

// 放大模式下鼠标位置 → 天数索引（浮点）
function zoomIdxFromClientX(clientX) {
  const zc = document.getElementById("zoomCanvas");
  const rect = zc.getBoundingClientRect();
  const zx = (clientX - rect.left) * W / rect.width;
  const span = Math.max(1, ZOOM.i1 - ZOOM.i0);
  if (view === "future") {
    const padL = 70, padR = 24, plotW = W - padL - padR;
    return ZOOM.i0 + (zx - padL) * span / plotW;
  }
  return ZOOM.i0 + (zx - PAD.L) * span / (W - PAD.L - PAD.R);
}

// 放大模式下画十字线（叠加在已绘制的图上）
function drawZoomCrosshair(fi) {
  const zc = document.getElementById("zoomCanvas");
  const zctx = zc.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const rect = zc.getBoundingClientRect();
  zctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  zctx.scale(rect.width / W, rect.height / H);
  zctx.strokeStyle = "rgba(0,0,0,0.35)"; zctx.setLineDash([4,4]); zctx.lineWidth = 1;
  const span = Math.max(1, ZOOM.i1 - ZOOM.i0);
  if (view === "future") {
    const padL = 70, padR = 24, plotW = W - padL - padR;
    const x = padL + (fi - ZOOM.i0) * plotW / span;
    zctx.beginPath(); zctx.moveTo(x, 60); zctx.lineTo(x, H - 46); zctx.stroke();
  } else {
    const x = xOf(fi);
    zctx.beginPath(); zctx.moveTo(x, PAD.T); zctx.lineTo(x, H - PAD.B); zctx.stroke();
  }
  zctx.setLineDash([]);
}

(function () {
  const zc = document.getElementById("zoomCanvas");
  document.getElementById("zoomClose").addEventListener("click", exitZoom);
  // 双击图表进入放大（到达时取消挂起的单击锁定，避免单击/双击同时触发）
  cv.addEventListener("dblclick", () => { clearTimeout(_clickTimer); enterZoom(); });
  fcv.addEventListener("dblclick", () => { clearTimeout(_clickTimer); enterZoom(); });

  zc.addEventListener("mousemove", e => {
    if (!ZOOM) return;
    if (_zoomDrag) {
      const rect = zc.getBoundingClientRect();
      const dx = (e.clientX - _zoomDrag.startX) * (W / rect.width);
      const span = _zoomDrag.i1 - _zoomDrag.i0;
      const dIdx = Math.round(-dx * span / (W - PAD.L - PAD.R));
      const total = zoomTotalIdx();
      let i0 = _zoomDrag.i0 + dIdx;
      i0 = Math.max(0, Math.min(total - span, i0));
      ZOOM.i0 = i0; ZOOM.i1 = i0 + span;
      drawZoomCanvas();
      if (window._pin && window._pin.view === view) showPinTip(view);  // 固定窗口随图像移动
      _zoomDrag.moved = true;
      return;
    }
    const fi = zoomIdxFromClientX(e.clientX);
    const i = Math.round(fi);
    if (i < ZOOM.i0 || i > ZOOM.i1) return;
    drawZoomCanvas();
    drawZoomCrosshair(fi);
    const tip = view === "future" ? document.getElementById("futureTip")
              : view === "fit" ? document.getElementById("fitTip")
              : document.getElementById("chartTip");
    tip.innerHTML = tipContentFor(view, i);
    tip.style.display = "block";
    let tx = e.clientX + 14, ty = e.clientY - 10;
    if (tx + 260 > window.innerWidth) tx = e.clientX - 270;
    if (ty + 200 > window.innerHeight) ty = window.innerHeight - 210;
    tip.style.left = tx + "px"; tip.style.top = ty + "px";
  });

  zc.addEventListener("mouseleave", () => {
    if (_zoomDrag) return;
    document.getElementById("chartTip").style.display = "none";
    document.getElementById("fitTip").style.display = "none";
    document.getElementById("futureTip").style.display = "none";
  });

  // 放大模式双击锁定参考线（不用单击锁定，避免与拖拽冲突）
  zc.addEventListener("dblclick", e => {
    if (!ZOOM) return;
    const fi = zoomIdxFromClientX(e.clientX);
    const i = Math.round(fi);
    if (i < ZOOM.i0 || i > ZOOM.i1) return;
    window._pin = {view: view, i: i};
    _pinTipManual = null;  // 重新锁定后清除手动位置，窗口重新吸附固定点
    drawZoomCanvas();
    showPinTip(view);
  });

  // 滚轮：以鼠标位置为中心缩放横轴
  zc.addEventListener("wheel", e => {
    if (!ZOOM) return;
    e.preventDefault();
    const total = zoomTotalIdx();
    const span = ZOOM.i1 - ZOOM.i0;
    const fi = zoomIdxFromClientX(e.clientX);
    const i = Math.max(0, Math.min(total, fi));
    const factor = e.deltaY > 0 ? 1.3 : 0.75;
    const newSpan = Math.max(3, Math.min(total, span * factor));
    const ratio = (i - ZOOM.i0) / Math.max(1, span);
    let i0 = Math.round(i - newSpan * ratio);
    i0 = Math.max(0, Math.min(total - newSpan, i0));
    ZOOM.i0 = i0;
    ZOOM.i1 = i0 + newSpan;
    drawZoomCanvas();
    if (window._pin && window._pin.view === view) showPinTip(view);  // 固定窗口随缩放位置更新
  }, {passive: false});

  // 左键按下拖拽平移
  zc.addEventListener("mousedown", e => {
    if (!ZOOM || e.button !== 0) return;
    _zoomDrag = {startX: e.clientX, i0: ZOOM.i0, i1: ZOOM.i1, moved: false};
    zc.style.cursor = "grabbing";
  });
  window.addEventListener("mouseup", () => {
    if (_zoomDrag) {
      _zoomDrag = null;
      zc.style.cursor = "";
      // 拖拽结束：松手处（可能落在固定窗口上）随后产生的 click 不触发清除
      _zoomDragEnded = true;
    }
  });
})();

// ---- 点击图表之外：清除锁定参考线与固定窗口 ----
document.addEventListener("click", e => {
  // 放大模式拖拽刚结束：松手处的 click（可能在固定窗口上）不触发清除
  if (_zoomDragEnded) { _zoomDragEnded = false; return; }
  const t = e.target;
  if (t.closest && (t.closest("canvas") || t.closest(".tip-box") || t.closest("#zoomClose"))) return;
  if (!window._pin) return;
  window._pin = null;
  _pinTipManual = null;
  document.getElementById("pinTip").style.display = "none";
  // 重绘去掉锁定线（放大模式重画放大画布，普通模式按视图重绘）
  if (ZOOM) drawZoomCanvas();
  else if (view === "future") drawFuture();
  else if (view === "fit") drawFit();
  else paint(currentType);
});

// ---- 固定窗口可拖动 ----
(function () {
  const tip = document.getElementById("pinTip");
  let drag = null;
  tip.addEventListener("mousedown", e => {
    e.preventDefault();  // 防止拖动时选中文本
    drag = {startX: e.clientX, startY: e.clientY,
            left: parseFloat(tip.style.left) || 0,
            top: parseFloat(tip.style.top) || 0};
  });
  window.addEventListener("mousemove", e => {
    if (!drag) return;
    let nx = drag.left + (e.clientX - drag.startX);
    let ny = drag.top + (e.clientY - drag.startY);
    // 碰到图表边界即停止移动：限制在图表（放大画布/主画布）范围内
    const zooming = document.getElementById("zoomOverlay").style.display === "flex";
    const boundEl = zooming ? document.getElementById("zoomCanvas") : cv;
    const b = boundEl.getBoundingClientRect();
    if (zooming) {
      nx = Math.max(b.left + 4, Math.min(b.right - tip.offsetWidth - 4, nx));
      ny = Math.max(b.top + 4, Math.min(b.bottom - tip.offsetHeight - 4, ny));
    } else {
      nx = Math.max(b.left + window.scrollX + 4, Math.min(b.right + window.scrollX - tip.offsetWidth - 4, nx));
      ny = Math.max(b.top + window.scrollY + 4, Math.min(b.bottom + window.scrollY - tip.offsetHeight - 4, ny));
    }
    tip.style.left = nx + "px";
    tip.style.top = ny + "px";
    // 记录相对图表的偏移：放大模式下拖拽图表时窗口保持相对位置跟随
    if (zooming) _pinTipManual = {dx: nx - b.left, dy: ny - b.top};
  });
  window.addEventListener("mouseup", () => { drag = null; });
})();

// ---- 查询流程 ----
function setStatus(msg) { document.getElementById("status").textContent = msg; }

async function doSearch() {
  const q = document.getElementById("stockInput").value.trim();
  if (!q) { setStatus("请输入股票名称或代码"); return; }
  const box = document.getElementById("candidateBox");
  box.innerHTML = "";
  const btn = document.getElementById("searchBtn");
  btn.disabled = true;
  setStatus("搜索中...");
  try {
    const resp = await fetch("/api/search?q=" + encodeURIComponent(q));
    const data = await resp.json();
    if (data.error) { setStatus("搜索失败: " + data.error); return; }
    if (!data.length) { setStatus("未找到该股票"); return; }
    box.innerHTML = data.map((c, i) =>
      "<button class='cand' data-i='" + i + "'>" + c["名称"] + " " + c["代码"] + " " + c["市场"] + "</button>" +
      "<button class='wl-add' data-i='" + i + "' title='加入自选'>＋</button>"
    ).join("");
    window._cands = data;
    setStatus("找到 " + data.length + " 个候选，点选一个（＋加入自选）：");
  } catch (e) {
    setStatus("请求异常: " + e);
  } finally {
    btn.disabled = false;
  }
}

// ---- 实时行情 / 自选股 ----
let _curSecid = null;
let _quoteTimer = null;
let _watchQuotes = {};
let _watch = [];
try { _watch = JSON.parse(localStorage.getItem("wl") || "[]"); } catch (e) { _watch = []; }

function fmtAmount(v) { if (v==null || !(v>0)) return "—"; if (v>=1e8) return (v/1e8).toFixed(2)+"亿"; if (v>=1e4) return (v/1e4).toFixed(2)+"万"; return v; }

async function refreshAllQuotes() {
  // 只请求自选股：当前股标题涨跌幅不单独抓取——直接复用自选股列表数据行，
  // 自选行渲染后同步标题（同一份 _watchQuotes），保证两处永远一致
  const secids = [];
  for (const w of _watch) if (w && w.secid && !secids.includes(w.secid)) secids.push(w.secid);
  if (!secids.length) return;
  try {
    const resp = await fetch("/api/quotes?secids=" + encodeURIComponent(secids.join(",")));
    const list = await resp.json();
    if (!Array.isArray(list)) {
      setStatus("实时行情获取失败，30 秒后自动重试");
      return;
    }
    const map = {};
    for (const q of list) if (q && q.price != null) map[q.secid] = q;
    _watchQuotes = map;
    renderWatchQuotes(map);
  } catch (e) {
    setStatus("实时行情获取失败，30 秒后自动重试");
  }
}

function startQuoteTimer() {
  if (_quoteTimer) clearInterval(_quoteTimer);
  _quoteTimer = setInterval(refreshAllQuotes, 30000);
}

function setChartData(data, name, secid) {
  D = data;
  n = D.dates.length;
  _curSecid = secid;
  // 当前查询股票醒目标题：名称 + 代码 + 涨跌幅（红涨绿跌）
  // 涨跌幅不单独抓取——直接从自选股列表数据行同步（syncTitleFromWatch），
  // 自选行有值标题就有，自选行无数据标题显示 —，两处永远一致
  const code = String(secid || "").split(".")[1] || "";
  document.getElementById("curStock").innerHTML =
    (data.name || name) + "<span class='code'>" + code + "</span>" +
    "<span class='chg' style='color:#bbb;font-weight:400'>—</span>";
  document.getElementById("rangeInfo").textContent =
    (data.name || name) + " | " + D.dates[0] + " ~ " + D.dates[n-1] + " | 共 " + n + " 个交易日";
  // 注意：不清空 candidateBox——候选选项在加入自选股/重新搜索前保持显示，避免闪烁消失
  setStatus("完成");
  // 标题涨跌幅零延迟：立即用自选股列表已有数据同步（_watchQuotes 缓存），
  // 不等网络请求返回——数据更新刷新是自选股列表的事，标题只做直接显示
  syncTitleFromWatch();
  // 按当前视图完整重绘：仅 paint 只更新行情图，模型拟合/未来预测视图的
  // 画布与右侧面板都需同步重渲染（否则点击自选股切换时图和数据不更新）
  window._pin = null;  // 切换股票后旧固定参考线失效，清除
  if (view === "fit") { renderFitPanel(); drawFit(); }
  else if (view === "future") { renderFuturePanel(); drawFuture(); }
  else paint(currentType);
  showPinTip(view);
  refreshAllQuotes();
  startQuoteTimer();
}

function saveWatch() { try { localStorage.setItem("wl", JSON.stringify(_watch)); } catch (e) {} }

// 当前股标题涨跌幅直接从自选股列表数据行同步（不单独抓取当前股）：
// 自选行有有效行情时标题显示同一数值；自选行无数据时标题显示 —（两处一致）
function syncTitleFromWatch() {
  const el = document.querySelector("#curStock .chg");
  if (!el) return;
  const q = _watchQuotes[_curSecid];
  if (_curSecid && q && q.price != null && q.price > 0 && q.change_pct != null) {
    const up = q.change_pct >= 0;
    el.textContent = (up ? "+" : "") + q.change_pct.toFixed(2) + "%";
    el.style.color = up ? UP : DOWN;
    el.style.fontWeight = "600";
  } else {
    el.textContent = "—";
    el.style.color = "#bbb";
    el.style.fontWeight = "400";
  }
}
function renderWatchlist() {
  const bar = document.getElementById("watchlist");
  if (!_watch.length) { bar.innerHTML = "<div class='wl-empty'>自选股（查询候选旁点 ＋ 添加）</div>"; return; }
  bar.innerHTML = "<div class='wl-head'>自选股实时行情（每30秒统一刷新）</div>" +
    "<div class='wl-row wl-head-row'>" +
      "<span class='wl-name'>名称</span>" +
      "<span class='wl-price'>最新价</span>" +
      "<span class='wl-chg'>涨跌幅</span>" +
      "<span class='wl-prev'>昨收</span>" +
      "<span class='wl-pe'>PE</span>" +
      "<span class='wl-pb'>PB</span>" +
      "<span class='wl-cap'>总市值</span>" +
      "<span class='wl-x'></span>" +
    "</div>" + _watch.map(w =>
    "<div class='wl-row' data-secid='"+w.secid+"'>" +
      "<span class='wl-name'>"+w.name+" <b>"+w.code+"</b></span>" +
      "<span class='wl-price' style='color:#999'>正在查询数据</span>" +
      "<span class='wl-chg'></span>" +
      "<span class='wl-prev'></span>" +
      "<span class='wl-pe'></span>" +
      "<span class='wl-pb'></span><span class='wl-cap'></span>" +
      "<span class='wl-x' data-rm='"+w.secid+"'>×</span>" +
    "</div>"
  ).join("");
  renderWatchQuotes(_watchQuotes);
}
function renderWatchQuotes(map) {
  for (const row of document.querySelectorAll(".wl-row")) {
    const q = map[row.dataset.secid];
    // 无效行情（无数据/价格为0）不渲染，保持"正在查询数据"，避免显示 000
    if (!q || q.price == null || !(q.price > 0)) continue;
    const up = q.change_pct >= 0;
    const color = up ? UP : DOWN;
    const set = (cls, txt) => { const el = row.querySelector("."+cls); if (el) el.textContent = txt; };
    const pr = row.querySelector(".wl-price");
    if (pr) { pr.textContent = q.price.toFixed(2); pr.style.color = color; }
    const cg = row.querySelector(".wl-chg");
    if (cg) { cg.textContent = (q.change_pct != null && !isNaN(q.change_pct))
        ? (up?"+":"")+q.change_pct.toFixed(2)+"%" : "—"; cg.style.color = color; }
    set("wl-prev", q.prev_close>0 ? q.prev_close.toFixed(2) : "—");
    set("wl-pe", q.pe!=null ? q.pe.toFixed(2) : "—");
    set("wl-pb", q.pb!=null ? q.pb.toFixed(2) : "—");
    set("wl-cap", fmtAmount(q.mktcap));
  }
  // 自选行渲染后同步当前股标题涨跌幅（同一份数据，保证两处一致）
  syncTitleFromWatch();
}
function addWatch(secid, name, code) {
  if (_watch.some(w => w.secid === secid)) { setStatus("已在自选中"); return; }
  _watch.push({secid: secid, name: name, code: code});
  saveWatch(); renderWatchlist();
  refreshAllQuotes();
  setStatus("已加入自选: " + name + "，正在下载数据...");
  // 加入自选后自动下载该股K线并打开图表（命中缓存秒开），无需再点候选
  doKlineSecid(secid, name);
}
function removeWatch(secid) {
  _watch = _watch.filter(w => w.secid !== secid);
  saveWatch(); renderWatchlist();
}

// ---- K线缓存：已下载过的股票（含自选股）当日不再重复下载 ----
function cacheKey(secid) {
  const s = document.getElementById("startDate").value;
  const e = document.getElementById("endDate").value;
  return "kline_" + secid + "_" + s + "_" + e;
}
function getKlineCache(secid) {
  try {
    const raw = localStorage.getItem(cacheKey(secid));
    if (!raw) return null;
    const o = JSON.parse(raw);
    if (!o.ts || Date.now() - o.ts > 24*3600*1000) {
      localStorage.removeItem(cacheKey(secid));
      return null;
    }
    return o.data;
  } catch (e) { return null; }
}
function setKlineCache(secid, data) {
  try {
    localStorage.setItem(cacheKey(secid), JSON.stringify({ts: Date.now(), data: data}));
    // 清理：kline_ 缓存超过 20 个时删最旧一半
    const keys = Object.keys(localStorage).filter(k => k.startsWith("kline_"));
    if (keys.length > 20) {
      keys.sort((a, b) => (JSON.parse(localStorage.getItem(a)).ts || 0) -
                          (JSON.parse(localStorage.getItem(b)).ts || 0));
      for (let i = 0; i < 10; i++) localStorage.removeItem(keys[i]);
    }
  } catch (e) {}
}

async function doKline(idx) {
  const c = window._cands[idx];
  const cached = getKlineCache(c.secid);
  if (cached) {
    setChartData(cached, c["名称"], c.secid);
    setStatus("已用今日缓存数据（无需重新下载）");
    return;
  }
  const s = document.getElementById("startDate").value;
  const e = document.getElementById("endDate").value;
  setStatus("下载 " + c["名称"] + " 数据中（约需 10~30 秒）...");
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 90000);
  try {
    const resp = await fetch("/api/kline?secid=" + encodeURIComponent(c.secid) +
                             "&start=" + s + "&end=" + e, {signal: ctrl.signal});
    const data = await resp.json();
    if (data.error) { setStatus("下载失败: " + data.error); return; }
    setKlineCache(c.secid, data);
    setChartData(data, c["名称"], c.secid);
  } catch (err) {
    setStatus("请求异常或超时: " + err);
  } finally {
    clearTimeout(timer);
  }
}

// 自选股直接打开（跳过搜索；命中缓存不重新下载）
async function doKlineSecid(secid, name) {
  const cached = getKlineCache(secid);
  if (cached) {
    setChartData(cached, name, secid);
    setStatus("已用今日缓存数据（无需重新下载）");
    return;
  }
  const s = document.getElementById("startDate").value;
  const e = document.getElementById("endDate").value;
  setStatus("下载 " + name + " 数据中（约需 10~30 秒）...");
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 90000);
  try {
    const resp = await fetch("/api/kline?secid=" + encodeURIComponent(secid) +
                             "&start=" + s + "&end=" + e, {signal: ctrl.signal});
    const data = await resp.json();
    if (data.error) { setStatus("下载失败: " + data.error); return; }
    setKlineCache(secid, data);
    setChartData(data, name, secid);
  } catch (err) {
    setStatus("请求异常或超时: " + err);
  } finally {
    clearTimeout(timer);
  }
}

document.getElementById("candidateBox").addEventListener("click", e => {
  const add = e.target.closest(".wl-add");
  if (add) {
    const c = window._cands[Number(add.dataset.i)];
    if (c) addWatch(c.secid, c["名称"], c["代码"]);
    return;
  }
  const b = e.target.closest(".cand");
  if (b) doKline(Number(b.dataset.i));
});
document.getElementById("watchlist").addEventListener("click", e => {
  const rm = e.target.closest(".wl-x");
  if (rm && rm.dataset.rm) { removeWatch(rm.dataset.rm); return; }
  const row = e.target.closest(".wl-row");
  if (row) {
    const w = _watch.find(x => x.secid === row.dataset.secid);
    if (w) doKlineSecid(w.secid, w.name);
  }
});
document.getElementById("searchBtn").addEventListener("click", doSearch);
document.getElementById("stockInput").addEventListener("keydown", e => {
  if (e.key === "Enter") doSearch();
});
document.getElementById("chartType").addEventListener("change", e => {
  currentType = e.target.value;
  paint(currentType);
});
// 关闭/离开页面立即停止本地服务（sendBeacon 在页面卸载时可靠发送）
window.addEventListener("pagehide", () => {
  try { navigator.sendBeacon("/api/shutdown"); } catch (e) {}
});

// 初始化：默认时间范围为近一年
(function () {
  const now = new Date();
  const fmtDate = d => d.toISOString().slice(0,10);
  document.getElementById("endDate").value = fmtDate(now);
  const d = new Date(now); d.setFullYear(d.getFullYear()-1);
  document.getElementById("startDate").value = fmtDate(d);
  drawTooltip();
  paint(currentType);
  renderWatchlist();
  refreshAllQuotes();
})();
</script>
</body>
</html>
"""


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
                self._send_html(INDEX_TEMPLATE)
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
                if not secid or not start or not end:
                    self._send_json({"error": "缺少参数 secid/start/end"}, 400)
                    return
                try:
                    s = datetime.strptime(start, "%Y-%m-%d")
                    e = datetime.strptime(end, "%Y-%m-%d")
                except ValueError:
                    self._send_json({"error": "日期格式错误，应为 YYYY-MM-DD"}, 400)
                    return
                name, rows = fetch_kline(secid, s, e)
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
