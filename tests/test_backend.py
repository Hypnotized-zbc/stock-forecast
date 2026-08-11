# -*- coding: utf-8 -*-
"""backend.py 纯函数测试（pytest）。

运行：cd stock-forecast && python3 -m pytest tests/ -q
覆盖：MA/BOLL/MACD/KDJ/RSI 数值正确性、周期聚合、图表数据装配。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backend


def test_ma_basic():
    assert backend.ma([1, 2, 3, 4], 2) == [None, 1.5, 2.5, 3.5]
    assert backend.ma([1, 2, 3], 5) == [None, None, None]


def test_boll_mid_equals_ma():
    closes = [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]
    up, mid, low = backend.boll(closes, n=5)
    ma5 = backend.ma(closes, 5)
    for a, b in zip(mid, ma5):
        if b is not None:
            assert a == pytest.approx(b, abs=1e-6)
    # 上轨 >= 中轨 >= 下轨
    for u, m, l in zip(up, mid, low):
        if m is not None:
            assert u >= m >= l


def test_ema_monotonic_bounds():
    vals = list(range(1, 21))
    out = backend.ema(vals, 5)
    assert out[0] == 1
    assert all(a <= b for a, b in zip(out, out[1:]))  # 递增
    assert out[-1] < 20  # EMA 落后于末值


def test_macd_dif_is_ema_diff():
    vals = [10 + (i % 7) for i in range(40)]
    dif, dea, hist = backend.macd(vals)
    e12 = backend.ema(vals, 12)
    e26 = backend.ema(vals, 26)
    for i in range(40):
        if e12[i] is not None and e26[i] is not None:
            assert dif[i] == pytest.approx(e12[i] - e26[i], abs=1e-9)
    assert hist[0] is None or hist[0] == pytest.approx(0, abs=1e-9)


def test_kdj_bounds():
    import random
    random.seed(7)
    closes = [10 + random.random() for _ in range(50)]
    highs = [c + 0.5 for c in closes]
    lows = [c - 0.5 for c in closes]
    k, d, j = backend.kdj(highs, lows, closes)
    assert all(0 <= x <= 100 for x in k)
    assert all(0 <= x <= 100 for x in d)
    # J 可能越界，但形状合法
    assert len(k) == len(d) == len(j) == 50


def test_rsi_range_and_extremes():
    # 全涨 → RSI 100
    up = backend.rsi(list(range(1, 30)), 14)
    assert up[14] == pytest.approx(100, abs=1e-6)
    assert up[-1] == pytest.approx(100, abs=1e-6)
    # 全跌 → RSI 0
    down = backend.rsi(list(range(30, 1, -1)), 14)
    assert down[14] == pytest.approx(0, abs=1e-6)
    # 波动序列 → 0~100
    vals = [10 + ((i % 5) - 2) for i in range(60)]
    out = backend.rsi(vals, 14)
    assert all(v is None or 0 <= v <= 100 for v in out)


def test_agg_period_week_and_month():
    rows = [
        ["2026-06-01", "10", "11", "12", "9", "100", "1000", "1", "1.0", "0.5", "2.0"],
        ["2026-06-02", "11", "13", "14", "10", "200", "2000", "1", "1.0", "0.5", "2.0"],
        ["2026-07-01", "13", "14", "15", "12", "300", "3000", "1", "1.0", "0.5", "2.0"],
    ]
    weeks = backend.agg_period(rows, "week")
    assert len(weeks) == 2
    months = backend.agg_period(rows, "month")
    assert len(months) == 2
    # 月聚合：第一组 open=10 close=13 high=max(12,14)=14 low=min(9,10)=9 vol=300
    assert months[0][1] == "10"
    assert months[0][2] == "13"
    assert months[0][3] == "14.0"
    assert months[0][4] == "9.0"
    assert months[0][5] == "300.0"
    assert months[0][6] == "3000.0"


def test_build_chart_data_shape():
    rows = []
    for i in range(30):
        c = 10 + i * 0.1
        rows.append([
            f"2026-06-{i+1:02d}", f"{c-0.2:.2f}", f"{c:.2f}", f"{c+0.3:.2f}", f"{c-0.3:.2f}",
            "1000", "10000", "1", "0.5", "0.5", "1.0",
        ])
    data = backend.build_chart_data(rows)
    assert len(data["dates"]) == 30
    assert len(data["ma5"]) == 30
    assert len(data["macd_dif"]) == 30
    assert len(data["macd_dea"]) == 30
    assert len(data["macd_hist"]) == 30
    assert len(data["kdj_k"]) == 30
    assert len(data["rsi"]) == 30
    assert data["macd_hist"][-1] is not None
