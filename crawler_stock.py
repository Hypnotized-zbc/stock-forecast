# -*- coding: utf-8 -*-
"""
crawler_stock.py — 股票历史数据下载 + HTML 图表（单文件）
===========================================================
输入股票名称 → 搜索候选 → 下载近一年日K → 生成自包含 HTML 图表页并打开。

图表（下拉列表切换，每次显示一个）：
    1. K线图（叠加 5日/20日均线、BOLL 布林带）
    2. 5日均线图（收盘价 + MA5）
    3. 布林带 BOLL 图（上/中/下轨 + 区间填充）
    4. 成交量 VOL 图
    5. 涨跌幅柱状图

数据源：东方财富公开接口（无 token、无反爬，A股/美股/港股全覆盖）。
HTML 为单文件：内嵌数据 + 原生 Canvas 绘图，无任何外部依赖，离线可开。

运行方式：
    python3 crawler_stock.py
    程序先问"要下载哪个股票？"，再自动完成搜索、下载、生成图表并打开。

依赖：requests（其余全用标准库 + 浏览器自带 Canvas）
"""
import csv
import json
import math
import os
import statistics
import subprocess
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


def ask_stock_name():
    """下载前询问股票名称。"""
    name = input("要下载哪个股票的历史数据？请输入名称或代码（如：贵州茅台 / 600519 / AAPL）：").strip()
    while not name:
        name = input("输入不能为空，请输入股票名称或代码：").strip()
    return name


def choose_candidate(candidates):
    """多候选时让用户选择，返回候选 dict。"""
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
# K线下载与指标计算
# ---------------------------------------------------------------------------
def fetch_kline(secid, start, end):
    """下载 [start, end] 区间日K，返回 (股票名, 行列表)。"""
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


def ma(values, n):
    """简单移动平均；前 n-1 个位置补 None。"""
    out = [None] * len(values)
    for i in range(n - 1, len(values)):
        out[i] = round(sum(values[i - n + 1:i + 1]) / n, 3)
    return out


def boll(values, n=20, k=2.0):
    """布林带：中轨 MA(n)，上下轨 = 中轨 ± k*标准差(总体)。"""
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


