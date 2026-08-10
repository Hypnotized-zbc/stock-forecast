# 更新报告

## v0.1.0 (2026-08-10)
- 建立通用爬虫框架 crawler/core.py：
  - HttpClient：UA 随机伪装、失败重试（指数退避）、请求限速、本地缓存
  - 支持 get_json / get_text / get_soup 三种取数方式
- crawler/parsers.py：宽松 JSON 解析（容忍注释/尾逗号）、HTML 表格转二维列表
- examples/demo.py：演示请求 JSON 接口、网页抓取、缓存命中
- 修复：缓存回读时正文分段错误（partition 替代 split）
- 本次未接入任何股票数据源，纯通用框架

## v0.2.0 (2026-08-10)
- 新增 crawler_stock.py（单文件自包含）：定向爬取 cn.investing.com/equities
  - 爬取前交互询问股票名称；搜索多候选时让用户确认选择
  - 反爬方案：Windows Edge 无头 + CDP（复用 spider_framework 已验证通道），
    内嵌 PowerShell 脚本写入 %TEMP%，单文件即可运行，不依赖外部脚本
  - 解析：优先 #__NEXT_DATA__ JSON 递归提取，回退常见 DOM 选择器
  - 结果打印 + 存 data/investing_<名称>_<时间>.json
- 按约定未实测：选择器基于站点常规结构，首次运行解析为空时需抓
  实际 HTML 微调（代码内有说明）

## v0.3.0 (2026-08-10)
- 数据源替换：investing.com（Cloudflare 防护，requests 与 Edge 无头均 403）
  → 东方财富公开接口（无 token、无防护，A股/美股/港股全覆盖）
- crawler_stock.py 重写为单文件下载器：
  输入名称/代码 → 搜索候选（suggest API）→ 多候选交互选择 →
  下载近一年日K（kline API，前复权）→ CSV 保存到 Windows 桌面
- CSV 列：日期/开盘/收盘/最高/最低/成交量/成交额/振幅/涨跌幅/涨跌额/换手率，
  utf-8-sig 编码，Excel 直接打开不乱码
- 删除 Edge CDP 内嵌脚本（不再依赖 PowerShell / Edge / spider_framework）
- 实测：输入"茅台" → 贵州茅台 242 条日K → 桌面 CSV 生成成功

## v0.4.0 (2026-08-10)
- 不再把 CSV 保存到桌面；CSV 改为 data/ 目录留档
- 新增 HTML 图表页（自包含单文件，原生 Canvas 绘图，无外部依赖，离线可开）：
  下拉列表切换，每次显示一张图：
  1. K线图（叠加 MA5/MA20/BOLL 上下中轨）
  2. 5日均线图（收盘价 + MA5）
  3. 布林带 BOLL 图（上/中/下轨 + 区间填充）
  4. 成交量 VOL 图（红涨绿跌）
  5. 涨跌幅柱状图（含 0 轴）
- 指标计算在 Python 侧完成（MA5/MA20/BOLL），JS 只负责绘图
- 鼠标悬浮显示当日 OHLCV/MA/BOLL 明细
- 自动用 Windows 默认浏览器打开（explorer.exe + UNC 路径）
- 实测：茅台 242 条日K → data/chart_*.html 生成并在 Edge 中打开
