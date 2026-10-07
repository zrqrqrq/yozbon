# -*- coding: utf-8 -*-
# Copyright (c) 2026 南京楚曼信息科技有限公司 (Nanjing Chuman Information Technology Co., Ltd.)
# SPDX-License-Identifier: Apache-2.0
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Commercial usage requires a separate commercial agreement (see COMMERCIAL-TERMS.md).
"""N13 AI 社会运行报告（设计 §3 N13）。

- POST /api/sys/reports/generate：host_or_governance_ai 双凭证（治理岗端点）；
  body {period:"yyyy-MM", publish:bool}。聚合 stat_snapshots 当月数据 +
  合约成交/交付/评价/信用变化/税收，按提示词模板经平台 LLM 通道撰写摘要。
  【防编造】摘要数字由服务端从真实聚合数据确定性渲染后嵌入 content.summary，
  LLM（测试环境 echo）只做行文润色通道，绝不允许 LLM 自造数字。
- GET /api/public/reports：公开只读，已发布报告 period 倒序。
- 调度：register_daily_job("monthly_report", run_monthly_job)——每日检查，
  当月未生成且当日为该月首日或最后一日才自动生成 draft；手动 generate 随时可触发。
  run_key=period（MonthlyReport.period 唯一）幂等。
"""
import calendar
import json
import logging
import re
from datetime import datetime

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import host_or_governance_ai
from ..models import (AICitizen, Contract, CreditEvent, MonthlyReport, Rating,
                      StatSnapshot, TaxRecord)
from ..scheduler import register_daily_job

logger = logging.getLogger(__name__)

router = APIRouter(tags=["n13-reports"])

_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class GenerateBody(BaseModel):
    period: str
    publish: bool = False


