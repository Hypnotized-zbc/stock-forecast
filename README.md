# 股票历史数据查询 · stock-forecast

一个**零第三方依赖**的 A 股行情分析与预测工具：东财/新浪双数据源、Canvas 手绘 K 线、
纯 Python 实现的 5 模型拟合与未来预测、全屏放大交互、AI 技术解读。

> 仅供学习研究，不构成任何投资建议。

---

## ✨ 功能一览

### 图表
- **K线 / MA5 / BOLL / 成交量 / 涨跌幅 / MACD / KDJ / RSI** 八种视图，Canvas 手绘（红涨绿跌）
- **日K / 周K / 月K** 周期切换（东财原生周期接口，新浪回退时自动聚合）
- **全屏放大**：双击进入 → 滚轮缩放横轴、左键拖拽平移、双击锁定参考线 + 固定数据窗口
  - 坐标轴分层绘制（clip 裁剪不吞刻度）、浮动窗口碰撞避让、窗口可拖动、出界停边界
- 十字线悬停浮窗、单击/双击自动区分（250ms 延时判定，互不冲突）

### 预测（纯 Python，零依赖）
- **ARIMA / ETS / Prophet(轻量) / SVR / 随机森林** 五模型拟合并给出未来 10 日预测
- 按 RMSE 逆加权的最终预测，右侧面板展示指标

### 智能与增强
- **✨ AI 技术解读**：调用 DeepSeek 对最近 20 日行情生成中文分析（趋势/支撑压力/风险）
- **⇄ 多股对比**：多支股票归一化（首日=100）曲线对比，一眼看出相对强弱
- **📋 分享卡片**：生成含 K 线截图 + 关键指标的行情卡片（可打印/另存）
- **▦ 热力图**：自选股按涨跌幅着色的网格视图，切换列表/热力图
- 自选股实时行情 30 秒统一刷新，标题涨跌幅与自选股**同源零延迟**显示
- K线 24h 本地缓存、CSV 留档、页面关闭 30 秒自动停服

---

## 🚀 运行

```bash
# 需要 Python 3.9+，仅依赖 requests
pip install requests

# 启动（自动打开浏览器）
python3 app.py
```

- 前端页面在 `static/index.html`，后端逻辑在 `backend.py`，入口 `app.py`
- 数据源：东方财富（主）→ 新浪（备），失败自动切换
- 退出：关闭浏览器页面，30 秒后服务自动停止；或 Ctrl+C

### AI 解读配置（可选）
设置环境变量 `DEEPSEEK_API_KEY`，或在项目目录创建 `llm_key.txt` 填入 Key。
不配置也能用其余全部功能，AI 按钮会提示缺少 Key。

### 运行测试
```bash
python3 -m pytest tests/ -q
```

### 云部署（可选）
自选股 / AI 解读缓存 / 查询历史会存入本地 SQLite 文件 `stock_forecast.db`（自动创建），
并且**按登录用户隔离**：注册账号（用户名+密码+图形验证码）后，自选股等数据
只属于该用户，换设备登录即可读取自己的数据。
部署到云服务器时，用环境变量控制监听地址和端口：

```bash
# Linux / WSL 服务器
STOCK_HOST=0.0.0.0 STOCK_PORT=8000 python3 app.py
# Windows 服务器（PowerShell）
$env:STOCK_HOST="0.0.0.0"; $env:STOCK_PORT="8000"; python app.py
```

- 公网模式不会因"页面关闭"自动停止，需 Ctrl+C
- 记得在云安全组放行对应端口（如 8000）
- 数据库文件路径可用 `STOCK_DB` 覆盖；四张表：`users` 用户、`watchlist` 自选股、
  `ai_cache` AI解读缓存、`history` 查询历史
- 密码以 PBKDF2-SHA256 加盐哈希存储，不存明文；登录会话 token 存内存，
  服务重启后需重新登录

---

## 🧩 技术架构

```
浏览器 (static/index.html, 原生 JS + Canvas)
   │  fetch /api/...
   ▼
backend.py (Python 标准库 HTTP 服务)
   │
   ├── 数据层   东方财富 API ──失败──▶ 新浪 API（备用）
   ├── 指标层   MA / BOLL / MACD / KDJ / RSI（手写）
   ├── 模型层   ARIMA / ETS / Prophet / SVR / RF（纯 Python 数值实现）
   ├── AI 层    DeepSeek Chat API（可选，需 Key）
   └── 存储层   db.py → SQLite（用户/自选股 / AI缓存 / 历史，云部署时存服务器）
```

亮点实现（均无第三方科学计算库）：
- **Cholesky 分解**解最小二乘（Prophet 轻量版：线性趋势 + 变点 + 傅里叶季节）
- **ARIMA**：指数平滑 + 差分自回归；**ETS**：Holt 双指数平滑
- **SVR/KRR**：高斯核 + 岭回归闭式解；**随机森林**：自建决策树 CART

---

## 📁 目录结构

```
stock-forecast/
├── app.py              # 启动入口（python3 app.py）
├── backend.py          # 后端：数据源/指标/模型/HTTP API/AI 解读
├── db.py               # 数据库：SQLite 三表（自选股/AI缓存/历史）
├── static/login.html    # 登录/注册页（/）
├── static/index.html    # 功能页（/app）：页面 + Canvas 图表 + 交互
├── tests/test_backend.py  # pytest 单元测试
├── backups/            # 每次改动前的版本备份
├── data/               # K 线 CSV 留档（自动生成）
└── stock_forecast.db   # 用户数据（自动生成，gitignore）
```

---

## 📚 API

| 接口 | 说明 |
|---|---|
| `/api/search?q=` | 股票搜索建议 |
| `/api/kline?secid=&start=&end=&period=` | K线（period: day/week/month） |
| `/api/quotes?secids=` | 批量实时行情 |
| `/api/quote?secid=` | 单只实时行情 |
| `/api/insight?secid=&name=&recent=` | AI 技术解读 |
| `/api/captcha` | 图形验证码（注册用） |
| `/api/register` | 注册（POST: username, password, captcha_id, captcha） |
| `/api/login` | 登录（POST: username, password → token） |
| `/api/logout` | 登出（POST，带 Bearer token） |
| `/api/me` | 当前登录用户（GET，带 Bearer token） |
| `/api/watchlist` | 自选股列表（GET）／增删（POST: action=add/remove, secid, name），需登录 |
| `/api/ai-cache` | AI 解读缓存（GET: ?secid=&period= ／POST 保存） |
| `/api/history` | 查询历史（GET ／POST 记录） |
| `/api/shutdown` | 页面关闭信号 |

---

## 📄 更新记录

见 [UPDATES.md](UPDATES.md)（版本降序，最新在上）。

---

*本项目由 AI 辅助开发完成。数据来自公开接口，预测结果仅为算法演示，不构成投资建议。*
