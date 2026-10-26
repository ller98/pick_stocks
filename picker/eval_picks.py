#!/usr/bin/env python3
"""eval_picks.py - 评估历史选股的后续涨跌幅和胜率
读取 _pick_history.jsonl，用 westock-data kline 拉后续 1日/1周/1月/3月 收盘价，
计算绝对收益+相对沪深300超额，输出 _winrate_report.json + 选股胜率追踪报告.html
"""
import json, subprocess, sys, time, os
from pathlib import Path
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))
from board_filter import BOARD_LABELS, classify_board, load_excluded_boards

WORK_DIR = Path(__file__).parent
HISTORY_FILE = WORK_DIR / "_pick_history.jsonl"
WINRATE_JSON = WORK_DIR / "_winrate_report.json"
WINRATE_HTML = WORK_DIR / "选股胜率追踪报告.html"

HOLDING_PERIODS = [1, 5, 20, 60]
HS300_CODE = "sh000300"
PERIOD_LABELS = {"1d": "1日", "5d": "1周", "20d": "1月", "60d": "3月"}

PYTHON = sys.executable


def run_cli(args, retries=2):
    cmd = ["npx", "-y", "westock-data-skillhub@1.0.5"] + args
    for attempt in range(retries + 1):
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=30, shell=True)
            if res.returncode == 0 and res.stdout.strip():
                try:
                    return json.loads(res.stdout)
                except json.JSONDecodeError:
                    pass
            time.sleep(0.5)
        except subprocess.TimeoutExpired:
            time.sleep(1)
    return None


def get_kline(code, limit=120):
    """获取K线（前复权 qfq），返回按日期升序列表
    F1 加固：westock-data kline 默认即返回前复权(qfq)价（已实测：newest 价=实盘价，
    与 --fq qfq 完全一致；该接口不支持 bfq 不复权）。此处仍显式加 --fq qfq 固化意图，
    防止接口默认行为变更导致除权日价格跳变、胜率失真。
    前复权保证 entry 价与 future 价处于同一复权基准，持有期内分红/送转不影响收益计算。
    limit 取 120：覆盖 entry + 60 日(3月)窗口——冷启动期 60d 样本多因实际未达持有期而为空，
    待 11 月起样本才逐步闭合；留足余量避免边界截断。
    """
    data = run_cli(["kline", code, "--period", "day", "--limit", str(limit), "--raw", "--fq", "qfq"])
    if not data or not isinstance(data, list):
        return []
    return list(reversed(data))  # kline 返回最新在前，反转为升序


def find_price_on(klines, target_date):
    """找到 target_date 当天或之前最近的收盘价"""
    for k in klines:
        if k["date"] == target_date:
            return k.get("last")
    # 找最接近且 <= target_date 的
    for k in klines:
        if k["date"] <= target_date:
            return k.get("last")
    return None


def find_price_after(klines, entry_date, n_days):
    """找到 entry_date 之后的第 n_days 个交易日的收盘价"""
    entry_idx = None
    for i, k in enumerate(klines):
        if k["date"] >= entry_date:
            entry_idx = i
            break
    if entry_idx is None:
        return None
    target_idx = entry_idx + n_days
    if target_idx < len(klines):
        return klines[target_idx].get("last")
    return None


def calc_stats(results, only_first=False):
    """计算分组胜率
    only_first=True 时只统计每只票首次出现的那条（去重）
    """
    if only_first:
        results = [r for r in results if r.get("is_first_appearance")]
    stats = {}
    for period_label in [f"{n}d" for n in HOLDING_PERIODS]:
        groups = {"all": {"count": 0, "win": 0, "rets": [], "excess": []}}
        for tier in ["growth", "cyclical", "bank"]:
            groups[tier] = {"count": 0, "win": 0, "rets": [], "excess": []}

        for r in results:
            ret_data = r["returns"].get(period_label)
            if not ret_data or ret_data["abs"] is None:
                continue
            ret = ret_data["abs"]
            exc = ret_data["excess"]
            tier = r.get("tier", "growth")

            for gname in ["all", tier]:
                g = groups.get(gname)
                if g is None:
                    g = {"count": 0, "win": 0, "rets": [], "excess": []}
                    groups[gname] = g
                g["count"] += 1
                if ret > 0:
                    g["win"] += 1
                g["rets"].append(ret)
                if exc is not None:
                    g["excess"].append(exc)

        for g in groups.values():
            c = g["count"]
            g["winrate"] = round(g["win"] / c * 100, 1) if c > 0 else 0
            g["avg_ret"] = round(sum(g["rets"]) / c, 2) if c > 0 else 0
            g["avg_excess"] = round(sum(g["excess"]) / len(g["excess"]), 2) if g["excess"] else 0

        stats[period_label] = groups

    return stats


