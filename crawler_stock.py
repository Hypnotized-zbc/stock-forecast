# -*- coding: utf-8 -*-
"""
crawler_stock.py — investing.com 股票数据爬虫（单文件自包含版）
================================================================
定向爬取 https://cn.investing.com/equities。

反爬说明：
    cn.investing.com 有 Cloudflare 防护，requests/curl 直连一律 403。
    本脚本通过 Windows Edge 无头浏览器 + CDP（Chrome DevTools Protocol）
    抓取页面 —— 真实浏览器指纹可以绕过防护。该方案在你的环境中已多次
    验证（spider_framework 项目同款通道）。

运行方式：
    python3 crawler_stock.py
    程序会先询问要爬取的股票名称，再搜索、抓取并输出数据。

注意：
    按约定本文件尚未实测。搜索/详情页的解析基于站点常规结构
    （Next.js #__NEXT_DATA__ + 通用链接模式）。首次运行若解析为空，
    先调用 fetch_html(url) 抓一页 HTML 检查真实结构，再微调解析函数。

依赖：
    requests, beautifulsoup4（requirements.txt 已包含）
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://cn.investing.com"
EQUITIES_URL = BASE_URL + "/equities"

# ---------------------------------------------------------------------------
# 内嵌 PowerShell 脚本（Edge CDP 通道）
# ---------------------------------------------------------------------------
PS_START_EDGE = r'''
$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$edgePath = $null
$pf86 = ${env:ProgramFiles(x86)}
$pf = $env:ProgramFiles
$la = $env:LOCALAPPDATA
$candidates = @(
  "$pf86\Microsoft\Edge\Application\msedge.exe",
  "$pf\Microsoft\Edge\Application\msedge.exe",
  "$la\Microsoft\Edge\Application\msedge.exe"
)
foreach ($c in $candidates) {
  if (Test-Path $c) { $edgePath = $c; break }
}
if (-not $edgePath) {
  try {
    $reg = Get-Item "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe" -ErrorAction SilentlyContinue
    if ($reg) {
      $v = $reg.GetValue("")
      if ($v -and (Test-Path $v)) { $edgePath = $v }
    }
  } catch {}
}
if (-not $edgePath) {
  Write-Output "EDGE_NOT_FOUND"
  exit 1
}

Get-Process msedge -ErrorAction SilentlyContinue | Where-Object {
  $_.Path -eq $edgePath -and $_.CommandLine -match "remote-debugging-port=9222"
} | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep 2

$profile = Join-Path $env:TEMP "stock_edge_profile"
if (Test-Path $profile) { Remove-Item $profile -Recurse -Force -ErrorAction SilentlyContinue }
Start-Process $edgePath -ArgumentList @(
  "--headless", "--disable-gpu", "--no-first-run",
  "--user-data-dir=$profile",
  "--remote-debugging-port=9222", "--remote-debugging-address=0.0.0.0",
  "about:blank"
)
Start-Sleep 6
try {
  $v = Invoke-RestMethod -Uri "http://127.0.0.1:9222/json/version" -TimeoutSec 5
  Write-Output ("CDP OK: " + $v.Browser)
} catch {
  Write-Output ("CDP FAIL: " + $_.Exception.Message)
}
'''

PS_FETCH = r'''
param([string]$Url)
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$targets = Invoke-RestMethod -Uri "http://127.0.0.1:9222/json/list" -TimeoutSec 5
$page = $targets | Where-Object { $_.type -eq "page" } | Select-Object -First 1
if (-not $page) { Write-Output "NO_PAGE_TARGET"; exit 1 }
$wsUrl = $page.webSocketDebuggerUrl

$ws = [System.Net.WebSockets.ClientWebSocket]::new()
$ct = [System.Threading.CancellationToken]::None
$ws.ConnectAsync([Uri]$wsUrl, $ct).Wait()
if ($ws.State -ne "Open") { Write-Output "WS_CONNECT_FAIL"; exit 1 }

$script:nextId = 100
function Send-Cdp($method, $params) {
    $id = $script:nextId; $script:nextId++
    $obj = @{ id = $id; method = $method }
    if ($params) { $obj.params = $params }
    $json = $obj | ConvertTo-Json -Compress -Depth 8
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
    $seg = [ArraySegment[byte]]::new($bytes)
    $ws.SendAsync($seg, [System.Net.WebSockets.WebSocketMessageType]::Text, $true, $ct).Wait()
    return $id
}
function Read-OneMessage {
    $ms = [System.IO.MemoryStream]::new()
    $buffer = New-Object byte[] 2097152
    do {
        $seg = [ArraySegment[byte]]::new($buffer)
        $res = $ws.ReceiveAsync($seg, $ct).Result
        if ($res.Count -gt 0) { $ms.Write($buffer, 0, $res.Count) }
    } while (-not $res.EndOfMessage)
    return [System.Text.Encoding]::UTF8.GetString($ms.ToArray())
}
function Wait-Response($wantId, $timeoutMs) {
    $deadline = [DateTime]::UtcNow.AddMilliseconds($timeoutMs)
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($ws.State -ne "Open") { return $null }
        $msg = Read-OneMessage
        $obj = $msg | ConvertFrom-Json
        if ($obj.id -eq $wantId) { return $obj }
    }
    return $null
}

$id = Send-Cdp "Page.navigate" @{ url = $Url }
$null = Wait-Response $id 15000

for ($i = 0; $i -lt 24; $i++) {
    Start-Sleep -Milliseconds 500
    $id = Send-Cdp "Runtime.evaluate" @{ expression = "({rs: document.readyState, hasTitle: !!document.querySelector('h1'), hasNextData: !!document.getElementById('__NEXT_DATA__')})"; returnByValue = $true }
    $resp = Wait-Response $id 8000
    $st = $resp.result.result.value
    if ($st.rs -eq "complete" -and $st.hasTitle) { break }
}
Start-Sleep -Milliseconds 1000

$id = Send-Cdp "Runtime.evaluate" @{ expression = "document.documentElement.outerHTML"; returnByValue = $true }
$resp = Wait-Response $id 15000

if ($resp -and $resp.result.result.value) {
    [Console]::Write($resp.result.result.value)
    try { $ws.CloseAsync([System.Net.WebSockets.WebSocketCloseStatus]::NormalClosure, "done", $ct).Wait() } catch {}
    exit 0
} else {
    Write-Output "CDP_EVAL_FAIL"
    try { $ws.CloseAsync([System.Net.WebSockets.WebSocketCloseStatus]::NormalClosure, "done", $ct).Wait() } catch {}
    exit 1
}
'''


# ---------------------------------------------------------------------------
# Edge CDP 通道
# ---------------------------------------------------------------------------
class EdgeCDP:
    """封装 Windows Edge 无头浏览器 + CDP 取页面 HTML。"""

    def __init__(self, workdir=None):
        # PowerShell 脚本写到 Windows 临时目录（-File 不接受 UNC 路径）
        win_tmp = os.environ.get("TEMP", r"C:\Users\Public\Temp")
        self.ps_dir = win_tmp + r"\crawler_stock"
        if workdir is None:
            workdir = Path(tempfile.gettempdir())
        self._ensure_ps_scripts()

    def _write_ps(self, name, content):
        os.makedirs(self.ps_dir, exist_ok=True)
        p = Path(self.ps_dir) / name
        p.write_text(content, encoding="utf-8")
        return str(p)

    def _ensure_ps_scripts(self):
        self._write_ps("start_edge_cdp.ps1", PS_START_EDGE)
        self._write_ps("fetch_via_edge.ps1", PS_FETCH)

    @staticmethod
    def _run_ps(script_path, args=None):
        cmd = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
               "-File", script_path]
        if args:
            cmd.extend(args)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            raise RuntimeError(f"PowerShell 执行失败: {proc.stderr.strip()[:300]}")
        return proc.stdout

    def ensure_started(self):
        """启动/复用 CDP 浏览器。幂等：已启动则直接返回。"""
        out = self._run_ps(os.path.join(self.ps_dir, "start_edge_cdp.ps1"))
        if "CDP OK" not in out and "EDGE_NOT_FOUND" not in out:
            raise RuntimeError(f"Edge CDP 启动失败: {out.strip()[:300]}")

    def fetch_html(self, url, retries=2):
        """通过 CDP 导航到 url 并返回渲染后的完整 HTML。"""
        last_err = None
        for attempt in range(1, retries + 1):
            try:
                out = self._run_ps(os.path.join(self.ps_dir, "fetch_via_edge.ps1"), [url])
                if out.strip() in ("NO_PAGE_TARGET", "CDP_EVAL_FAIL", "WS_CONNECT_FAIL"):
                    raise RuntimeError(out.strip())
                return out
            except (RuntimeError, subprocess.TimeoutExpired) as exc:
                last_err = exc
                print(f"[!] 抓取失败(第{attempt}次): {exc}")
                time.sleep(3)
        raise RuntimeError(f"页面抓取失败: {url} ({last_err})")


# ---------------------------------------------------------------------------
# investing.com 解析
# ---------------------------------------------------------------------------
def _find_in_next_data(soup, keys):
    """在 #__NEXT_DATA__ JSON 里递归找第一个命中的键。"""
    node = soup.find("script", id="__NEXT_DATA__")
    if not node:
        return None
    try:
        payload = json.loads(node.string)
    except (json.JSONDecodeError, TypeError):
        return None

    def walk(obj, depth=0):
        if depth > 12 or obj is None:
            return None
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in keys and v not in (None, ""):
                    return v
                hit = walk(v, depth + 1)
                if hit is not None:
                    return hit
        elif isinstance(obj, list):
            for item in obj:
                hit = walk(item, depth + 1)
                if hit is not None:
                    return hit
        return None
    return walk(payload)


