"""picker — 量化选股模型（开源、可复现、可单测版本）。

目录：
  board_filter.py     板块权限过滤（纯函数，100% 可测）
  coarse_filter.py    粗筛四道闸（纯函数 + CLI）
  factors.py          评分 / 择时 / 动量 / 仓位 核心数学（纯函数，100% 可测）
  data_provider.py    数据层抽象（MCP 实现 / Sample 实现离线演示）
  model.py            生产级端到端编排（依赖数据后端，非离线可跑）
  five_review.py      五项重点复核（结构化）
  fetch_sw.py         申万行业映射补齐
  track_picks.py / eval_picks.py / annotate_repeat.py / monitor_exits.py  跟踪与纪律
"""