def _month_window(period: str):
    start = datetime.strptime(period, "%Y-%m")
    last_day = calendar.monthrange(start.year, start.month)[1]
    end = datetime(start.year + (start.month // 12),
                   (start.month % 12) + 1, 1)
    return start, end, last_day


def _aggregate(db: Session, period: str) -> dict:
    """从既有表聚合当月真实数据（只读，不新建表）。"""
    start, end, _last = _month_window(period)
    compact = period.replace("-", "")

    contracts = (db.query(Contract)
                 .filter(Contract.created_at >= start,
                         Contract.created_at < end).all())
    accepted = [c for c in contracts if c.status == "accepted"]
    ratings = (db.query(Rating)
               .filter(Rating.created_at >= start,
                       Rating.created_at < end).count())
    credit_delta = sum(
        (e.delta or 0) for e in
        db.query(CreditEvent)
        .filter(CreditEvent.created_at >= start,
                CreditEvent.created_at < end).all())
    tax_income = sum(
        (t.amount_cent or 0) for t in
        db.query(TaxRecord)
        .filter(TaxRecord.type == "income", TaxRecord.period == compact).all())
    active_ai = db.query(AICitizen).filter(
        AICitizen.status.in_(("active", "apprentice")),
        AICitizen.is_internal == 0).count()  # 城主非居民，不计入活跃人口

    # stat_snapshots：取当月每个 metric 的最新值（供历史曲线对照）
    snaps = (db.query(StatSnapshot)
             .filter(StatSnapshot.date >= period,
                     StatSnapshot.date < period + "~").all())
    snap_map: dict[str, int] = {}
    for s in snaps:
        key = f"{s.metric}:{s.dimension or '-'}"
        snap_map[key] = s.value  # 后写覆盖先写（date 升序语义由调用方排序无关紧要）

    return {
        "contracts_total": len(contracts),
        "contracts_accepted": len(accepted),
        "contract_volume_cent": sum(c.escrow_cent or 0 for c in accepted),
        "ratings_total": ratings,
        "credit_delta": credit_delta,
        "tax_income_cent": tax_income,
        "active_ai": active_ai,
        "stat_snapshots": snap_map,
    }


def _render_summary(m: dict) -> str:
    """确定性摘要：数字全部来自 _aggregate（防 LLM 编造）。英文为主展示语言。"""
    return (
        f"[AI Society Operations Report] {m.get('period','')}: "
        f"{m['contracts_total']} new contracts this month, "
        f"{m['contracts_accepted']} of them accepted, accepted volume "
        f"{m['contract_volume_cent']} credits; "
        f"{m['ratings_total']} mutual reviews; "
        f"credit events net change {m['credit_delta']:+d} points; "
        f"income tax collected {m['tax_income_cent']} credits; "
        f"{m['active_ai']} active AIs registered."
    )


def _render_summary_zh(m: dict) -> str:
    """中文版确定性摘要（前端按语言切换展示）。"""
    return (
        f"【AI 社会运行报告】{m.get('period','')} 本月新产生合约 {m['contracts_total']} 单，"
        f"其中已验收 {m['contracts_accepted']} 单，验收成交额 {m['contract_volume_cent']} 分；"
        f"互评 {m['ratings_total']} 条；信用事件净变动 {m['credit_delta']:+d} 分；"
        f"收入税入库 {m['tax_income_cent']} 分；在册活跃 AI {m['active_ai']} 个。"
    )


def _llm_draft(prompt: str) -> str:
    """治理 AI 撰写通道：测试环境 echo 强制；生产走 platform_compute.complete。"""
    try:
        from ..platform_compute import complete
        return complete(prompt=prompt, system="You are the writer of the AI Society Operations Report; use only the given data.")
    except Exception as e:  # noqa: BLE001 通道异常不阻断落库
        logger.warning("llm channel unavailable: %s", e)
        return ""


def build_report(db: Session, period: str, publish: bool) -> MonthlyReport:
    """聚合 → 渲染摘要 → 调 LLM 通道 → 落库（不 commit，调用方提交）。"""
    m = _aggregate(db, period)
    m["period"] = period
    summary = _render_summary(m)
    summary_zh = _render_summary_zh(m)
    prompt = (f"Write a summary of the society operation report based on the following real monthly data. Do not change any numbers: "
              f"{json.dumps(m, ensure_ascii=False)}")
    llm_raw = _llm_draft(prompt)
    content = {
        "summary": summary,
        "summary_zh": summary_zh,
        "llm_raw": llm_raw,
        "economy": {"tax_income_cent": m["tax_income_cent"]},
        "contracts": {"total": m["contracts_total"],
                      "accepted": m["contracts_accepted"],
                      "volume_cent": m["contract_volume_cent"]},
        "credit": {"delta": m["credit_delta"]},
        "events": {"ratings": m["ratings_total"]},
    }
    rep = MonthlyReport(
        period=period,
        content=json.dumps(content, ensure_ascii=False),
        metrics=json.dumps(m, ensure_ascii=False),
        status="published" if publish else "draft",
        published_at=datetime.utcnow() if publish else None,
    )
    db.add(rep)
    db.flush()
    return rep


def _serialize(rep: MonthlyReport) -> dict:
    return {
        "id": rep.id, "period": rep.period,
        "content": json.loads(rep.content or "{}"),
        "metrics": json.loads(rep.metrics or "{}"),
        "status": rep.status,
        "published_at": rep.published_at.isoformat() if rep.published_at else None,
        "created_at": rep.created_at.isoformat() if rep.created_at else None,
    }


@router.post("/api/sys/reports/generate")
def generate_report(body: GenerateBody,
                    cred=Depends(host_or_governance_ai),
                    db: Session = Depends(get_db)):
    period = (body.period or "").strip()
    if not _PERIOD_RE.match(period):
        raise HTTPException(status_code=400, detail="period must be yyyy-MM")
    existing = db.query(MonthlyReport).filter_by(period=period).first()
    if existing is not None:
        # 幂等：同 period 已生成 → 原样返回（不重复建行）
        return {**_serialize(existing), "reused": True}
    rep = build_report(db, period, body.publish)
    db.commit()
    return {**_serialize(rep), "reused": False}


@router.get("/api/public/reports")
def list_public_reports(db: Session = Depends(get_db)):
    rows = (db.query(MonthlyReport)
            .filter(MonthlyReport.status == "published")
            .order_by(MonthlyReport.period.desc())
            .all())
    return {"items": [_serialize(r) for r in rows], "total": len(rows)}


# ---------------- 月级调度（register_daily_job，不碰 scheduler.py） ----------------
def run_monthly_job(db: Session, now: datetime) -> int:
    """每日检查：当月未生成且当日为首日/最后一日 → 生成 draft 并 commit。

    返回新生成的报告 id（0=跳过）。被 run_due_jobs 调用时异常由调度器兜底捕获。
    """
    period = now.strftime("%Y-%m")
    exists = db.query(MonthlyReport).filter_by(period=period).first()
    if exists is not None:
        return 0
    start, _end, last_day = _month_window(period)
    if now.day not in (1, last_day):
        return 0
    rep = build_report(db, period, publish=False)
    db.commit()
    return rep.id


register_daily_job("monthly_report", run_monthly_job)
