# stock-forecast

股票预测项目。第一步：通用爬虫框架（不含股票数据源，后续按需接入）。

## 项目结构

```
stock-forecast/
├── crawler/
│   ├── __init__.py
│   ├── core.py        # 通用爬虫核心：请求、重试、限速、UA、缓存
│   └── parsers.py     # 通用解析工具：宽松 JSON、HTML 表格
├── examples/
│   └── demo.py        # 演示：抓 JSON 接口 + 网页 + 缓存命中
├── requirements.txt
├── backup.sh          # 备份脚本：拷贝关键文件到 backups/<时间戳>/
└── UPDATES.md         # 更新报告
```

## 快速开始

```bash
source ~/venv/bin/activate
pip install -r requirements.txt
python -m examples.demo          # 通用爬虫框架演示
python crawler_stock.py          # 输入名称 → 近一年数据 → 自包含 HTML 图表页并打开浏览器
```

## 框架用法

```python
from crawler.core import HttpClient

# interval: 请求最小间隔(秒)  retries: 重试次数
# cache_dir: 开启本地缓存，同 URL 不再重复请求
client = HttpClient(interval=1.0, cache_dir="data/cache")

client.get_json(url)      # 返回 dict/list
client.get_text(url)      # 返回 str
client.get_soup(url)      # 返回 BeautifulSoup
```

## 约定

- 每次改动前运行 `./backup.sh` 备份
- 改完同步更新 `UPDATES.md` 并推送 GitHub
- 股票数据源后续以 `crawler/sources/` 模块形式接入，不改动 core
