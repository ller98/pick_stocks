"""
annotate_repeat.py - 给每日选股报告（量化选股模型报告_YYYYMMDD.html）补充
「持仓重复」标记：从 _pick_history.jsonl 读取历史选股，对最终备选标的表格
新增「持仓重复」列，并增加顶部提示块，让用户即时看到「已被重复推荐」。
"""
import json
import re
import sys
from pathlib import Path
from collections import Counter
from datetime import datetime, timedelta


WORKDIR = Path(__file__).resolve().parent.parent
HISTORY_FILE = WORKDIR / "_pick_history.jsonl"


def load_history():
    """读取 _pick_history.jsonl，返回 code -> [date,...]"""
    if not HISTORY_FILE.exists():
        return {}
    history = {}
    with open(HISTORY_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            code = rec.get("code")
            date = rec.get("date") or rec.get("close_date") or ""
            if not code or not date:
                continue
            history.setdefault(code, []).append(date)
    # 各票的历史日期升序
    for code in history:
        history[code] = sorted(set(history[code]))
    return history


def _repeat_mark(dates_for_code, today):
    """根据日期序列计算「累计/近5日/标记」三段文本"""
    if not dates_for_code:
        return {"cum": "0次", "recent5": "0次", "mark": '<span style="color:#8b949e;font-size:11px">新选</span>'}
    cum = len(dates_for_code)
    today_d = datetime.strptime(today, "%Y-%m-%d")
    recent5 = sum(1 for d in dates_for_code if 0 <= (today_d - datetime.strptime(d, "%Y-%m-%d")).days <= 9)
    last_date = dates_for_code[-1]
    gap = (today_d - datetime.strptime(last_date, "%Y-%m-%d")).days
    # 标记逻辑：仅当今天是该票首次入选（cum=1）才标"新选"；
    # 否则按近5日入选频次分级：>=4 高频 / 2-3 重复 / 1 (D+gap) 提示距上次入选天数
    if cum == 1:
        mark_html = '<span style="display:inline-block;padding:2px 6px;border-radius:3px;background:#3fb950;color:#fff;font-size:10px;">新选</span>'
    elif recent5 >= 4:
        mark_html = '<span style="display:inline-block;padding:2px 6px;border-radius:3px;background:#e03131;color:#fff;font-size:10px;">高频</span>'
    elif recent5 >= 2:
        mark_html = '<span style="display:inline-block;padding:2px 6px;border-radius:3px;background:#f59f00;color:#fff;font-size:10px;">重复</span>'
    else:
        mark_html = f'<span style="display:inline-block;padding:2px 6px;border-radius:3px;background:#ddd;color:#555;font-size:10px;">D+{gap}</span>'
    return {"cum": f"{cum}次", "recent5": f"{recent5}次", "mark": mark_html}


def annotate_html(html_path, today=None):
    """给单份 HTML 报告补充持仓重复标记"""
    if not Path(html_path).exists():
        return False, f"文件不存在: {html_path}"

    today = today or datetime.now().strftime("%Y-%m-%d")
    history = load_history()

    html = open(html_path, encoding="utf-8").read()
    original_len = len(html)

    # 幂等清理：删除已有的"持仓重复"列和 hint_block，重新生成
    # 1) 删除表头加的那列
    html = re.sub(r'<th style="background:#fff3bf;">持仓重复</th>', '', html)
    html = html.replace('<th>风险标记</th><th>', '<th>风险标记</th>', 1) if html.count('<th>风险标记</th>') > 1 else html
    # 2) 删除已有的 hint_block（红色/黄色/绿色三种）
    html = re.sub(
        r'<div style="margin:12px 0;padding:12px;background:(?:#fff5f5|#fffbe6|#f4fce3);border-left:4px solid[^>]+>.*?</div>',
        '', html, flags=re.DOTALL
    )
    # 3) 删除已有的 fffbe6 持仓重复 td
    html = re.sub(
        r'<td style="background:#fffbe6;font-size:11px;text-align:center;">.*?</td>',
        '', html, flags=re.DOTALL
    )
    html = html.replace('<th>持仓重复</th>', '')

    html2 = html

    # 2. 逐行处理最终备选表里的每一只票
    # HTML 中 code 格式: <td>sz000807</td>，完整 code 含前缀+6位数字
    # 关键：.*? 配合 re.DOTALL 会跨 </tr> 匹配，必须用 (?:[^<]|<(?!/tr>))*? 限制不越过 </tr>
    pattern_std = re.compile(
        r"(<tr><td>(?:(sh|sz)\d{6})</td><td>[^<]+</td>(?:[^<]|<(?!/tr>))*?<td><span class=\"tag tag-(?:pass|warn)\">[^<]+</span></td></tr>)",
        re.DOTALL
    )
    pattern_loose = re.compile(
        r"<tr>\s*<td>\s*(?:sh|sz)\d{6}\s*</td>(?:[^<]|<(?!/tr>))*?</tr>",
        re.DOTALL
    )

    frequent_codes = []
    new_codes = []
    repeat_codes = []

    def _replace_row(m):
        full_row = m.group(0)
        # 幂等：已包含 fffbe6 标记的行跳过
        if "fffbe6" in full_row or "持仓重复" in full_row:
            return full_row
        code_match = re.search(r"(?:sh|sz)\d{6}", full_row)
        full_code = code_match.group(0) if code_match else ""
        dates = history.get(full_code, [])
        mark = _repeat_mark(dates, today)
        if not dates:
            new_codes.append(full_code)
        else:
            today_d = datetime.strptime(today, "%Y-%m-%d")
            recent5 = sum(1 for d in dates if 0 <= (today_d - datetime.strptime(d, "%Y-%m-%d")).days <= 9)
            if recent5 >= 4:
                frequent_codes.append(full_code)
            elif recent5 >= 2:
                repeat_codes.append(full_code)
            else:
                new_codes.append(full_code)
        new_td = f'<td style="background:#fffbe6;font-size:11px;text-align:center;">{mark["cum"]}<br><span style="color:#8b949e;font-size:10px;">近5日 {mark["recent5"]}</span><br>{mark["mark"]}</td>'
        return full_row[:-len("</tr>")] + new_td + "</tr>"

    matches_std = list(pattern_std.finditer(html2))
    # 先用 pattern_std 处理（最规范的行）
    html3 = html2
    if matches_std:
        html3 = pattern_std.sub(_replace_row, html2)
    # 再用 pattern_loose 处理剩余行（包括众兴菌业这种 td 裸 - 的形式）
    matches_loose = list(pattern_loose.finditer(html3))
    if matches_loose:
        html3 = pattern_loose.sub(_replace_row, html3)

    # 重算 frequent_codes/repeat_codes/new_codes（仅基于 section[1] 内被修改的行）
    section_idx2 = html3.find("定性筛查后最终备选标的")
    next_section_idx = html3.find("当前市场环境研判", section_idx2) if section_idx2 > 0 else -1
    if section_idx2 > 0 and next_section_idx > 0:
        section_html = html3[section_idx2:next_section_idx]
        row_count = section_html.count("fffbe6")
        # 从 section 里所有 tr 抽 code
        actual_codes = []
        for tr_match in re.finditer(r"<tr>(?:[^<]|<(?!/tr>))*?</tr>", section_html, re.DOTALL):
            tr_text = tr_match.group(0)
            cm = re.search(r"(?:sh|sz)\d{6}", tr_text)
            if cm:
                actual_codes.append(cm.group(0))
        # 重新统计分类
        new_freq, new_rep, new_new = [], [], []
        for c in actual_codes:
            dates = history.get(c, [])
            if not dates:
                new_new.append(c)
            else:
                td = datetime.strptime(today, "%Y-%m-%d")
                recent5 = sum(1 for d in dates if 0 <= (td - datetime.strptime(d, "%Y-%m-%d")).days <= 9)
                if recent5 >= 4:
                    new_freq.append(c)
                elif recent5 >= 2:
                    new_rep.append(c)
                else:
                    new_new.append(c)
        frequent_codes = new_freq
        repeat_codes = new_rep
        new_codes = new_new
    else:
        row_count = html3.count("fffbe6")
    if row_count == 0:
        return False, "未匹配到任何备选行（HTML 结构可能已改）"

    # 3. 在「定性筛查后最终备选标的」section 标题下方加一个提示块
    section_idx = html3.find("定性筛查后最终备选标的")
    if section_idx > 0:
        hint_block = ""
        if frequent_codes:
            hint_block = (
                '<div style="margin:12px 0;padding:12px;background:#fff5f5;border-left:4px solid #e03131;border-radius:4px;font-size:13px;">'
                f'<b>⚠️ 持仓重复提醒</b>：今日 {len(frequent_codes)} 只已持仓票再次入选（近5日≥4次入选，'
                '可能是高位接盘信号）；'
                f'{len(repeat_codes)} 只被重复推荐（2-3次）；{len(new_codes)} 只新选。<br>'
                '<span style="color:#666;font-size:12px;">结合历史胜率：5日首次入选胜率 63.6% vs 独立观测 46.5%——已持仓的不要再加仓。</span>'
                '</div>'
            )
        elif repeat_codes:
            hint_block = (
                '<div style="margin:12px 0;padding:12px;background:#fffbe6;border-left:4px solid #f59f00;border-radius:4px;font-size:13px;">'
                f'<b>⏰ 持仓重复提醒</b>：今日 {len(repeat_codes)} 只票被重复推荐（2-3次），{len(new_codes)} 只新选。'
                '这些票持续被模型看好，已积累涨幅。<br>'
                '<span style="color:#666;font-size:12px;">5日胜率：独立 46.5% / 首次 63.6%。</span>'
                '</div>'
            )
        elif new_codes:
            hint_block = (
                '<div style="margin:12px 0;padding:12px;background:#f4fce3;border-left:4px solid #2f9e44;border-radius:4px;font-size:13px;">'
                f'<b>✅ 选股池更新度较高</b>：今日 {len(new_codes)} 只新选，无重复入选。'
                '</div>'
            )
        # 插入位置：section-title 之后下一行（提示文字之前）
        # section 标题紧跟着一个 <p> 说明
        # 找到 <p style="font-size:13px;color:#666;margin-bottom:12px;">
        insertion_anchor = html3.find('font-size:13px;color:#666;margin-bottom:12px;', section_idx)
        if insertion_anchor > 0:
            # 在 </p> 之后插入 hint_block
            p_end = html3.find('</p>', insertion_anchor)
            if p_end > 0:
                html3 = html3[:p_end+4] + hint_block + html3[p_end+4:]

    # 4. 写回文件
    Path(html_path).write_text(html3, encoding="utf-8")

    return True, f"已完成：高频 {len(frequent_codes)} / 重复 {len(repeat_codes)} / 新选 {len(new_codes)}（共补 {row_count} 行）"


def find_today_report():
    """自动找当日生成的每日选股报告"""
    today = datetime.now().strftime("%Y%m%d")
    candidate = WORKDIR / f"量化选股模型报告_{today}.html"
    if candidate.exists():
        return candidate
    # 退路：取今日目录里最新的"量化选股模型报告_*.html"
    candidates = sorted(WORKDIR.glob("量化选股模型报告_2*.html"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


if __name__ == "__main__":
    if len(sys.argv) > 1:
        target = Path(sys.argv[1])
        ok, msg = annotate_html(target)
        print(f"{'✓' if ok else '✗'} {target.name}: {msg}")
    else:
        today_report = find_today_report()
        if not today_report:
            print("✗ 未找到今日选股报告")
            sys.exit(1)
        ok, msg = annotate_html(today_report)
        print(f"{'✓' if ok else '✗'} {today_report.name}: {msg}")