def search_stock(html, name):
    """从搜索页 HTML 提取候选股票：[(显示名, 链接), ...]"""
    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.startswith("/equities/"):
            continue
        text = a.get_text(strip=True)
        if not text or len(text) < 2:
            continue
        key = (text, href)
        if key in seen:
            continue
        seen.add(key)
        candidates.append((text, BASE_URL + href))
    # 名称与搜索词相关度优先：把包含搜索词的排前面
    needle = name.lower()
    candidates.sort(key=lambda c: 0 if needle in c[0].lower() else 1)
    return candidates


def parse_quote(html):
    """从个股详情页 HTML 提取报价信息。"""
    soup = BeautifulSoup(html, "html.parser")

    # 股票名称：h1 或 __NEXT_DATA__
    title = soup.find("h1")
    name = title.get_text(strip=True) if title else None
    if not name:
        name = _find_in_next_data(soup, ["name", "stockName"])

    # 最新价：优先 __NEXT_DATA__，回退常见 DOM 结构
    price = _find_in_next_data(soup, ["last", "lastPrice", "price", "last_close"])
    if price is None:
        el = soup.find(id="last_last") or soup.select_one("[class*='instrument-price']")
        if el:
            m = re.search(r"[\d,]+\.\d+", el.get_text())
            price = m.group(0) if m else el.get_text(strip=True)

    change = _find_in_next_data(soup, ["change", "changeValue"])
    if change is None:
        el = soup.find(id="change_id") or soup.select_one("[id*='change']")
        if el:
            change = el.get_text(strip=True)

    change_pct = _find_in_next_data(soup, ["changePercent", "change_pct"])
    if change_pct is None:
        el = soup.select_one("[id*='changePercent']") or soup.select_one("[class*='percent']")
        if el:
            m = re.search(r"[+-]?[\d.,]+%", el.get_text())
            change_pct = m.group(0) if m else el.get_text(strip=True)

    return {
        "名称": name,
        "最新价": price,
        "涨跌额": change,
        "涨跌幅": change_pct,
        "抓取时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def ask_stock_name():
    """爬取前询问股票名称。"""
    name = input("要爬取哪个股票？请输入名称（如：贵州茅台 / 茅台 / AAPL）：").strip()
    while not name:
        name = input("输入不能为空，请输入股票名称：").strip()
    return name


def choose_candidate(candidates):
    """多个候选时让用户确认选择。"""
    if not candidates:
        return None
    if len(candidates) == 1:
        print(f"[i] 找到唯一匹配：{candidates[0][0]}")
        return candidates[0][1]
    print("[i] 找到多个匹配，请选择：")
    for i, (text, url) in enumerate(candidates[:10], 1):
        print(f"    {i}. {text}")
    while True:
        try:
            idx = int(input("输入序号（1-10，回车默认第 1 个）：").strip() or "1")
            if 1 <= idx <= min(10, len(candidates)):
                return candidates[idx - 1][1]
        except ValueError:
            pass
        print("序号无效，重新输入。")


def main():
    print("=" * 50)
    print("investing.com 股票数据爬虫")
    print("目标站点:", EQUITIES_URL)
    print("=" * 50)

    name = ask_stock_name()
    print(f"[i] 正在搜索: {name} ...")

    client = EdgeCDP()
    client.ensure_started()

    # 1. 搜索页
    search_url = BASE_URL + "/search/?q=" + urllib.parse.quote(name)
    html = client.fetch_html(search_url)
    candidates = search_stock(html, name)
    if not candidates:
        print(f"[x] 未找到股票「{name}」，请检查名称后重试。")
        sys.exit(1)

    # 2. 选择目标
    detail_url = choose_candidate(candidates)
    if not detail_url:
        sys.exit(1)

    # 3. 详情页
    print(f"[i] 抓取详情: {detail_url}")
    detail_html = client.fetch_html(detail_url)
    quote = parse_quote(detail_html)

    # 4. 输出
    print("\n" + "=" * 50)
    print("爬取结果")
    print("=" * 50)
    for k, v in quote.items():
        print(f"  {k}: {v}")

    # 5. 存盘
    out_dir = Path("data")
    out_dir.mkdir(exist_ok=True)
    safe = re.sub(r"[^\w\u4e00-\u9fff-]", "_", str(quote.get("名称") or name))
    out_path = out_dir / f"investing_{safe}_{datetime.now():%Y%m%d_%H%M%S}.json"
    out_path.write_text(
        json.dumps(quote, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[i] 结果已保存: {out_path}")


if __name__ == "__main__":
    main()
