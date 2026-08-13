# 股票历史数据查询 · stock-forecast

一个**零第三方依赖**的 A 股行情分析与预测工具：东财/新浪双数据源、Canvas 手绘 K 线、
纯 Python 实现的 5 模型拟合与未来预测、全屏放大交互、AI 技术解读、多用户账号体系。
**顶部导航栏多板块**：行情分析 / 排行榜 / 资金流向 / 历史统计 / 预测战绩。

> 仅供学习研究，不构成任何投资建议。

---

## ✨ 功能一览

### 板块导航（顶部栏目，点击切换）
- **行情分析**：搜索/自选股/K线/拟合/预测/AI/对比/分享（原有全部功能）
- **排行榜**：沪深A股 涨幅/跌幅/成交额/换手率/量比 五大榜单，点击行直达行情分析
- **资金流向**：个股主力/超大/大/中/小单净流入柱状图 + 全列数据表（10~120 日）
- **历史统计**：区间/年化收益、波动率、夏普、最大回撤 + 日收益率分布直方图
- **预测战绩**：预测回测闭环——每次查询自动记录预测，到期自动结算实际收盘，
  统计 MAE / 方向命中率 / 平均误差，展开逐日预测 vs 实际对比

### 图表
- **K线 / MA5 / BOLL / 成交量 / 涨跌幅 / MACD / KDJ / RSI** 多种视图，Canvas 手绘（红涨绿跌）
- **日K / 周K / 月K** 周期切换（东财原生周期接口，新浪回退时自动聚合）
- **全屏放大**：双击进入 → 滚轮缩放横轴、左键拖拽平移、双击锁定参考线 + 固定数据窗口
- 十字线悬停浮窗、单击/双击自动区分（250ms 延时判定，互不冲突）

### 预测（纯 Python，零依赖）
- **ARIMA / ETS / Prophet(轻量) / SVR / 随机森林** 五模型拟合并给出未来 10 日预测
- 按 RMSE 逆加权的最终预测，右侧面板展示指标
- **回测闭环**：预测落库 predict_log，到期自动拉实际收盘对账（MAE/方向命中率）

### 智能与增强
- **✨ AI 技术解读**：调用 DeepSeek 对最近 20 日行情生成中文分析（趋势/支撑压力/风险）
- **⇄ 多股对比**：多支股票归一化（首日=100）曲线对比，一眼看出相对强弱
- **📋 分享卡片**：生成含 K 线截图 + 关键指标的行情卡片（PNG 图片，可分享）
- 自选股实时行情 30 秒统一刷新；登录后自动按序预取自选股图表数据（行右侧显示获取状态）
- K线 24h 本地缓存、页面关闭 30 秒自动停服（本地模式）

### 用户体系与安全
- 注册/登录（用户名 + 密码 + **图形验证码 + 滑块人机验证**双重校验，防机器人批量入侵）
- 自选股 / AI 解读缓存 / 查询历史按账号云端同步，换设备登录即可读取
- 密码 PBKDF2-SHA256 加盐哈希存储；会话 7 天有效；支持修改密码
- **IP 限速**：登录/注册/验证码/滑块接口限频，连续登录失败自动锁定 IP
- **XSS 防护**：CSP 安全响应头 + 前端输出转义
- 中英双语切换（登录页 + 功能页右上角 Eng/中文）

---

## 🚀 运行

```bash
# 需要 Python 3.9+，仅依赖 requests
pip install -r requirements.txt   # 或 pip install requests

# 启动（自动打开浏览器）
python3 app.py
```

- 前端页面在 `static/index.html`，登录页在 `static/login.html`，后端逻辑在 `backend.py`，入口 `app.py`
- 数据源：东方财富（主）→ 新浪（备）→ 腾讯（备），失败自动切换
- 本地模式退出：关闭浏览器页面，30 秒后服务自动停止；或 Ctrl+C

### AI 解读配置（可选）
设置环境变量 `DEEPSEEK_API_KEY`，或在项目目录创建 `llm_key.txt` 填入 Key。
不配置也能用其余全部功能，AI 按钮会提示缺少 Key。

### 运行测试
```bash
python3 -m pytest tests/ -q
```

---

## ☁️ 云部署

### 方式一：直接运行（简单）
```bash
# Linux / WSL 服务器
STOCK_HOST=0.0.0.0 STOCK_PORT=8000 python3 app.py
# Windows 服务器（PowerShell）
$env:STOCK_HOST="0.0.0.0"; $env:STOCK_PORT="8000"; python app.py
```

- 公网模式不会因"页面关闭"自动停止，需 Ctrl+C
- 记得在云安全组放行对应端口（如 8000）

### 方式二：systemd 常驻（Linux 推荐）
项目内已附单元文件 `deploy/stock-forecast.service`：
```bash
sudo cp deploy/stock-forecast.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now stock-forecast
```
（部署前按注释修改路径/用户；日志：`journalctl -u stock-forecast -f`）

### 方式三：Windows 云服务器（RDP 部署）
项目内已附两个脚本（`deploy/` 目录）：
- **`start_server.bat`** — 启动脚本。把数据库放到项目目录**外**的
  `C:\stock-app\data\stock_forecast.db`（STOCK_DB 环境变量），公网监听 0.0.0.0:8000。
  代码更新时永远不会覆盖数据目录。
- **`update_server.bat`** — 一键更新脚本。流程：备份数据库 → 下载 GitHub 最新 ZIP →
  解压替换代码 → 提示重启。全程不碰 `data` 目录。

> **核心原则：数据库文件（stock_forecast.db）与代码目录分离。**
> 更新代码时只替换 app.py/backend.py/db.py/static 等，**永远不要覆盖数据库文件**。
> 后端每次启动会自动把数据库备份到 `backups/db/<时间戳>/`（保留最近 30 份），
> 即使误操作也能找回。

