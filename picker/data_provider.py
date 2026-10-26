#!/usr/bin/env python3
"""data_provider.py - 数据层抽象（解耦 westock-mcp，使仓库可离线演示）。

生产环境 model.py 通过 westock-mcp / westock-data CLI 获取初选、行情、K线；
为让本开源仓库「clone 即可跑、无密钥、无外部依赖」，这里提供 SampleProvider（读本地示例 JSON）。
真实接入只需实现 DataProvider 接口（见 MCPProvider 占位）。
"""
import json
import os
from abc import ABC, abstractmethod

WORK_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class DataProvider(ABC):
    @abstractmethod
    def screen(self) -> dict:
        """返回初选结果：{generated_at, source, growth:[...], cyclical:[...]}。"""

    @abstractmethod
    def get_klines(self, code: str, start: str, end: str):
        """返回个股日线（用于动量因子）。离线示例返回空列表。"""


class SampleProvider(DataProvider):
    """离线示例数据源：直接读取 data/screener_raw.json。"""

    def __init__(self, raw_path=None):
        self.raw_path = raw_path or os.path.join(WORK_DIR, "data", "screener_raw.json")

    def screen(self) -> dict:
        with open(self.raw_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def get_klines(self, code, start, end):
        return []


class MCPProvider(DataProvider):
    """生产数据源占位（依赖 westock-mcp / westock-data CLI，不在开源仓库实现）。

    实现要点：
      - screen(): 调用 mcp__westock-mcp__tool_filter 跑成长/周期两条线，写入 screener_raw 结构
      - get_klines(): 调用 westock-data kline 取日线（见 model.compute_momentum 的翻页补齐逻辑）
    接入后即可让 model.py 端到端运行；本开源仓库仅演示 SampleProvider 路径。
    """

    def screen(self) -> dict:  # pragma: no cover - 需要外部 MCP
        raise NotImplementedError("MCPProvider 需接入 westock-mcp 后实现")

    def get_klines(self, code, start, end):  # pragma: no cover
        raise NotImplementedError("MCPProvider 需接入 westock-mcp 后实现")