# ---------------------------------------------------------------------------
# HTML 图表页生成
# ---------------------------------------------------------------------------
CHART_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>__NAME__ 近一年行情图表</title>
<style>
  body {{ font-family: "Microsoft YaHei", sans-serif; margin: 20px; background: #f7f8fa; }}
  .header {{ max-width: 1160px; margin: 0 auto 12px; display: flex; align-items: center; gap: 16px; }}
  h2 {{ margin: 0; font-size: 20px; color: #222; }}
  .sub {{ color: #888; font-size: 13px; }}
  select {{ padding: 7px 12px; font-size: 14px; border: 1px solid #ccc; border-radius: 6px; background: #fff; }}
  .wrap {{ max-width: 1160px; margin: 0 auto; background: #fff; border-radius: 10px;
          box-shadow: 0 2px 8px rgba(0,0,0,.06); padding: 12px; }}
  canvas {{ display: block; width: 100%; height: auto; cursor: crosshair; }}
  #tip {{ max-width: 1160px; margin: 10px auto 0; font-size: 13px; color: #333;
          background: #fff; border-radius: 6px; padding: 8px 12px; box-shadow: 0 1px 4px rgba(0,0,0,.08); min-height: 18px; }}
  .legend {{ font-size: 12px; color: #666; margin-top: 6px; }}
  .legend span {{ margin-right: 14px; }}
  .up {{ color: #e03434; }} .down {{ color: #089981; }}
</style>
</head>
<body>
<div class="header">
  <h2>__NAME__ 近一年行情</h2>
  <select id="chartType">
    <option value="kline">K线图（蜡烛图）</option>
    <option value="ma5">5日均线图</option>
    <option value="boll">布林带 BOLL 图</option>
    <option value="vol">成交量 VOL 图</option>
    <option value="change">涨跌幅柱状图</option>
  </select>
  <span class="sub" id="rangeInfo"></span>
</div>
<div class="wrap"><canvas id="chart"></canvas>
  <div class="legend" id="legend"></div>
</div>
<div id="tip">鼠标移到图上查看每日数据</div>
<script>
"use strict";
const D = __DATA_JSON__;
const n = D.dates.length;
const UP = "#e03434", DOWN = "#089981";
const W = 1140, H = 620, PAD = {L:64, R:20, T:24, B:42};

const cv = document.getElementById("chart");
const ctx = cv.getContext("2d");
// 高清屏适配：画布按 devicePixelRatio 放大，避免高分屏模糊
const dpr = window.devicePixelRatio || 1;
cv.width = W * dpr; cv.height = H * dpr;
cv.style.width = W + "px"; cv.style.height = H + "px";
ctx.scale(dpr, dpr);
ctx.lineWidth = 1;

const fmt = (v, d=2) => (v==null || isNaN(v)) ? "-" : Number(v).toFixed(d);

function priceMinMax() {{
  const lo = Math.min(...D.lows), hi = Math.max(...D.highs);
  const pad = (hi-lo)*0.06 || 1; return [lo-pad, hi+pad];
}}
function seriesMinMax(arr, padRatio) {{
  const vals = arr.filter(v=>v!=null);
  if (!vals.length) return [0,1];
  let lo = Math.min(...vals), hi = Math.max(...vals);
  const pad = (hi-lo)*(padRatio||0.08) || 1; return [lo-pad, hi+pad];
}}
function xOf(i) {{ return PAD.L + i * (W-PAD.L-PAD.R) / Math.max(1,n-1); }}
function yOf(v, mn, mx) {{ return PAD.T + (mx-v) * (H-PAD.T-PAD.B) / (mx-mn); }}

function drawAxes(mn, mx, ticks) {{
  ctx.strokeStyle = "#f0f0f0"; ctx.fillStyle = "#888"; ctx.font = "12px sans-serif"; ctx.lineWidth = 1;
  for (let t=0; t<=ticks; t++) {{
    const v = mn + (mx-mn)*t/ticks;
    const y = yOf(v, mn, mx);
    ctx.beginPath(); ctx.moveTo(PAD.L, y); ctx.lineTo(W-PAD.R, y); ctx.stroke();
    ctx.textAlign = "right"; ctx.fillText(fmt(v), PAD.L-8, y+4);
  }}
  // 日期刻度：约每 30 根标一个
  ctx.textAlign = "center";
  const step = Math.ceil(n/8);
  for (let i=0; i<n; i+=step) {{
    ctx.fillText(D.dates[i], xOf(i), H-PAD.B+18);
  }}
}}

let currentType = "kline";
function paint(type) {{
  ctx.clearRect(0, 0, W, H);
  if (type=="kline") drawKline();
  else if (type=="ma5") drawMA5();
  else if (type=="boll") drawBOLL();
  else if (type=="vol") drawVol();
  else drawChange();
}}
function drawCrosshair(i) {{
  const x = xOf(i);
  ctx.strokeStyle = "rgba(0,0,0,0.22)"; ctx.lineWidth = 1;
  ctx.setLineDash([4, 4]);
  ctx.beginPath(); ctx.moveTo(x, PAD.T); ctx.lineTo(x, H-PAD.B); ctx.stroke();
  ctx.setLineDash([]);
}}
function drawTooltip() {{
  cv.onmousemove = e => {{
    const rect = cv.getBoundingClientRect();
    const px = (e.clientX-rect.left) * W / rect.width;
    const i = Math.round((px-PAD.L) * Math.max(1,n-1) / (W-PAD.L-PAD.R));
    if (i<0 || i>=n) return;
    paint(currentType);
    drawCrosshair(i);
    const ch = D.changes[i];
    const cls = ch>=0 ? 'up' : 'down';
    document.getElementById("tip").innerHTML =
      "<b>"+D.dates[i]+"</b> &nbsp;开 "+fmt(D.opens[i])+" &nbsp;收 "+fmt(D.closes[i])+
      " &nbsp;高 "+fmt(D.highs[i])+" &nbsp;低 "+fmt(D.lows[i])+
      " &nbsp;量 "+fmt(D.vols[i],0)+"手 &nbsp;涨跌 <span class='"+cls+"'>"+fmt(ch)+"%</span>"+
      " &nbsp;MA5 "+fmt(D.ma5[i])+" &nbsp;BOLL上 "+fmt(D.boll_up[i])+" / 中 "+fmt(D.boll_mid[i])+" / 下 "+fmt(D.boll_low[i]);
  }};
}}

function legend(html) {{ document.getElementById("legend").innerHTML = html; }}

// ---- K线（纯蜡烛，不叠加指标） ----
function drawKline() {{
  const [mn, mx] = priceMinMax();
  drawAxes(mn, mx, 5);
  const bw = Math.max(1.5, (W-PAD.L-PAD.R)/n*0.68);
  for (let i=0; i<n; i++) {{
    const up = D.closes[i] >= D.opens[i];
    ctx.strokeStyle = ctx.fillStyle = up ? UP : DOWN;
    const x = xOf(i);
    // 影线
    ctx.lineWidth = 1; ctx.beginPath();
    ctx.moveTo(x, yOf(D.highs[i],mn,mx)); ctx.lineTo(x, yOf(D.lows[i],mn,mx)); ctx.stroke();
    // 实体
    const yO = yOf(D.opens[i],mn,mx), yC = yOf(D.closes[i],mn,mx);
    const y1 = Math.min(yO,yC), h1 = Math.max(1, Math.abs(yC-yO));
    ctx.fillRect(x-bw/2, y1, bw, h1);
  }}
  legend("<span><i style='color:"+UP+"'>■</i> 涨</span>"+
         "<span><i style='color:"+DOWN+"'>■</i> 跌</span>");
}}

// ---- 5日均线图 ----
function drawMA5() {{
  const arrs = [["收盘", D.closes, "#6b7280", 1.0], ["MA5", D.ma5, "#3b82f6", 2.0]];
  const [mn, mx] = seriesMinMax([...D.closes, ...D.ma5.filter(v=>v!=null)], 0.06);
  drawAxes(mn, mx, 5);
  for (const [nm, arr, color, lw] of arrs) {{
    ctx.strokeStyle = color; ctx.lineWidth = lw; ctx.beginPath();
    let started = false;
    for (let i=0; i<n; i++) {{
      if (arr[i]==null) {{ started = false; continue; }}
      const x = xOf(i), y = yOf(arr[i], mn, mx);
      started ? ctx.lineTo(x,y) : ctx.moveTo(x,y);
      started = true;
    }}
    ctx.stroke();
  }}
  legend("<span><i style='color:#6b7280'>—</i> 收盘价</span><span><i style='color:#3b82f6'>—</i> MA5</span>");
}}

// ---- BOLL 布林带 ----
function drawBOLL() {{
  const vals = [...D.boll_up.filter(v=>v!=null), ...D.boll_low.filter(v=>v!=null)];
  const [mn, mx] = seriesMinMax(vals, 0.05);
  drawAxes(mn, mx, 5);
  // 区间填充
  ctx.fillStyle = "rgba(59,130,246,0.10)"; ctx.beginPath();
  let started = false;
  for (let i=0; i<n; i++) {{
    if (D.boll_up[i]==null) {{ started=false; continue; }}
    const x=xOf(i), y=yOf(D.boll_up[i],mn,mx);
    started ? ctx.lineTo(x,y) : ctx.moveTo(x,y); started=true;
  }}
  for (let i=n-1; i>=0; i--) {{
    if (D.boll_low[i]==null) continue;
    ctx.lineTo(xOf(i), yOf(D.boll_low[i],mn,mx));
  }}
  ctx.closePath(); ctx.fill();
  const lines = [["BOLL上", D.boll_up, "#e03434"], ["BOLL中", D.boll_mid, "#9ca3af"],
                 ["BOLL下", D.boll_low, "#089981"]];
  for (const [nm, arr, color] of lines) {{
    ctx.strokeStyle = color; ctx.lineWidth = 1.2; ctx.beginPath();
    let started=false;
    for (let i=0; i<n; i++) {{
      if (arr[i]==null) {{ started=false; continue; }}
      const x=xOf(i), y=yOf(arr[i],mn,mx);
      started ? ctx.lineTo(x,y) : ctx.moveTo(x,y); started=true;
    }}
    ctx.stroke();
  }}
  legend("<span><i style='color:#e03434'>—</i> 上轨</span><span><i style='color:#9ca3af'>—</i> 中轨(MA20)</span><span><i style='color:#089981'>—</i> 下轨</span>");
}}

// ---- 成交量 ----
function drawVol() {{
  const [mn, mx] = seriesMinMax(D.vols, 0.05);
  drawAxes(mn, mx, 4);
  const bw = Math.max(1.2, (W-PAD.L-PAD.R)/n*0.6);
  for (let i=0; i<n; i++) {{
    const up = D.closes[i] >= D.opens[i];
    ctx.fillStyle = up ? UP : DOWN;
    const y = yOf(D.vols[i], mn, mx);
    ctx.fillRect(xOf(i)-bw/2, y, bw, Math.max(1, H-PAD.B-y));
  }}
  legend("<span><i style='color:"+UP+"'>■</i> 阳线量</span><span><i style='color:"+DOWN+"'>■</i> 阴线量</span><span>单位：手</span>");
}}

// ---- 涨跌幅 ----
function drawChange() {{
  const [mn, mx] = seriesMinMax(D.changes, 0.15);
  // 让 0 轴可见：若区间不含 0，扩展
  const lo = Math.min(mn, 0), hi = Math.max(mx, 0);
  drawAxes(lo, hi, 5);
  const y0 = yOf(0, lo, hi);
  ctx.strokeStyle = "#999"; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(PAD.L, y0); ctx.lineTo(W-PAD.R, y0); ctx.stroke();
  const bw = Math.max(1.2, (W-PAD.L-PAD.R)/n*0.6);
  for (let i=0; i<n; i++) {{
    ctx.fillStyle = D.changes[i] >= 0 ? UP : DOWN;
    const y = yOf(D.changes[i], lo, hi);
    const y1 = Math.min(y, y0), h1 = Math.max(1, Math.abs(y-y0));
    ctx.fillRect(xOf(i)-bw/2, y1, bw, h1);
  }}
  legend("<span><i style='color:"+UP+"'>■</i> 上涨</span><span><i style='color:"+DOWN+"'>■</i> 下跌</span><span>单位：%</span>");
}}

document.getElementById("chartType").addEventListener("change", e => {
  currentType = e.target.value;
  paint(currentType);
});
document.getElementById("rangeInfo").textContent =
  D.dates[0] + " ~ " + D.dates[n-1] + " | 共 " + n + " 个交易日";
drawTooltip();
paint(currentType);
</script>
</body>
</html>
"""


def build_chart_html(stock_name, rows):
    """生成自包含 HTML 图表页内容。"""
    data = build_chart_data(rows)
    html = (CHART_TEMPLATE
            .replace("__NAME__", stock_name)
            .replace("__DATA_JSON__", json.dumps(data, ensure_ascii=False)))
    return html


# ---------------------------------------------------------------------------
# 输出与打开
# ---------------------------------------------------------------------------
def save_csv(rows, out_dir):
    """写 CSV 留档（项目 data/ 目录，不再放桌面）。返回文件路径。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"kline_{datetime.now():%Y%m%d_%H%M%S}.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_COLUMNS)
        writer.writerows(rows)
    return path


def open_in_browser(html_path):
    """用 Windows 默认浏览器打开本地 HTML（经 WSL UNC 路径）。"""
    try:
        win = subprocess.run(["wslpath", "-w", str(html_path)],
                             capture_output=True, text=True, timeout=10)
        unc = win.stdout.strip()
        subprocess.Popen(["explorer.exe", unc])
        print(f"[i] 已在浏览器打开：{unc}")
    except Exception as exc:
        print(f"[!] 自动打开失败（可手动打开）：{html_path} ({exc})")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    print("=" * 56)
    print("股票历史数据图表生成器（近一年 · 东方财富数据源）")
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

    # 4. CSV 留档（data/ 目录）
    data_dir = Path(__file__).resolve().parent / "data"
    csv_path = save_csv(rows, data_dir)
    print(f"[i] CSV 留档：{csv_path}")

    # 5. 生成 HTML 图表并打开
    title = f"{stock_name or chosen['名称']} ({chosen['代码']})"
    html = build_chart_html(title, rows)
    html_path = data_dir / f"chart_{datetime.now():%Y%m%d_%H%M%S}.html"
    html_path.write_text(html, encoding="utf-8")
    print(f"[i] 图表页：{html_path}")
    open_in_browser(html_path)


if __name__ == "__main__":
    main()
