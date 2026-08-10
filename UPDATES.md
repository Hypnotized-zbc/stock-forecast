# 更新报告

## v0.1.0 (2026-08-10)
- 建立通用爬虫框架 crawler/core.py：
  - HttpClient：UA 随机伪装、失败重试（指数退避）、请求限速、本地缓存
  - 支持 get_json / get_text / get_soup 三种取数方式
- crawler/parsers.py：宽松 JSON 解析（容忍注释/尾逗号）、HTML 表格转二维列表
- examples/demo.py：演示请求 JSON 接口、网页抓取、缓存命中
- 修复：缓存回读时正文分段错误（partition 替代 split）
- 本次未接入任何股票数据源，纯通用框架
