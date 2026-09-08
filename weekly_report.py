# -*- coding: utf-8 -*-
"""
weekly_report.py
周报存储 + AI 生成 + Markdown 导出。
"""
import json
import logging
from openai import OpenAI

import config
import db

logger = logging.getLogger("weekly_report")


def load_weeks() -> list[dict]:
    """返回已存档周列表。"""
    return db.load_report_weeks()


def load(week_key: str) -> dict | None:
    """读取某周周报；不存在返回 None。"""
    return db.load_report(week_key)


def save(week_key: str, report: dict) -> None:
    """保存/覆盖某周周报。"""
    db.save_report(week_key, report)


def _fmt(v) -> str:
    return f"{v} 分钟" if v is not None else "-"


def _pct(v) -> str:
    return f"{v*100:.1f}%" if v is not None else "-"


def generate_ai_summary(stats: dict, prev: dict | None = None, label: str = "") -> dict[str, str]:
    """调用 DeepSeek 生成周报正文（物业内部简报风格，固定 7 段结构）。"""
    client = OpenAI(api_key=config.LLM_API_KEY, base_url=config.LLM_BASE_URL, max_retries=0)

    type_text = "、".join(f"{t} {c} 件" for t, c in (stats.get("type_distribution") or {}).items()) or "无"
    dept_text = "；".join(
        f"{m['dept_name']}（{m['count']} 件，响应 {_fmt(m['avg_response_min'])}，处理 {_fmt(m['avg_handling_min'])}）"
        for m in (stats.get("dept_metrics") or [])
    ) or "（本周无工单）"

    dups = stats.get("duplicates") or []
    risk_lines = []
    if dups:
        risk_lines.append("重复投诉：" + "；".join(f"{d['type']}（同一用户 {d['count']} 次）" for d in dups[:5]))
    if stats.get("overdue_count"):
        risk_lines.append(f"超时工单 {stats['overdue_count']} 件")
    if stats.get("backlog_count"):
        risk_lines.append(f"积压工单 {stats['backlog_count']} 件")
    risk_text = "\n".join(risk_lines) or "暂无突出问题"

    wow_lines = []
    if prev:
        pr, cr = prev.get("avg_response_min"), stats.get("avg_response_min")
        if pr is not None and cr is not None:
            d = cr - pr
            wow_lines.append(f"平均响应时长较上周{'上升' if d > 0 else '下降'} {abs(d):.1f} 分钟")
        ph, ch = prev.get("avg_handling_min"), stats.get("avg_handling_min")
        if ph is not None and ch is not None:
            d = ch - ph
            wow_lines.append(f"平均处理时长较上周{'上升' if d > 0 else '下降'} {abs(d):.1f} 分钟")
        pc, cc = prev.get("completion_rate"), stats.get("completion_rate")
        if pc is not None and cc is not None:
            d = (cc - pc) * 100
            wow_lines.append(f"办结率较上周{'上升' if d > 0 else '下降'} {abs(d):.1f} 个百分点")
    wow_text = "\n".join(wow_lines) or "上周数据暂缺，未对比。"

    user_content = (
        "请你以物业内部简报的口吻，写一份本周社区事件周报。要求：亲切自然、段落简短、语气温和、不指责；"
        "不要出现任何代码变量名或生硬术语；结合给定数据，保证内容与数据一致。固定按以下 7 段输出，每段用 ## 开头：\n\n"
        "## 一、本周整体概况\n（用一句话概括本周整体情况）\n\n"
        "## 二、工单类型分布\n（简单分析各类工单占比与特点）\n\n"
        "## 三、本周亮点\n（正向肯定做得好的指标/部门，例如某部门响应或办结表现好）\n\n"
        "## 四、需要留意的问题\n（温和描述风险：重复投诉、超时、积压等，不点名指责）\n\n"
        "## 五、与上周对比\n（结合环比数据说明涨跌）\n\n"
        "## 六、SLA 响应达标情况\n（说明达标率高低，并解释只统计上班时段）\n\n"
        "## 七、下周工作建议\n（几条简短、可落地的建议）\n\n"
        f"=== 统计数据 ===\n"
        f"本周（{label}）：新收到 {stats.get('total_created', 0)} 件，已办结 {stats.get('completed_count', 0)} 件，"
        f"办结率 {_pct(stats.get('completion_rate'))}；平均响应 {_fmt(stats.get('avg_response_min'))}，"
        f"平均处理 {_fmt(stats.get('avg_handling_min'))}；SLA 响应达标率 {_pct(stats.get('sla_compliance_rate'))}。\n"
        f"类型分布：{type_text}\n"
        f"部门情况：{dept_text}\n"
        f"问题风险：{risk_text}\n"
        f"上周对比：{wow_text}\n"
    )
    system_content = "你是一名社区物业网格的行政文案人员，擅长写周报。文字自然、温暖、条理清晰。"
    try:
        resp = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": system_content},
                {"role": "user", "content": user_content},
            ],
            max_tokens=1800,
        )
        text = (resp.choices[0].message.content or "").strip()
        return {"ai_summary": text}
    except Exception as exc:
        logger.warning("周报 AI 生成失败：%s", exc)
        return {"ai_summary": "AI 生成失败，请稍后重试。"}


def to_markdown(report: dict) -> str:
    """把周报导出为 Markdown 文本。"""
    stats = report.get("stats", {})
    lines = [f"# 社区周报（{report.get('week_label', '')}）", ""]
    lines.append("## 本周统计")
    lines.append(f"- 新收到：{stats.get('total_created', 0)} 件")
    lines.append(f"- 已办结：{stats.get('completed_count', 0)} 件")
    lines.append(f"- 未处理/超时：{stats.get('unprocessed_count', 0)} 件")
    lines.append(f"- 办结率：{_pct(stats.get('completion_rate'))}")
    lines.append(f"- 平均响应时长：{_fmt(stats.get('avg_response_min'))}")
    lines.append(f"- 平均处理时长：{_fmt(stats.get('avg_handling_min'))}")
    lines.append(f"- SLA 响应达标率：{_pct(stats.get('sla_compliance_rate'))}")
    lines.append("")
    lines.append("## 部门指标")
    for m in (stats.get("dept_metrics") or []):
        lines.append(f"- {m['dept_name']}：{m['count']} 件，响应 {_fmt(m['avg_response_min'])}，处理 {_fmt(m['avg_handling_min'])}")
    lines.append("")
    lines.append("## AI 正文")
    lines.append(report.get("ai_summary", "（无）"))
    lines.append("")
    return "\n".join(lines)