def generate_html(output):
    """生成HTML报告"""
    stats = output["stats"]
    stats_first = output.get("stats_first_only", {})
    results = output["results"]
    eval_date = output["eval_date"]
    unique_count = output.get("unique_picks", 0)
    total_count = output["total_picks"]

    # 按日期分组统计
    by_date = defaultdict(list)
    for r in results:
        by_date[r["date"]].append(r)

    # 计算每条记录的「持仓重复」标记
    # 遍历排序后的 results（按日期升序），对每只票累计到该日的出现次数
    sorted_for_history = sorted(results, key=lambda x: (x["date"], x["code"]))
    code_cum_count = {}    # 截至当前记录的累计出现次数（含本次）
    recent5_window = {}    # code -> 最近5个交易日的 date 列表
    recent10_window = {}   # code -> 最近10个交易日的 date 列表
    code_first_seen = {}   # code -> 首次入选日期

    from datetime import datetime as _dt, timedelta as _td
    history_marker = {}  # (date, code) -> dict(cum_count, last5_count, last10_count, is_first, gap_from_first)

    for r in sorted_for_history:
        date = r["date"]
        code = r["code"]
        d_obj = _dt.strptime(date, "%Y-%m-%d")
        if code not in code_cum_count:
            code_first_seen[code] = date
            is_first = True
        else:
            is_first = False
        code_cum_count[code] = code_cum_count.get(code, 0) + 1

        # 滑动窗口：剔除超出 5/10 个交易日的记录
        recent5_window.setdefault(code, [])
        recent10_window.setdefault(code, [])
        recent5_window[code] = [d for d in recent5_window[code] if d_obj - _dt.strptime(d, "%Y-%m-%d") <= _td(days=10)]
        recent10_window[code] = [d for d in recent10_window[code] if d_obj - _dt.strptime(d, "%Y-%m-%d") <= _td(days=20)]
        recent5_window[code].append(date)
        recent10_window[code].append(date)

        first_date = code_first_seen[code]
        gap_days = (d_obj - _dt.strptime(first_date, "%Y-%m-%d")).days

        history_marker[(date, code)] = {
            "cum_count": code_cum_count[code],
            "last5_count": len(recent5_window[code]),
            "last10_count": len(recent10_window[code]),
            "is_first": is_first,
            "gap_from_first": gap_days,
        }

    # 构建统计表格行
    stat_rows = ""
    for period in [f"{n}d" for n in HOLDING_PERIODS]:
        ps = stats.get(period, {})
        all_s = ps.get("all", {})
        growth_s = ps.get("growth", {})
        cyc_s = ps.get("cyclical", {})
        bank_s = ps.get("bank", {})
        stat_rows += f"""
        <tr>
          <td class="period">{period}</td>
          <td>{all_s.get('count',0)}</td>
          <td class="{'win' if all_s.get('winrate',0)>=50 else 'loss'}">{all_s.get('winrate',0)}%</td>
          <td>{all_s.get('avg_ret',0)}%</td>
          <td class="{'win' if all_s.get('avg_excess',0)>0 else 'loss'}">{all_s.get('avg_excess',0)}%</td>
          <td>{growth_s.get('count',0)}</td>
          <td class="{'win' if growth_s.get('winrate',0)>=50 else 'loss'}">{growth_s.get('winrate',0)}%</td>
          <td>{cyc_s.get('count',0)}</td>
          <td class="{'win' if cyc_s.get('winrate',0)>=50 else 'loss'}">{cyc_s.get('winrate',0)}%</td>
          <td>{bank_s.get('count',0)}</td>
          <td class="{'win' if bank_s.get('winrate',0)>=50 else 'loss'}">{bank_s.get('winrate',0)}%</td>
        </tr>"""

    # 构建双口径对比行
    compare_rows = ""
    for period in [f"{n}d" for n in HOLDING_PERIODS]:
        a = stats.get(period, {}).get("all", {})
        f = stats_first.get(period, {}).get("all", {})
        a_count = a.get("count", 0)
        f_count = f.get("count", 0)
        a_wr = a.get("winrate", 0)
        f_wr = f.get("winrate", 0)
        a_ex = a.get("avg_excess", 0)
        f_ex = f.get("avg_excess", 0)
        wr_diff = round(a_wr - f_wr, 1) if (a_count > 0 and f_count > 0) else None
        ex_diff = round(a_ex - f_ex, 2) if (a_count > 0 and f_count > 0) else None

        def color_posneg(v, is_pct=False):
            if v is None:
                return "—"
            cls = "win" if v > 0 else "loss" if v < 0 else ""
            return f'<span class="{cls}">{v:+.2f}</span>' if is_pct else f'<span class="{cls}">{v:+.1f}</span>'

        compare_rows += f"""
        <tr>
          <td class="period">{PERIOD_LABELS[period]} ({period})</td>
          <td class="{'win' if a_wr>=50 else 'loss'}">{a_wr}%</td>
          <td class="{'win' if a_ex>0 else 'loss'}">{a_ex:+.2f}%</td>
          <td class="{'win' if f_wr>=50 else 'loss'}">{f_wr}%</td>
          <td class="{'win' if f_ex>0 else 'loss'}">{f_ex:+.2f}%</td>
          <td>{wr_diff if wr_diff is None else color_posneg(wr_diff)}</td>
          <td>{color_posneg(ex_diff, True) if ex_diff is not None else '—'}</td>
        </tr>"""

    # 构建个股明细
    detail_rows = ""
    sorted_results = sorted(results, key=lambda x: (x["date"], x["tier"]))
    for r in sorted_results:
        ret_1d = r["returns"].get("1d", {})
        ret_5d = r["returns"].get("5d", {})
        ret_20d = r["returns"].get("20d", {})
        ret_60d = r["returns"].get("60d", {})
        r1 = ret_1d.get("abs")
        r5 = ret_5d.get("abs")
        r20 = ret_20d.get("abs")
        r60 = ret_60d.get("abs")
        e20 = ret_20d.get("excess")
        e60 = ret_60d.get("excess")

        def fmt(v, suffix="%"):
            if v is None:
                return "—"
            cls = "win" if v > 0 else "loss" if v < 0 else ""
            return f'<span class="{cls}">{v:+.2f}{suffix}</span>'

        tier_cls = {"growth": "tag-blue", "cyclical": "tag-orange", "bank": "tag-green"}.get(r.get("tier"), "")
        # 注：tier="bank" 现含 银行 + 非银金融(券商/保险等)，展示统一为「金融」
        # 持仓重复标记
        hm = history_marker.get((r["date"], r["code"]), {"cum_count": 1, "last5_count": 1, "is_first": True, "gap_from_first": 0})
        if hm["is_first"]:
            mark = '<span class="tag tag-green">新选</span>'
        elif hm["last10_count"] >= 4:
            mark = f'<span class="tag tag-red">频繁</span>'
        elif hm["last10_count"] >= 2:
            mark = f'<span class="tag tag-orange">重复</span>'
        else:
            mark = f'<span class="tag tag-orange">重复</span>'
        # 距离首次出现的天数
        gap = hm["gap_from_first"]
        detail_rows += f"""
        <tr>
          <td>{r['date']}</td>
          <td><span class="tag {tier_cls}">{r.get('tier','')}</span></td>
          <td class="name">{r['name']}</td>
          <td class="code">{r['code']}</td>
          <td>{r.get('entry_price','—')}</td>
          <td>{fmt(r1)}</td>
          <td>{fmt(r5)}</td>
          <td>{fmt(r20)}</td>
          <td>{fmt(r60)}</td>
          <td>{fmt(e20)}</td>
          <td>{fmt(e60)}</td>
          <td>{hm['cum_count']}次<span style='color:#8b949e;font-size:10px'> (D+{gap})</span></td>
          <td>{hm['last5_count']}次</td>
          <td>{mark}</td>
        </tr>"""

    # 日期汇总
    date_summary = ""
    for date in sorted(by_date.keys()):
        picks = by_date[date]
        count = len(picks)
        tiers = ", ".join(sorted(set(p.get("tier", "") for p in picks)))
        ret_5d_vals = [p["returns"].get("5d", {}).get("abs") for p in picks if p["returns"].get("5d", {}).get("abs") is not None]
        avg_5d = round(sum(ret_5d_vals) / len(ret_5d_vals), 2) if ret_5d_vals else None
        date_summary += f"""
        <tr>
          <td>{date}</td>
          <td>{count}</td>
          <td>{tiers}</td>
          <td>{f'{avg_5d:+.2f}%' if avg_5d is not None else '—'}</td>
        </tr>"""

    # 持仓重复风险块：取最近 5 个交易日，统计每只票的累计入选次数
    sorted_dates = sorted(by_date.keys())
    last5_dates = set(sorted_dates[-5:]) if len(sorted_dates) >= 5 else set(sorted_dates)
    last5_picks = [r for r in results if r["date"] in last5_dates]

    from collections import Counter as _Counter
    recent_code_count = _Counter(r["code"] for r in last5_picks)
    recent_code_meta = {}
    for r in last5_picks:
        c = r["code"]
        if c not in recent_code_meta:
            recent_code_meta[c] = {"name": r["name"], "tier": r.get("tier", ""), "dates": []}
        if r["date"] not in recent_code_meta[c]["dates"]:
            recent_code_meta[c]["dates"].append(r["date"])

    # 按入选频次排序
    sorted_recent = sorted(recent_code_count.items(), key=lambda x: (-x[1], x[0]))
    repeat_section = '<div class="section"><h2>持仓重复风险（最近 5 个交易日）</h2>'
    if not sorted_recent:
        repeat_section += '<p style="color:#8b949e;">数据不足，暂无持仓重复统计。</p>'
    else:
        # 分类：新选(1次) / 重复(2-3次) / 频繁(4-5次)
        new_codes = [(c, n) for c, n in sorted_recent if n == 1]
        repeat_codes = [(c, n) for c, n in sorted_recent if 2 <= n <= 3]
        frequent_codes = [(c, n) for c, n in sorted_recent if n >= 4]

        repeat_section += '<table>'
        repeat_section += '<tr><th>分类</th><th>数量</th><th>标的明细（按入选次数降序）</th></tr>'

        def fmt_codes(code_list):
            if not code_list:
                return '<span style="color:#8b949e;">—</span>'
            parts = []
            for c, n in code_list:
                m = recent_code_meta.get(c, {})
                tier_cn = {"growth": "成长", "cyclical": "周期", "bank": "金融"}.get(m.get("tier", ""), "")
                if n >= 4:
                    badge = f'<span class="tag tag-red">{n}次</span>'
                elif n >= 2:
                    badge = f'<span class="tag tag-orange">{n}次</span>'
                else:
                    badge = f'<span class="tag tag-green">{n}次</span>'
                parts.append(f'{badge} {m.get("name","?")} ({c}) <span style="color:#8b949e;font-size:10px">[{tier_cn}]</span>')
            return '<br>'.join(parts)

        repeat_section += f'<tr><td><span class="tag tag-red">频繁 (≥4次)</span></td><td>{len(frequent_codes)}</td><td>{fmt_codes(frequent_codes)}</td></tr>'
        repeat_section += f'<tr><td><span class="tag tag-orange">重复 (2-3次)</span></td><td>{len(repeat_codes)}</td><td>{fmt_codes(repeat_codes)}</td></tr>'
        repeat_section += f'<tr><td><span class="tag tag-green">新选 (1次)</span></td><td>{len(new_codes)}</td><td>{fmt_codes(new_codes)}</td></tr>'
        repeat_section += '</table>'

        # 建议
        if frequent_codes:
            advice = f'<p style="color:#ff7b72;margin-top:8px;">⚠️ 检测到 <b>{len(frequent_codes)} 只</b>频繁入选（5日内≥4次）。结合双口径胜率：5日胜率 首次 {stats_first.get("5d",{}).get("all",{}).get("winrate",0)}% vs 独立 {stats.get("5d",{}).get("all",{}).get("winrate",0)}%，高位再选反而接盘。建议：<b>已持仓的不再加仓</b>，关注新高频票的「高位风险」。</p>'
        elif repeat_codes:
            advice = f'<p style="color:#d29922;margin-top:8px;">⏰ 近 5 日有 {len(repeat_codes)} 只票被重复推荐（2-3次）。这些属于「持续看好」但也已积累涨幅，可关注「累计入选次数」列。5日胜率：独立 {stats.get("5d",{}).get("all",{}).get("winrate",0)}% / 首次 {stats_first.get("5d",{}).get("all",{}).get("winrate",0)}%。</p>'
        else:
            advice = '<p style="color:#3fb950;margin-top:8px;">✅ 近 5 日选股池更新度较高，重复入选票少。</p>'
        repeat_section += advice

    repeat_section += '</div>'

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>选股胜率追踪报告</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", sans-serif; background: #0d1117; color: #c9d1d9; padding: 20px; }}
  .header {{ text-align: center; margin-bottom: 24px; }}
  .header h1 {{ font-size: 22px; color: #58a6ff; margin-bottom: 4px; }}
  .header .meta {{ font-size: 13px; color: #8b949e; }}
  .section {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px 20px; margin-bottom: 16px; }}
  .section h2 {{ font-size: 16px; color: #58a6ff; margin-bottom: 12px; border-bottom: 1px solid #21262d; padding-bottom: 8px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ padding: 6px 10px; text-align: center; border-bottom: 1px solid #21262d; }}
  th {{ color: #8b949e; font-weight: 600; background: #161b22; }}
  tr:hover td {{ background: #1c2128; }}
  td.name {{ text-align: left; color: #e6edf3; }}
  td.code {{ color: #8b949e; font-family: monospace; }}
  td.period {{ font-weight: 600; color: #58a6ff; }}
  .win {{ color: #f85149; }}
  .loss {{ color: #3fb950; }}
  .tag {{ display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 11px; }}
  .tag-blue {{ background: #1f6feb33; color: #58a6ff; }}
  .tag-orange {{ background: #d2992233; color: #d29922; }}
  .tag-green {{ background: #3fb95033; color: #3fb950; }}
  .summary-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 16px; }}
  .stat-card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 12px 16px; }}
  .stat-card .label {{ font-size: 12px; color: #8b949e; }}
  .stat-card .value {{ font-size: 20px; font-weight: 700; color: #58a6ff; }}
</style>
</head>
<body>
  <div class="header">
    <h1>选股胜率追踪报告</h1>
    <div class="meta">评估时间：{eval_date} ｜ 选股跟踪记录：{output['total_picks']} 条</div>
  </div>

  <div class="summary-grid">
    <div class="stat-card"><div class="label">总跟踪标的</div><div class="value">{output['total_picks']}</div></div>
    <div class="stat-card"><div class="label">1周胜率</div><div class="value">{stats.get('5d',{}).get('all',{}).get('winrate',0)}%</div></div>
    <div class="stat-card"><div class="label">1周平均超额</div><div class="value">{stats.get('5d',{}).get('all',{}).get('avg_excess',0)}%</div></div>
    <div class="stat-card"><div class="label">1月胜率</div><div class="value">{stats.get('20d',{}).get('all',{}).get('winrate',0)}%</div></div>
    <div class="stat-card"><div class="label">1月平均超额</div><div class="value">{stats.get('20d',{}).get('all',{}).get('avg_excess',0)}%</div></div>
    <div class="stat-card"><div class="label">3月胜率</div><div class="value">{stats.get('60d',{}).get('all',{}).get('winrate',0)}%</div></div>
    <div class="stat-card"><div class="label">3月平均超额</div><div class="value">{stats.get('60d',{}).get('all',{}).get('avg_excess',0)}%</div></div>
  </div>

  <div class="section">
    <h2>双口径胜率对比：独立观测 vs 首次出现</h2>
    <div style="font-size:12px;color:#8b949e;margin-bottom:10px;">
      <b style="color:#e6edf3;">独立观测</b>：每次出现分别算一次（适合"每日调仓"策略，反映选股模型本身的 alpha 强度）。样本 {total_count} 条。<br>
      <b style="color:#e6edf3;">首次出现</b>：每只票只算最早入选那天（适合"持仓式"策略，假设不再加仓）。样本 {unique_count} 条。<br>
      <span style="color:#d29922;">差异解读</span>：两者差距越大 → 说明模型倾向"重复推荐"少数强势股。差距小 → 每天名单差异大。
    </div>
    <table>
      <tr>
        <th rowspan="2">持有周期</th>
        <th colspan="2">独立观测（每次出现）</th>
        <th colspan="2">首次出现（去重）</th>
        <th colspan="2">差异（独立 - 首次）</th>
      </tr>
      <tr>
        <th>胜率</th><th>超额</th>
        <th>胜率</th><th>超额</th>
        <th>胜率差</th><th>超额差</th>
      </tr>
      {compare_rows}
    </table>
  </div>

  <div class="section">
    <h2>胜率统计（按持有周期 × 分层）</h2>
    <table>
      <tr>
        <th rowspan="2">持有周期</th>
        <th colspan="4">全部</th>
        <th colspan="2">成长</th>
        <th colspan="2">周期</th>
        <th colspan="2">金融</th>
      </tr>
      <tr>
        <th>样本</th><th>胜率</th><th>平均收益</th><th>超额</th>
        <th>样本</th><th>胜率</th><th>样本</th><th>胜率</th><th>样本</th><th>胜率</th>
      </tr>
      {stat_rows}
    </table>
  </div>

  <div class="section">
    <h2>每日选股汇总</h2>
    <table>
      <tr><th>选股日期</th><th>标的数</th><th>分层</th><th>1周平均收益</th></tr>
      {date_summary}
    </table>
  </div>

  {repeat_section}

  <div class="section">
    <h2>个股明细（持仓重复标记）</h2>
    <table>
      <tr>
        <th>选股日</th><th>分层</th><th>名称</th><th>代码</th><th>入场价</th>
        <th>1日</th><th>1周</th><th>1月</th><th>3月</th><th>1月超额</th><th>3月超额</th>
        <th>累计入选</th><th>近5日入选</th><th>入选标记</th>
      </tr>
      {detail_rows}
    </table>
  </div>

  <p style="text-align:center;color:#8b949e;font-size:12px;margin-top:16px;">
    以上内容仅为模型推演结果的跟踪统计，不构成投资建议。
  </p>
</body>
</html>"""


def main():
    if not HISTORY_FILE.exists():
        print("[eval] _pick_history.jsonl not found")
        sys.exit(1)

    picks = []
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                picks.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    # ---- 板块权限排除 ----
    # 账户无交易权限的板块（如创业板）不计入胜率统计，否则胜率不反映"实际可执行"成绩。
    # 历史记录保留原样（仅打标不删除），排除数量记入报告 JSON 供追溯。
    _excluded_boards = load_excluded_boards()
    board_excluded_recs = []
    if _excluded_boards:
        _kept = []
        for p in picks:
            if classify_board(p.get("code")) in _excluded_boards:
                p["_excluded_board"] = classify_board(p.get("code"))
                board_excluded_recs.append(p)
            else:
                _kept.append(p)
        if board_excluded_recs:
            labels = "、".join(BOARD_LABELS.get(b, b) for b in sorted(_excluded_boards))
            print(f"[eval] 板块权限排除（{labels}）：{len(board_excluded_recs)} 条不计入胜率")
            _by_code = {}
            for p in board_excluded_recs:
                _by_code.setdefault((p.get("code"), p.get("name")), 0)
                _by_code[(p.get("code"), p.get("name"))] += 1
            for (c, n), k in sorted(_by_code.items(), key=lambda x: -x[1]):
                print(f"    - {c} {n} × {k} 次")
        picks = _kept

    print(f"[eval] {len(picks)} picks to evaluate")

    # 给每条记录标记是否首次出现（按 code 升序遍历，date 最小的为首次）
    picks_sorted_by_date = sorted(picks, key=lambda x: x["date"])
    seen_codes = set()
    unique_count = 0
    for p in picks_sorted_by_date:
        if p["code"] not in seen_codes:
            p["is_first_appearance"] = True
            seen_codes.add(p["code"])
            unique_count += 1
        else:
            p["is_first_appearance"] = False

    print(f"[eval] unique picks (first appearance only): {unique_count}")

    # 获取沪深300 K线
    hs300_klines = get_kline(HS300_CODE, 120)
    print(f"[eval] HS300 klines: {len(hs300_klines)}")

    # 按去重获取每只股票的K线
    all_codes = list(set(p["code"] for p in picks))
    code_klines = {}
    for i, code in enumerate(all_codes):
        klines = get_kline(code, 120)
        code_klines[code] = klines
        if (i + 1) % 10 == 0:
            print(f"  [{i + 1}/{len(all_codes)}] fetched")
            time.sleep(0.3)
        else:
            time.sleep(0.1)

    # 评估每条 pick
    results = []
    for p in picks:
        code = p["code"]
        entry_date = p.get("close_date") or p.get("date", "")
        klines = code_klines.get(code, [])

        entry_price = find_price_on(klines, entry_date)
        hs300_entry = find_price_on(hs300_klines, entry_date)

        rec = {**p, "entry_price": entry_price, "returns": {}}

        for n in HOLDING_PERIODS:
            future_price = find_price_after(klines, entry_date, n)
            hs300_future = find_price_after(hs300_klines, entry_date, n)

            ret = (future_price - entry_price) / entry_price * 100 if (entry_price and future_price) else None
            hs_ret = (hs300_future - hs300_entry) / hs300_entry * 100 if (hs300_entry and hs300_future) else None
            excess = round(ret - hs_ret, 2) if (ret is not None and hs_ret is not None) else None

            rec["returns"][f"{n}d"] = {
                "abs": round(ret, 2) if ret is not None else None,
                "hs300": round(hs_ret, 2) if hs_ret is not None else None,
                "excess": excess,
            }

        results.append(rec)

    # 统计胜率 — 两个口径
    stats_all = calc_stats(results, only_first=False)
    stats_first = calc_stats(results, only_first=True)

    # 输出 JSON
    output = {
        "eval_date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "total_picks": len(results),
        "unique_picks": unique_count,
        "board_excluded_boards": sorted(_excluded_boards),
        "board_excluded_count": len(board_excluded_recs),
        "board_excluded_detail": [
            {"date": p.get("date"), "code": p.get("code"), "name": p.get("name"),
             "board": p.get("_excluded_board")}
            for p in board_excluded_recs
        ],
        "stats": stats_all,
        "stats_first_only": stats_first,
        "results": results,
    }
    with open(WINRATE_JSON, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    # 追加胜率快照（支持周环比判定）
    # 修复：剔除 rets/excess 原始数组，避免快照文件膨胀固化
    def _clean_snapshot(d):
        if not d:
            return {}
        return {k: v for k, v in d.items() if k not in ("rets", "excess")}

    snapshot_file = WORK_DIR / "_winrate_snapshots.jsonl"
    snapshot = {
        "eval_date": output["eval_date"],
        "total_picks": output["total_picks"],
        "unique_picks": output["unique_picks"],
        "stats_5d_all": _clean_snapshot(stats_all.get("5d", {}).get("all", {})),
        "stats_5d_growth": _clean_snapshot(stats_all.get("5d", {}).get("growth", {})),
        "stats_5d_cyclical": _clean_snapshot(stats_all.get("5d", {}).get("cyclical", {})),
        "stats_5d_bank": _clean_snapshot(stats_all.get("5d", {}).get("bank", {})),
        "stats_first_5d_all": _clean_snapshot(stats_first.get("5d", {}).get("all", {})),
    }
    with open(snapshot_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
    print(f"[eval] Snapshot appended to {snapshot_file}")

    # 输出 HTML
    html = generate_html(output)
    with open(WINRATE_HTML, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"[eval] Done. {len(results)} picks evaluated.")
    print(f"[eval] JSON: {WINRATE_JSON}")
    print(f"[eval] HTML: {WINRATE_HTML}")


if __name__ == "__main__":
    main()
