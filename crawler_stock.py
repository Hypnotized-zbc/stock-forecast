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
- 页面右下角"停止服务"可结束本地服务

运行方式：
    python3 crawler_stock.py
    浏览器自动打开 http://127.0.0.1:<port>/

依赖：requests（其余全用标准库 + 浏览器自带 Canvas）
"""
import csv
import json
import math
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


def compute_fits(closes):
    """对收盘价序列做三种模型拟合（in-sample），返回拟合值与误差指标。

    返回结构：
        {"linear": {"name", "values": [...], "mae", "rmse", "r2"}, ...}
    依赖 statsmodels/numpy；未安装或拟合失败时返回 None（前端提示不可用，
    不影响行情功能）。
    """
    try:
        import numpy as np
        from statsmodels.tsa.arima.model import ARIMA
        from statsmodels.tsa.holtwinters import ExponentialSmoothing
    except ImportError:
        return None

    y = np.array(closes, dtype=float)
    n = len(y)
    t = np.arange(n)
    if n < 10:
        return None

    results = {}

    def _clean(vals):
        """NaN/None → None，其余保留 4 位小数（json.dumps 默认输出 NaN 非法 JSON）。"""
        return [None if (v is None or (isinstance(v, float) and math.isnan(v)))
                else round(float(v), 4) for v in vals]

    # 线性回归：y = a*t + b（最小二乘；polyfit 返回 [斜率, 截距]）
    try:
        slope, intercept = np.polyfit(t, y, 1)
        results["linear"] = {"name": "线性回归",
                             "values": _clean(slope * t + intercept)}
    except Exception:
        pass

    # ARIMA(1,1,0)：一阶差分 + 一阶自回归，适合有趋势的价格序列
    try:
        model = ARIMA(y, order=(1, 1, 0))
        fitted = model.fit()
        vals = fitted.fittedvalues
        vals[0] = np.nan  # 差分后首项无定义
        results["arima"] = {"name": "ARIMA(1,1,0)",
                            "values": _clean(vals)}
    except Exception:
        pass

    # ETS：Holt 线性趋势指数平滑
    try:
        model = ExponentialSmoothing(y, trend="add", damped_trend=False)
        fitted = model.fit()
        results["ets"] = {"name": "ETS 指数平滑",
                          "values": _clean(fitted.fittedvalues)}
    except Exception:
        pass

    if not results:
        return None

    # 误差指标：MAE / RMSE / R²（跳过 NaN 的前缀段）
    y_mean = float(y.mean())
    for key, r in results.items():
        vals = np.array(r["values"], dtype=float)
        mask = ~np.isnan(vals)
        if mask.sum() < 5:
            continue
        err = y[mask] - vals[mask]
        mae = float(np.mean(np.abs(err)))
        rmse = float(np.sqrt(np.mean(err ** 2)))
        ss_res = float(np.sum(err ** 2))
        ss_tot = float(np.sum((y[mask] - y_mean) ** 2))
        r2 = 1 - ss_res / ss_tot if ss_tot > 1e-12 else 0.0
        r["mae"] = round(mae, 4)
        r["rmse"] = round(rmse, 4)
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
  .stop-btn { margin-left: auto; font-size: 12px; color: #999; background: none;
               border: 1px solid #ddd; border-radius: 6px; padding: 5px 10px; cursor: pointer; }
  .tabs { margin-bottom: 10px; }
  .tab { padding: 7px 18px; font-size: 14px; border: 1px solid #d1d5db; background: #fff;
         border-radius: 6px 6px 0 0; cursor: pointer; margin-right: 4px; }
  .tab.active { background: #2563eb; color: #fff; border-color: #2563eb; }
  #fitPanel .f-title { font-size: 14px; font-weight: 600; margin-bottom: 8px; }
  #fitPanel .f-row { display: flex; justify-content: space-between; padding: 5px 0;
         border-bottom: 1px dashed #e5e7eb; font-size: 13px; }
  #fitPanel .f-row .f-name { display: flex; align-items: center; gap: 6px; }
  #fitPanel .swatch { display: inline-block; width: 18px; height: 4px; border-radius: 2px; }
  #fitPanel .f-metrics { font-size: 13px; color: #666; margin-top: 6px; }
  #fitPanel .f-metrics div { padding: 2px 0; }
  #fitPanel .f-metrics b { font-weight: 600; }
  #candidateBox { margin-top: 8px; }
  .cand { display: inline-block; margin: 4px 6px 0 0; padding: 6px 12px; font-size: 13px;
          border: 1px solid #d1d5db; border-radius: 6px; background: #fff; cursor: pointer; }
  .cand:hover { border-color: #2563eb; color: #2563eb; }
  #status { font-size: 13px; color: #888; margin-left: 6px; }
  .row2 { max-width: 1180px; margin: 10px auto 0; display: flex; align-items: center; gap: 16px; }
  select { padding: 7px 12px; font-size: 14px; border: 1px solid #ccc; border-radius: 6px; background: #fff; }
  .sub { color: #888; font-size: 13px; }
  .wrap { max-width: 1180px; margin: 10px auto 0; background: #fff; border-radius: 10px;
          box-shadow: 0 2px 8px rgba(0,0,0,.06); padding: 12px;
          display: flex; gap: 14px; align-items: flex-start; }
  .chart-col { flex: 1; min-width: 0; }
  canvas { display: block; cursor: crosshair; max-width: 100%; height: auto; }
  #tip { width: 260px; flex: 0 0 260px; font-size: 14px; line-height: 2.0; color: #333;
          background: #fafbfc; border-left: 3px solid #e5e7eb; padding: 12px 14px;
          min-height: 260px; }
  #tip .r { display: flex; justify-content: space-between; }
  #tip .lbl { color: #888; }
  #tip .val { font-weight: 600; }
  .legend { font-size: 12px; color: #666; margin-top: 6px; }
  .legend span { margin-right: 14px; }
  .up { color: #e03434; } .down { color: #089981; }
  .empty { color: #bbb; text-align: center; padding: 60px 0; font-size: 14px; }
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
    <button class="stop-btn" id="stopBtn" title="结束本地服务">停止服务</button>
  </div>
  <div id="candidateBox"></div>
</div>
<div class="tabs">
  <button class="tab active" id="tabChart">行情图表</button>
  <button class="tab" id="tabFit">模型拟合</button>
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
    <div class="legend" id="legend"></div>
  </div>
  <div id="tip">查询后鼠标移到图上查看每日数据</div>
  <div id="fitPanel" style="display:none">
    <div class="f-title">模型拟合结果</div>
    <div id="fitBody"></div>
  </div>
</div>
<script>
"use strict";
const UP = "#e03434", DOWN = "#089981";
const W = 900, H = 620, PAD = {L:64, R:20, T:24, B:42};

const cv = document.getElementById("chart");
const ctx = cv.getContext("2d");
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
  const lo = Math.min(...D.lows), hi = Math.max(...D.highs);
  const pad = (hi-lo)*0.06 || 1; return [lo-pad, hi+pad];
}
function seriesMinMax(arr, padRatio) {
  const vals = arr.filter(v=>v!=null);
  if (!vals.length) return [0,1];
  let lo = Math.min(...vals), hi = Math.max(...vals);
  const pad = (hi-lo)*(padRatio||0.08) || 1; return [lo-pad, hi+pad];
}
function xOf(i) { return PAD.L + i * (W-PAD.L-PAD.R) / Math.max(1,n-1); }
function yOf(v, mn, mx) { return PAD.T + (mx-v) * (H-PAD.T-PAD.B) / (mx-mn); }

function drawAxes(mn, mx, ticks) {
  ctx.strokeStyle = "#f0f0f0"; ctx.fillStyle = "#888"; ctx.font = "12px sans-serif"; ctx.lineWidth = 1;
  for (let t=0; t<=ticks; t++) {
    const v = mn + (mx-mn)*t/ticks;
    const y = yOf(v, mn, mx);
    ctx.beginPath(); ctx.moveTo(PAD.L, y); ctx.lineTo(W-PAD.R, y); ctx.stroke();
    ctx.textAlign = "right"; ctx.fillText(fmt(v), PAD.L-8, y+4);
  }
  ctx.textAlign = "center";
  const step = Math.ceil(n/8);
  for (let i=0; i<n; i+=step) {
    ctx.fillText(D.dates[i], xOf(i), H-PAD.B+18);
  }
}

// ---- K线（纯蜡烛，不叠加指标） ----
function drawKline() {
  const [mn, mx] = priceMinMax();
  drawAxes(mn, mx, 5);
  const bw = Math.max(1.5, (W-PAD.L-PAD.R)/n*0.68);
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
  const bw = Math.max(1.2, (W-PAD.L-PAD.R)/n*0.6);
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
  const bw = Math.max(1.2, (W-PAD.L-PAD.R)/n*0.6);
  for (let i=0; i<n; i++) {
    ctx.fillStyle = D.changes[i] >= 0 ? UP : DOWN;
    const y = yOf(D.changes[i], lo, hi);
    const y1 = Math.min(y, y0), h1 = Math.max(1, Math.abs(y-y0));
    ctx.fillRect(xOf(i)-bw/2, y1, bw, h1);
  }
  legend("<span><i style='color:"+UP+"'>■</i> 上涨</span><span><i style='color:"+DOWN+"'>■</i> 下跌</span><span>单位：%</span>");
}

// ---- 模型拟合图（真实收盘 + ARIMA + 线性回归 + ETS）----
const FIT_COLORS = {arima: "#dc2626", linear: "#2563eb", ets: "#16a34a"};

function drawFit() {
  if (!D || !n) { paint(currentType); return; }
  const series = [["真实收盘", D.closes, "#111827", 2.0]];
  let all = [...D.closes];
  if (D.fit) {
    for (const k of ["arima", "linear", "ets"]) {
      if (D.fit[k]) {
        series.push([D.fit[k].name, D.fit[k].values, FIT_COLORS[k], 1.6]);
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
    for (const k of ["arima", "linear", "ets"]) {
      if (D.fit[k]) lg += "<span><i style='color:"+FIT_COLORS[k]+"'>—</i> "+D.fit[k].name+"</span>";
    }
  }
  legend(lg);
}

function renderFitPanel() {
  const body = document.getElementById("fitBody");
  if (!D || !n) { body.innerHTML = "<div style='color:#bbb'>请先查询股票</div>"; return; }
  if (!D.fit || !Object.keys(D.fit).length) {
    body.innerHTML = "<div style='color:#999'>模型拟合不可用（statsmodels 未安装或拟合失败）</div>";
    return;
  }
  const order = [["arima", "ARIMA 拟合"], ["linear", "线性回归"], ["ets", "ETS 指数平滑"]];
  let html = "";
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
  }
  body.innerHTML = html;
}

// ---- 视图切换 ----
let view = "chart";
function switchView(v) {
  view = v;
  document.getElementById("tabChart").className = v==="chart" ? "tab active" : "tab";
  document.getElementById("tabFit").className = v==="fit" ? "tab active" : "tab";
  document.getElementById("chartControls").style.display = v==="chart" ? "" : "none";
  document.getElementById("legend").style.display = v==="chart" ? "" : "none";
  document.getElementById("tip").style.display = v==="chart" ? "" : "none";
  const fp = document.getElementById("fitPanel");
  fp.style.display = v==="fit" ? "" : "none";
  if (v==="fit") { renderFitPanel(); drawFit(); }
  else paint(currentType);
}
document.getElementById("tabChart").addEventListener("click", () => switchView("chart"));
document.getElementById("tabFit").addEventListener("click", () => switchView("fit"));

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
}
function drawCrosshair(i) {
  const x = xOf(i);
  ctx.strokeStyle = "rgba(0,0,0,0.22)"; ctx.lineWidth = 1;
  ctx.setLineDash([4, 4]);
  ctx.beginPath(); ctx.moveTo(x, PAD.T); ctx.lineTo(x, H-PAD.B); ctx.stroke();
  ctx.setLineDash([]);
}
function drawTooltip() {
  cv.onmousemove = e => {
    if (!D || !n) return;
    const rect = cv.getBoundingClientRect();
    const px = (e.clientX-rect.left) * W / rect.width;
    const i = Math.round((px-PAD.L) * Math.max(1,n-1) / (W-PAD.L-PAD.R));
    if (i<0 || i>=n) return;
    paint(currentType);
    drawCrosshair(i);
    const ch = D.changes[i];
    const cls = ch>=0 ? 'up' : 'down';
    const row = (l, v) => "<div class='r'><span class='lbl'>"+l+"</span><span class='val'>"+v+"</span></div>";
    document.getElementById("tip").innerHTML =
      row("日期", "<b>"+D.dates[i]+"</b>") +
      row("开盘", fmt(D.opens[i])) +
      row("收盘", fmt(D.closes[i])) +
      row("最高", fmt(D.highs[i])) +
      row("最低", fmt(D.lows[i])) +
      row("成交量", fmt(D.vols[i],0)+" 手") +
      row("涨跌幅", "<span class='"+cls+"'>"+fmt(ch)+"%</span>") +
      row("MA5", fmt(D.ma5[i])) +
      row("BOLL上", fmt(D.boll_up[i])) +
      row("BOLL中", fmt(D.boll_mid[i])) +
      row("BOLL下", fmt(D.boll_low[i]));
  };
}

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
      "<button class='cand' data-i='" + i + "'>" + c["名称"] + " " + c["代码"] + " " + c["市场"] + "</button>"
    ).join("");
    window._cands = data;
    setStatus("找到 " + data.length + " 个候选，点选一个：");
  } catch (e) {
    setStatus("请求异常: " + e);
  } finally {
    btn.disabled = false;
  }
}

async function doKline(idx) {
  const c = window._cands[idx];
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
    D = data;
    n = D.dates.length;
    document.getElementById("rangeInfo").textContent =
      (data.name || c["名称"]) + " | " + D.dates[0] + " ~ " + D.dates[n-1] + " | 共 " + n + " 个交易日";
    document.getElementById("tip").innerHTML = "鼠标移到图上查看每日数据";
    document.getElementById("candidateBox").innerHTML = "";
    setStatus("完成");
    paint(currentType);
  } catch (err) {
    setStatus("请求异常或超时: " + err);
  } finally {
    clearTimeout(timer);
  }
}

document.getElementById("candidateBox").addEventListener("click", e => {
  const b = e.target.closest(".cand");
  if (b) doKline(Number(b.dataset.i));
});
document.getElementById("searchBtn").addEventListener("click", doSearch);
document.getElementById("stockInput").addEventListener("keydown", e => {
  if (e.key === "Enter") doSearch();
});
document.getElementById("chartType").addEventListener("change", e => {
  currentType = e.target.value;
  paint(currentType);
});
document.getElementById("stopBtn").addEventListener("click", () => {
  fetch("/api/shutdown").catch(()=>{});
  setStatus("服务已停止，可关闭本页");
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
})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# 本地 HTTP 服务
# ---------------------------------------------------------------------------
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
                # 三种模型拟合（statsmodels 未装则返回 null，前端提示不可用）
                data["fit"] = compute_fits([float(r[2]) for r in rows])
                try:
                    save_csv(rows)  # 留档，失败不影响响应
                except Exception:
                    pass
                self._send_json(data)
            elif path == "/api/shutdown":
                self._send_json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self._send_json({"error": "404 Not Found"}, 404)
        except ConnectionError as exc:
            self._send_json({"error": f"数据源请求失败: {exc}"}, 502)
        except Exception as exc:
            self._send_json({"error": f"服务器错误: {exc}"}, 500)


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
    print(f"[i] 按 Ctrl+C 或页面右下角「停止服务」结束")
    open_browser(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[i] 服务已停止")


if __name__ == "__main__":
    main()