### HTTPS（公网强烈建议）
密码与 token 均建议加密传输。两种方式任选：
1. **Caddy 反向代理（最简单，自动申请/续期证书）**：
   ```
   example.com {
       reverse_proxy 127.0.0.1:8000
   }
   ```
2. **内置 TLS**：配置证书后直接 HTTPS：
   ```bash
   STOCK_HOST=0.0.0.0 STOCK_PORT=8443 \
   STOCK_SSL_CERT=/etc/ssl/cert.pem STOCK_SSL_KEY=/etc/ssl/key.pem python3 app.py
   ```

### 数据库备份
```bash
./tools/backup_db.sh            # 备份到 backups/db/<时间戳>/
./tools/backup_db.sh nightly    # 可加备注
# 建议 crontab：0 3 * * * /path/to/stock-forecast/tools/backup_db.sh nightly
```
数据库文件路径可用 `STOCK_DB` 覆盖；五张表：`users` 用户、`watchlist` 自选股、
`ai_cache` AI解读缓存、`history` 查询历史、`predict_log` 预测回测记录。

---

## 🧩 技术架构

```
浏览器 (static/index.html, 原生 JS + Canvas)
   │  fetch /api/...
   ▼
backend.py (Python 标准库 HTTP 服务)
   │
   ├── 数据层   东方财富 API ──失败──▶ 新浪 API（备）──▶ 腾讯 API（备）
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
├── backend.py          # 后端：数据源/指标/模型/HTTP API/AI 解读/认证/限速
├── db.py               # 数据库：SQLite 五表（users/sessions/watchlist/ai_cache/history）
├── static/login.html   # 登录/注册页（/）：背景图 + 中英切换 + 滑块验证
├── static/index.html   # 功能页（/app）：页面 + Canvas 图表 + 交互
├── deploy/             # 部署文件（systemd 单元、Windows 启动/更新脚本）
├── tools/              # 工具（GitHub 上传 / 数据库备份）
├── tests/test_backend.py  # pytest 单元测试
├── backups/            # 每次改动前的版本备份 + 数据库备份
├── data/               # K 线 CSV 留档（自动生成）
├── crawler/ examples/  # 早期爬虫/示例代码（保留供参考，主程序不依赖）
└── stock_forecast.db   # 用户数据（自动生成，gitignore）
```

> 说明：`crawler/`（早期公告爬虫）与 `examples/`（演示脚本）是开发历史遗留，
> 与本 Web 应用运行无关，保留仅供参考，可自行删除。

---

## 📚 API

| 接口 | 说明 |
|---|---|
| `/api/search?q=` | 股票搜索建议 |
| `/api/kline?secid=&start=&end=&period=` | K线（period: day/week/month） |
| `/api/quotes?secids=` | 批量实时行情 |
| `/api/quote?secid=` | 单只实时行情 |
| `/api/insight?secid=&name=&recent=` | AI 技术解读 |
| `/api/captcha` | 图形验证码（注册/改密/重置密码用） |
| `/api/slider` | 滑块人机验证拼图（登录/注册/改密/重置密码用） |
| `/api/register` | 注册（POST: username, password, email, captcha_id, captcha, slider_*） |
| `/api/login` | 登录（POST: username, password, slider_* → token）。密保邮箱仅用于改密/重置，登录不需要 |
| `/api/logout` | 登出（POST，带 Bearer token） |
| `/api/me` | 当前登录用户（GET，带 Bearer token） |
| `/api/change-password` | 修改密码（POST: email, new_password, captcha_id, captcha, slider_*，带 Bearer token，需登录） |
| `/api/delete-account` | 注销账号（POST，带 Bearer token，删除该用户全部数据） |
| `/api/reset-password` | 忘记密码（POST: username, email, new_password, captcha_id, captcha, slider_*，无需登录） |
| `/api/watchlist` | 自选股列表（GET）／增删（POST: action=add/remove, secid, name），需登录 |
| `/api/ai-cache` | AI 解读缓存（GET: ?secid=&period= ／POST 保存） |
| `/api/history` | 查询历史（GET ／POST 记录） |
| `/api/predict-log` | 预测回测记录（GET：自动结算已到期预测日，返回记录+汇总；需登录） |
| `/api/leaderboard?kind=` | 沪深A股排行榜（kind: up/down/amount/turnover/volratio，60 秒缓存） |
| `/api/fflow?secid=&days=` | 个股历史资金流（主力/超大/大/中/小单净流入，10~120 日，60 秒缓存） |
| `/api/shutdown` | 页面关闭信号（仅本地模式生效） |

> 登录/注册/改密/重置密码均需先通过滑块验证：前端拖动拼图后提交
> `slider_id / slider_x / slider_duration_ms / slider_samples` 四个参数。
> 滑块接口和验证码接口均限速。

---

## 🔐 安全说明

- 密码：PBKDF2-SHA256 加盐哈希（10 万次迭代），不存明文
- 会话：Bearer token 存内存，7 天有效，改密后其余会话失效
- 限速：登录 10 次/分、注册 5 次/分、验证码/滑块 20 次/分（按 IP）；
  连续 5 次登录失败锁定该 IP 10 分钟
- 请求体上限 1MB；并发请求上限 50（超出返回 503）
- 响应头：CSP / X-Frame-Options / X-Content-Type-Options / Referrer-Policy
- 前端所有外部文本（API 返回、AI 解读、用户输入）经 esc() 转义后拼入 DOM

---

## 📄 更新记录

见 [UPDATES.md](UPDATES.md)（版本降序，最新在上）。

---

*本项目由 AI 辅助开发完成。数据来自公开接口，预测结果仅为算法演示，不构成投资建议。*
