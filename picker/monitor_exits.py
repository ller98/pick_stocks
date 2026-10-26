#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
真实持仓退出纪律执行脚本（2026-08-31 由"监控"升级为"强制纪律"）
读取 _my_holdings.json + _quant_summary.json + _pick_history.jsonl
输出每个持仓的退出/减仓信号（硬纪律优先，模型状态为辅）。

硬止损/退出纪律（来自 _exit_rules.json，强制执行）：
- 红色（强制减仓）：从成本价回撤 ≥ max_drawdown_pct% → 硬止损；或收盘价跌破 break_ma → 破位减仓；
                 或持有超 time_stop_days 日且浮亏 ≥ time_stop_min_pct%。
- 黄色（建议关注）：模型状态 warn；或已不在今日 final_picks；或持有天数>60日仍横盘/浮亏；
                 或PEG接近1.0（>=0.8）。
- 绿色（持有观察）：无明显触发信号。
- 模型侧红色（reject/剔除/PEG≥1.0/增速<20%）仍作为辅助退出信号。

注意：
- 缺少真实买入日期，以模型首次入选日期作为“纳入观察日”的代理。
- 脚本不会自动下单，仅输出纪律执行参考；破位判定需联网取MA，失败时跳过该条不阻断。
"""
import json
import os
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta

CWD = os.path.dirname(os.path.abspath(__file__))


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_jsonl(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def load_exit_rules():
    p = os.path.join(CWD, "_exit_rules.json")
    if os.path.exists(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            pass
    return {"enabled": True, "max_drawdown_pct": 8, "break_ma": "MA20",
            "time_stop_days": 60, "time_stop_min_pct": -10, "hard_exit": True}


def fetch_break_ma(codes, window=60):
    """批量取个股 break_ma（MA20/MA60）最新值；失败返回 {} 不阻断流程。"""
    out = {}
    if not codes:
        return out
    ma_name = load_exit_rules().get("break_ma", "MA20")
    n = 20 if ma_name == "MA20" else 60
    try:
        cmd = ["npx", "-y", "westock-data-skillhub@1.0.5", "kline",
               ",".join(codes), "--period", "day", "--limit", str(max(window, n)), "--raw"]
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                               errors="ignore", shell=True)
        try:
            kl = json.loads(res.stdout.strip())
        except Exception:
            return out
        items = kl if isinstance(kl, list) else (kl.get("data") if isinstance(kl, dict) else [])
        by_code = {}
        for it in (items or []):
            c = it.get("symbol") or it.get("code")
            if c:
                by_code.setdefault(c, []).append(it)
        for c in codes:
            bars = by_code.get(c, [])
            if not bars:
                continue
            bars_sorted = list(reversed(bars))
            closes = [float(b.get("last") or b.get("close")) for b in bars_sorted
                      if (b.get("last") or b.get("close"))]
            if len(closes) >= n:
                # 同时返回 MA 值与最新收盘价（bars_sorted[0] 为最新一根），供 current 缺失时兜底
                out[c] = {"ma": round(sum(closes[-n:]) / n, 3), "close": closes[0]}
    except Exception as e:
        print(f"[WARN] 破位MA取数失败，跳过破位判定：{e}", file=sys.stderr)
    return out


def build_code_index(summary_detail):
    return {item["code"]: item for item in summary_detail}


def build_history_index(pick_rows):
    first = {}
    latest = {}
    count = defaultdict(int)
    all_dates = set()
    for r in pick_rows:
        d = r.get("date")
        code = r.get("code")
        if not d or not code:
            continue
        all_dates.add(d)
        if code not in first or d < first[code]:
            first[code] = d
        if code not in latest or d > latest[code]:
            latest[code] = d
        count[code] += 1
    return first, latest, dict(count), sorted(all_dates)


def days_between(d1, d2):
    try:
        a = datetime.strptime(d1, "%Y-%m-%d")
        b = datetime.strptime(d2, "%Y-%m-%d")
        return (b - a).days
    except Exception:
        return None


def get_latest_summary_path(cwd):
    candidates = [os.path.join(cwd, "_quant_summary.json"),
                  os.path.join(cwd, "_quant_summary_v2.json")]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


def evaluate_holding(h, detail_item, final_picks, first_date, latest_date, today, latest_date_str,
                     exit_rules=None, break_price=None, cur_override=None, cur_source="synced"):
    signals = {"exit": [], "watch": [], "hold": []}

    code = h["code"]
    pnl_pct = h.get("pnl_pct", 0)
    in_final = code in final_picks
    status = (detail_item.get("status") if detail_item else None)
    peg = detail_item.get("peg") if detail_item else None
    pb = detail_item.get("PB") if detail_item else None
    growth = detail_item.get("np_deduct_growth") if detail_item else None
    reasons = detail_item.get("reasons", []) if detail_item else []

    # 拆分 reject_reasons / warn_notes（2026-08-27 修复：warn 不再误判为 EXIT）
    # 兼容旧版 summary（仅有 reasons 单字段）
    reject_reasons = detail_item.get("reject_reasons", []) if detail_item else []
    warn_notes = detail_item.get("warn_notes", []) if detail_item else []
    if not reject_reasons and not warn_notes and detail_item:
        if status == "reject":
            reject_reasons = reasons
        elif status == "warn":
            warn_notes = reasons

    # 纳入观察日（模型首次入选）
    observe_days = days_between(first_date, today) if first_date else None

    # —— 硬止损/退出纪律（2026-08-31，强制执行，优先级最高）——
    if exit_rules and exit_rules.get("enabled", True):
        mdd = exit_rules.get("max_drawdown_pct", 8)
        cost = h.get("cost")
        cur = cur_override if cur_override is not None else h.get("current")
        if cost and cur and cost > 0:
            cost_dd = (cur - cost) / cost
            if cost_dd <= -mdd / 100.0:
                signals["exit"].append(f"硬止损触发：从成本价回撤{cost_dd:.1%} ≥ -{mdd}%，强制减仓")
        if break_price is not None and cur is not None and break_price > 0 and cur < break_price:
            signals["exit"].append(
                f"破位触发：收盘价¥{cur:.2f}跌破{exit_rules.get('break_ma', 'MA20')}¥{break_price:.2f}，强制减仓")
        ts_days = exit_rules.get("time_stop_days", 60)
        ts_min = exit_rules.get("time_stop_min_pct", -10)
        if (observe_days is not None and observe_days > ts_days and pnl_pct is not None
                and pnl_pct <= ts_min):
            signals["exit"].append(
                f"时间止损触发：纳入观察{observe_days}日且浮亏{pnl_pct:.1f}% ≤ {ts_min}%，评估退出")

    # 红色：模型明确剔除 / 剔除理由非空 / 增速<20%的成长逻辑 / PEG兑现
    if status == "reject":
        signals["exit"].append("模型今日状态为 reject")
    if status == "warn":
        signals["watch"].append("模型今日状态为 warn")
    if reject_reasons:
        signals["exit"].append(f"剔除理由：{'；'.join(reject_reasons)}")
    if warn_notes:
        signals["watch"].append(f"观察项：{'；'.join(warn_notes)}")
    if peg is not None and peg >= 1.0:
        signals["exit"].append(f"PEG={peg:.3f} 已≥1.0，估值修复完成")
    elif peg is not None and peg >= 0.8:
        signals["watch"].append(f"PEG={peg:.3f} 接近1.0")
    if growth is not None and growth < 0.2:
        signals["exit"].append(f"扣非增速={growth:.1%} 已低于20%")

    # 黄色：不在今日 final_picks / 长期横盘 / 浮亏过久
    if not in_final:
        if status == "pass":
            signals["watch"].append("不在今日 final_picks，模型已降温")
        elif not status:
            signals["watch"].append("不在今日模型复核池，无最新数据")
    if observe_days is not None and observe_days > 60:
        if -10 <= pnl_pct <= 10:
            signals["watch"].append(f"纳入观察{observe_days}日仍横盘/微亏，警惕价值陷阱")
        elif pnl_pct < -10:
            signals["exit"].append(f"纳入观察{observe_days}日且浮亏>{pnl_pct:.1f}%，逻辑未兑现")

    # 绿色：无明显信号
    if not signals["exit"] and not signals["watch"]:
        signals["hold"].append("当前无退出/关注信号，按模型仓位纪律持有")

    # 确定主信号
    if signals["exit"]:
        level = "exit"
    elif signals["watch"]:
        level = "watch"
    else:
        level = "hold"

    return {
        "level": level,
        "signals": signals,
        "status": status,
        "in_final": in_final,
        "peg": peg,
        "pb": pb,
        "growth": growth,
        "first_date": first_date,
        "latest_date": latest_date,
        "observe_days": observe_days,
        "cur_source": cur_source,
        "base_price": cur,
    }


def main():
    today = datetime.now().strftime("%Y-%m-%d")

    holdings_path = os.path.join(CWD, "_my_holdings.json")
    pick_history_path = os.path.join(CWD, "_pick_history.jsonl")
    summary_path = get_latest_summary_path(CWD)

    if not os.path.exists(holdings_path):
        print(f"未找到 {holdings_path}，请先录入持仓。")
        sys.exit(1)
    if not summary_path:
        print("未找到 _quant_summary.json，请先运行选股模型。")
        sys.exit(1)

    holdings = load_json(holdings_path)
    summary = load_json(summary_path)
    pick_rows = load_jsonl(pick_history_path)

    exit_rules = load_exit_rules()
    market_data = fetch_break_ma([h["code"] for h in holdings.get("holdings", [])])
    break_map = {c: v.get("ma") for c, v in market_data.items()}
    close_map = {c: v.get("close") for c, v in market_data.items()}

    detail_index = build_code_index(summary.get("detail", []))
    # final_picks 格式为 "sz002772 众兴菌业"，提取裸代码用于匹配
    final_picks = set()
    for p in summary.get("final_picks", []):
        fp = p.split()[0] if isinstance(p, str) and p.split() else str(p)
        final_picks.add(fp)
    first_date_map, latest_date_map, count_map, all_dates = build_history_index(pick_rows)

    market_gate = summary.get("market_gate", {})
    sentiment = summary.get("sentiment", "")
    trend = summary.get("trend", "")
    action = summary.get("action", "")
    position = summary.get("position", "")

    results = []
    for h in holdings.get("holdings", []):
        code = h["code"]
        detail_item = detail_index.get(code)
        first_date = first_date_map.get(code)
        latest_date = latest_date_map.get(code)

        # 晨间基准价：优先用户同步的 current；缺失时联网昨收兜底并明确告警（避免静默失效）
        cur = h.get("current")
        cur_source = "synced"
        if not cur or cur <= 0:
            fb = close_map.get(code)
            if fb:
                cur = fb
                cur_source = "auto_fallback"
                print(f"[WARN] 持仓 {code}({h.get('name')}) 缺少同步 current，用联网昨收 ¥{fb:.2f} 兜底"
                      f"——请盘后同步 _my_holdings.json，否则纪律判定可能失真", file=sys.stderr)
            else:
                cur_source = "missing"
                print(f"[WARN] 持仓 {code}({h.get('name')}) 缺少 current 且联网兜底失败，"
                      f"回撤/破位判定将跳过", file=sys.stderr)

        eval_result = evaluate_holding(
            h, detail_item, final_picks, first_date, latest_date, today, summary.get("date", today),
            exit_rules=exit_rules, break_price=break_map.get(code),
            cur_override=cur, cur_source=cur_source
        )
        eval_result["code"] = code
        eval_result["name"] = h["name"]
        eval_result["quantity"] = h.get("quantity")
        eval_result["cost"] = h.get("cost")
        eval_result["current"] = h.get("current")
        eval_result["value"] = h.get("value")
        eval_result["pnl"] = h.get("pnl")
        eval_result["pnl_pct"] = h.get("pnl_pct")
        eval_result["history_count"] = count_map.get(code, 0)
        results.append(eval_result)

    # 汇总
    stats = {"exit": 0, "watch": 0, "hold": 0}
    for r in results:
        stats[r["level"]] += 1

    output = {
        "date": today,
        "snapshot_date": holdings.get("meta", {}).get("snapshot_date"),
        "base_price_policy": "晨间基准价优先取 _my_holdings.json 的 current（用户盘后同步）；"
                             "缺失时用联网昨收兜底（cur_source=auto_fallback）",
        "summary_path": summary_path,
        "market": {
            "sentiment": sentiment,
            "trend": trend,
            "action": action,
            "position": position,
        },
        "stats": stats,
        "holdings": results,
    }

    out_json = os.path.join(CWD, "_exit_monitor.json")
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    # 生成 HTML 报告
    html_path = os.path.join(CWD, f"持仓退出监控报告_{today.replace('-', '')}.html")
    write_html_report(html_path, holdings, output, results, exit_rules)

    print(f"已生成退出监控 JSON: {out_json}")
    print(f"已生成退出监控报告: {html_path}")


def write_html_report(path, holdings, output, results, exit_rules=None):
    today = output["date"]
    market = output["market"]
    total_value = holdings.get("total_value", 0)
    total_pnl = holdings.get("total_pnl", 0)
    total_pnl_pct = holdings.get("total_pnl_pct", 0)
    snapshot_date = holdings.get("meta", {}).get("snapshot_date", "未知")

    color_map = {
        "exit": ("#E24B4A", "#FCEBEB", "评估退出"),
        "watch": ("#BA7517", "#FAEEDA", "建议关注"),
        "hold": ("#1D9E75", "#E1F5EE", "持有观察"),
    }

    rows_html = []
    for r in results:
        color, bg, label = color_map[r["level"]]
        reasons = r["signals"][r["level"]]
        reason_str = "；".join(reasons) if reasons else "-"
        peg_str = f"{r['peg']:.4f}" if r['peg'] is not None else "-"
        pb_str = f"{r['pb']:.3f}" if r['pb'] is not None else "-"
        growth_str = f"{r['growth']:.1%}" if r['growth'] is not None else "-"
        pnl_color = "#E24B4A" if r['pnl_pct'] >= 0 else "#1D9E75"
        observe_days = r['observe_days'] if r['observe_days'] is not None else "?"
        if r.get("base_price"):
            bp = f"{r['base_price']:.3f}"
            if r.get("cur_source") == "auto_fallback":
                bp += "*"
        else:
            bp = "缺失"
        rows_html.append(
            f"""
            <tr style="background-color:{bg}">
              <td style="padding:8px;border:1px solid #ddd"><b>{r['name']}</b><br><span style="color:#666;font-size:12px">{r['code']}</span></td>
              <td style="padding:8px;border:1px solid #ddd;text-align:center"><span style="color:{color};font-weight:500">{label}</span></td>
              <td style="padding:8px;border:1px solid #ddd;text-align:right">{r['quantity']}</td>
              <td style="padding:8px;border:1px solid #ddd;text-align:right">{r['cost']:.3f}</td>
              <td style="padding:8px;border:1px solid #ddd;text-align:right">{bp}</td>
              <td style="padding:8px;border:1px solid #ddd;text-align:right">{r['value']:,.2f}</td>
              <td style="padding:8px;border:1px solid #ddd;text-align:right">{r['pnl']:,.2f}<br><span style="color:{pnl_color};font-size:12px">{r['pnl_pct']:+.2f}%</span></td>
              <td style="padding:8px;border:1px solid #ddd;text-align:right">{peg_str}</td>
              <td style="padding:8px;border:1px solid #ddd;text-align:right">{pb_str}</td>
              <td style="padding:8px;border:1px solid #ddd;text-align:right">{growth_str}</td>
              <td style="padding:8px;border:1px solid #ddd;text-align:center">{'是' if r['in_final'] else '否'}</td>
              <td style="padding:8px;border:1px solid #ddd;text-align:center">{r['first_date'] or 'N/A'}<br><span style="color:#666;font-size:12px">({observe_days}日)</span></td>
              <td style="padding:8px;border:1px solid #ddd;font-size:13px">{reason_str}</td>
            </tr>
            """
        )

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>持仓退出纪律执行报告 {today}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Microsoft YaHei", sans-serif; margin: 24px; background: #fff; color: #333; line-height: 1.6; }}
  h1 {{ font-size: 20px; margin-bottom: 8px; }}
  .summary {{ background: #f7f7f7; padding: 16px; border-radius: 8px; margin: 16px 0; }}
  .summary div {{ margin: 4px 0; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 13px; margin-top: 16px; }}
  th {{ background: #f0f0f0; padding: 10px 8px; border: 1px solid #ddd; text-align: center; font-weight: 500; }}
  td {{ padding: 8px; border: 1px solid #ddd; }}
  .note {{ color: #666; font-size: 12px; margin-top: 16px; }}
  .legend span {{ display: inline-block; padding: 2px 8px; border-radius: 4px; margin-right: 8px; font-size: 12px; }}
</style>
</head>
<body>
<h1>持仓退出纪律执行报告（{today}）</h1>
<div class="summary">
  <div><b>总市值：</b>¥{total_value:,.2f}　<b>总盈亏：</b>¥{total_pnl:,.2f}（{total_pnl_pct:+.2f}%）</div>
  <div><b>晨间基准价来源：</b>用户同步 _my_holdings.json（current，同步于 {snapshot_date}）；缺失时用联网昨收兜底并标注 *</div>
  <div><b>市场环境：</b>{market.get('sentiment', '')} / {market.get('trend', '')}</div>
  <div><b>模型仓位建议：</b>{market.get('position', '')}，{market.get('action', '')}</div>
  <div><b>信号统计：</b>
    <span style="color:#E24B4A">评估退出 {output['stats']['exit']}</span>　
    <span style="color:#BA7517">建议关注 {output['stats']['watch']}</span>　
    <span style="color:#1D9E75">持有观察 {output['stats']['hold']}</span>
  </div>
</div>
<table>
  <tr>
    <th>名称/代码</th>
    <th>信号</th>
    <th>数量</th>
    <th>成本</th>
    <th>现价</th>
    <th>市值</th>
    <th>盈亏</th>
    <th>PEG</th>
    <th>PB</th>
    <th>扣非增速</th>
    <th>今日模型池</th>
    <th>首次入选日</th>
    <th>触发说明</th>
  </tr>
  {''.join(rows_html)}
</table>
<div class="note">
  <p><b>说明：</b></p>
  <ul>
    <li><b>红色"强制减仓"</b>：硬止损纪律优先——从成本价回撤≥{exit_rules.get('max_drawdown_pct',8)}%、或收盘价跌破{exit_rules.get('break_ma','MA20')}、或纳入观察超{exit_rules.get('time_stop_days',60)}日且浮亏≤{exit_rules.get('time_stop_min_pct',-10)}%；叠加模型侧 reject/剔除/PEG≥1.0/增速&lt;20%。</li>
    <li>黄色"建议关注"：模型状态 warn、剔除理由非空（仅观察项，非剔除）、已不在今日 final_picks、PEG 接近1.0、或观察超60日横盘/微亏。</li>
    <li>绿色"持有观察"：当前无退出/关注信号，按模型仓位纪律持有。</li>
    <li>"首次入选日"为模型历史首次选出日期，作为观察起点代理，不一定是你的真实买入日。</li>
    <li><b>现价列 * 标记</b>：表示该持仓的 _my_holdings.json 缺少同步 current，本日用联网昨收兜底——<b>请尽快盘后同步</b>，否则回撤/破位判定基于非你确认的价格，可能失真；未标记则为你的同步价。</li>
  </ul>
  <p>本报告为<b>退出纪律执行参考</b>（非单纯监控）：硬止损/破位规则为模型强制标准，触发即应减仓；脚本不会自动下单，实际操作请依个人风险承受力执行。不构成投资建议。</p>
</div>
</body>
</html>
"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


if __name__ == "__main__":
    main()
