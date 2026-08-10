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
