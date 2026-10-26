# 量化选股模型（开源可复现版）

一个**三层过滤 + 量化择时 + 纪律化仓位**的 A 股选股系统。本仓库是从个人每日自动化选股流程中
抽取出的**可公开、可复现、可单测**版本：**不含任何真实持仓/盈亏隐私，不依赖任何外部密钥，clone 即可离线跑通**。

> ⚠️ 本项目为量化研究 / 工程能力展示样本，**所有结论不构成投资建议**。

---

## 一、它解决什么问题

把「拍脑袋选股」变成一条可回测、可纠错的数据管线：

1. **初选（数据层）**：用估值/盈利/质量硬指标从全市场筛出候选（成长线 PEG 体系 + 周期线 PB 体系）。
2. **粗筛四道闸**：板块权限 → 行业集中度去重 → PEG 合理性 → 增速质量，逐层收窄。
3. **深度评估**：财务复核 + 五项重点复核（商誉/政策/减持/毛利拐点/汇率）+ 0-100 综合评分。
4. **量化择时**：用市场估值/情绪/量能/宽度硬指标算「市场热度 heat」，反指定总仓区间（热→减、冷→加）。
5. **纪律化仓位**：评分加权 + 单票上限 + 硬止损/破位纪律，并跟踪历史胜率持续纠偏。

核心工程价值不在「选股准不准」，而在：
- **数据与代码分离**（初选是纯 JSON，脚本稳定不变）；
- **无前视偏见的回测纪律**（point-in-time、去重复、t 检验、诚实标注样本局限）；
- **配置驱动**（板块/因子/择时/仓位全部外置 JSON，改配置即生效）。

---

## 二、目录结构

```
pick_stocks/
├── config/                 # 所有策略参数（外置，改这里即可调模型）
│   ├── screener_config.json   初选表达式 + 行业配额
│   ├── board_config.json      禁用板块（无权限自动剔除）
│   ├── factor_config.json     评分因子（含价格成熟度惩罚）
│   ├── timing_config.json     择时热度权重与仓位区间
│   ├── sector_config.json     同板块去重上限
│   └── exit_rules.json        硬止损/破位纪律
├── data/                   # 输入/输出（*.json）
│   ├── screener_raw.json       示例初选（合成，离线演示用）
│   └── sw_map.json             示例申万行业映射
├── picker/                 # 全部 Python 逻辑
│   ├── board_filter.py     板块权限过滤（纯函数）
│   ├── coarse_filter.py    粗筛四道闸（纯函数 + CLI）
│   ├── factors.py          ★ 可测核心：评分/择时/动量/仓位数学
│   ├── data_provider.py    数据层抽象（Sample 离线 / MCP 生产）
│   ├── model.py            生产级端到端编排（依赖数据后端）
│   ├── five_review.py      五项重点复核
│   ├── fetch_sw.py         申万行业映射补齐
│   ├── track_picks.py / eval_picks.py / annotate_repeat.py / monitor_exits.py
│   └── run_demo.py         离线演示入口
└── tests/                  # pytest 单元测试（纯函数 100% 覆盖）
    ├── test_board_filter.py
    ├── test_coarse_filter.py
    └── test_factors.py
```

---

## 三、快速开始（离线，无需任何密钥）

```bash
cd pick_stocks
pip install -r requirements.txt

# 1) 跑粗筛 + 择时/动量演示（读 data/screener_raw.json，写 data/pool.json）
python -m picker.run_demo

# 2) 仅跑粗筛 CLI
python -m picker.coarse_filter

# 3) 跑单元测试
pytest -q
```

---

## 四、测试（这是本项目的重点）

作为工程样本，核心数学被抽取为**无副作用纯函数**并 100% 单测覆盖：

| 模块 | 覆盖内容 |
|------|----------|
| `board_filter` | 代码归一化、板块判定、权限过滤 |
| `coarse_filter` | PEG 计算、行业粒度、行业去重、四道闸串联 |
| `factors` | 综合评分（含价格成熟度惩罚）、动量映射、市场热度分桶、仓位计划 |

```bash
pytest -q
# 3 files, 19 tests, all green
```

设计取舍：把 `score_stock` / `quantify_market` / `compute_position_plan` 从 `model.py` 的
I/O 编排中抽离到 `factors.py`，使其可被直接 import 测试——这是「可测性」与「关注点分离」的落地。

---

## 五、接入真实数据（生产路径）

`model.py` 是端到端生产编排，依赖数据后端。本开源仓库只演示 `SampleProvider`（本地 JSON）。
接入真实行情只需实现 `data_provider.DataProvider`：

- `screen()`：调用 `westock-mcp` tool_filter 跑成长/周期两条线，产出 `screener_raw` 结构；
- `get_klines()`：取个股日线（动量因子用，见 `model.compute_momentum` 的翻页补齐逻辑）。

实现 `MCPProvider` 后，`model.py` 即可端到端运行，并产出带 HTML 报告的完整选股结果。

---

## 六、已知边界（诚实声明）

- 示例数据为**合成**，仅演示流程；真实样本来自特定下行市，结论不可外推。
- 评分/择时逻辑经过回测，但**短期(1日)预测力弱、优势在 5-20 日窗口**；统计上多未达显著。
- 因子（UMD 动量）经 A/B 回测验证为噪声，已切换为「价格成熟度惩罚」软降权（不硬剔除）。
- 本仓库是**工程能力样本**，不是「能稳定盈利的策略」。

---

## 七、隐私与合规

- 仓库已通过 `.gitignore` **硬性排除**所有真实持仓/盈亏文件（`*_holdings*.json` 等）；
- 所有脚本路径已改为相对路径，**不泄露任何个人目录/用户名**；
- 初选数据 `data/screener_raw.json` 为合成示例，非真实账户数据。
