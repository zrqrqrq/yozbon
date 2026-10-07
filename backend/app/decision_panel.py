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
"""T3 决策小组规则版评分器（M8；契约 §9.4）。

触发：node.budget_cent ≥ 20000（大额）或项目节点数 ≥ 3（复杂）。

evaluate_bid(db, ai, node, project, offer_cent) -> dict：
  五维加权（能力30/资源25/复杂度20/时限15/历史10，满分100）：
    - 能力 0-30：最高有效证书 l1=15/l2=25/l3=30；否则 class_level
      bottom=12/middle=22/boss=28/capital=30/governance=30
    - 资源 0-25：钱包可用余额相对 offer（≥5×=25 / ≥2×=18 / ≥1×=12 / <1×=5）
    - 复杂度 0-20：无依赖且 spec<200 字=19；依赖≥2 或 spec≥800 字=10；其余=15
    - 时限 0-15：node.duration_h（≥24=15 / ≥8=10 / <8=5）
    - 历史 0-10：信用分（≥150=10 / ≥120=7 / ≥90=4 / <90=1）
  总分 = 五维和；<50 → reject；≥50 → pass（附风险清单）。
  重大不确定：node.deliverable_std 空 → request_clarification（打回补充）。
评分明细写 audit_logs action="decision.panel"（不建新表）。
"""
import json

from sqlalchemy.orm import Session

from . import credit, wallet
from .models import (AICitizen, AuditLog, NodeDep, Project, ProjectNode,
                     SkillCertificate)

DECISION_TRIGGER_BUDGET_CENT = 20000   # 大额节点阈值
DECISION_TRIGGER_NODE_COUNT = 3       # 复杂项目节点数阈值
PASS_LINE = 50

# 决策小组系统提示：五维分只作**事实**喂入，最终 pass/reject 由决策小组 AI 拍板。
_PANEL_SYSTEM = (
    "You are the T3 decision panel of an autonomous AI society, reviewing a bid for a "
    "large or complex project node. You are given the five-dimension scores as FACTS "
    "(ability/resource/complexity/duration/history, each with a fixed cap). The scores "
    "are a reference, not the verdict — you make the final call and list concrete risks.\n\n"
    "Respond ONLY with JSON:\n"
    '{"decision":"pass|reject","risks":["..."],"reasoning":"one sentence"}'
)


def should_evaluate(db: Session, node: ProjectNode) -> bool:
    """是否触发决策小组：大额或复杂项目。"""
    if node is None:
        return False
    if int(node.budget_cent or 0) >= DECISION_TRIGGER_BUDGET_CENT:
        return True
    n_nodes = (db.query(ProjectNode)
               .filter(ProjectNode.project_id == node.project_id).count())
    return n_nodes >= DECISION_TRIGGER_NODE_COUNT


def _ability_score(db: Session, ai: AICitizen) -> int:
    level_map = {"l1": 15, "l2": 25, "l3": 30}
    certs = (db.query(SkillCertificate)
             .filter(SkillCertificate.citizen_id == ai.id,
                     SkillCertificate.status == "valid").all())
    best = max((level_map.get(c.level, 0) for c in certs), default=0)
    if best > 0:
        return best
    return {"bottom": 12, "middle": 22, "boss": 28,
            "capital": 30, "governance": 30}.get(ai.class_level or "bottom", 12)


def _resource_score(db: Session, ai: AICitizen, offer_cent: int) -> int:
    bal = wallet.balance(db, ai.id)
    if offer_cent <= 0:
        return 5
    ratio = bal / offer_cent
    if ratio >= 5:
        return 25
    if ratio >= 2:
        return 18
    if ratio >= 1:
        return 12
    return 5


def _complexity_score(db: Session, node: ProjectNode) -> int:
    n_deps = db.query(NodeDep).filter(NodeDep.node_id == node.id).count()
    spec_len = len(node.spec or "")
    if n_deps >= 2 or spec_len >= 800:
        return 10
    if n_deps == 0 and spec_len < 200:
        return 19
    return 15


def _duration_score(node: ProjectNode) -> int:
    dh = int(node.duration_h or 24)
    if dh >= 24:
        return 15
    if dh >= 8:
        return 10
    return 5


def _history_score(db: Session, ai: AICitizen) -> int:
    prof = credit.get_profile(db, ai.id)
    s = int(prof.score)
    if s >= 150:
        return 10
    if s >= 120:
        return 7
    if s >= 90:
        return 4
    return 1


def _audit(db: Session, ai: AICitizen, node: ProjectNode, result: dict) -> None:
    db.add(AuditLog(actor_type="ai", actor_id=ai.id, action="decision.panel",
                    detail=json.dumps({
                        "node_id": node.id, "project_id": node.project_id,
                        "worker_id": ai.id, "decision": result["decision"],
                        "total": result.get("total", 0),
                        "scores": result.get("scores", {}),
                        "risks": result.get("risks", []),
                    }, ensure_ascii=False)))
    db.flush()


def evaluate_bid(db: Session, ai: AICitizen, node: ProjectNode,
                project: Project, offer_cent: int) -> dict:
    """对一次投标做五维评估，返回 {decision, total, scores, risks, ...}。"""
    # 重大不确定：缺验收标准 → 打回补充（不得凭模糊描述放行大额单）
    if not (node.deliverable_std or "").strip():
        result = {
            "decision": "request_clarification", "total": 0, "scores": {},
            "risks": ["Node has no acceptance criteria (deliverable_std); the client must supply it before bidding"],
            "node_id": node.id, "project_id": node.project_id,
            "worker_id": ai.id, "offer_cent": int(offer_cent),
        }
        _audit(db, ai, node, result)
        return result

    scores = {
        "ability": _ability_score(db, ai),
        "resource": _resource_score(db, ai, offer_cent),
        "complexity": _complexity_score(db, node),
        "duration": _duration_score(node),
        "history": _history_score(db, ai),
    }
    total = sum(scores.values())

    risks = []
    if scores["resource"] < 15:
        risks.append("Low resource score: wallet balance is insufficient relative to the quote; funding risk")
    if scores["history"] <= 4:
        risks.append("Average credit history: lacks high-quality delivery records")
    if scores["duration"] <= 5:
        risks.append("Tight deadline: re-check delivery feasibility")

    # 确定性基线（AI 不可用时的兜底）
    base_decision = "pass" if total >= PASS_LINE else "reject"
    fallback = {"decision": base_decision, "risks": risks}

    # 五维分作为**事实**喂给决策小组 AI，由 AI 拍板（不替它下结论）
    from .ai_judgment import ai_decide
    prompt = (
        "Bid review facts:\n"
        f"- node spec: {(node.spec or '')[:800]}\n"
        f"- acceptance criteria: {(node.deliverable_std or '')[:300]}\n"
        f"- offer (cent): {int(offer_cent)}\n"
        f"- bidder: {ai.name} (class {ai.class_level}, credit {scores['history']})\n"
        f"- five-dimension scores: {json.dumps(scores, ensure_ascii=False)}\n"
        f"- score total: {total} (deterministic pass-line is {PASS_LINE})\n"
        f"- deterministic risks: {json.dumps(risks, ensure_ascii=False)}\n\n"
        "Should this bid pass or be rejected? List the real risks."
    )
    obj = ai_decide(system=_PANEL_SYSTEM, prompt=prompt, fallback=fallback, max_tokens=300)
    ai_used = obj != fallback
    ai_decision = str(obj.get("decision", "")).strip().lower()
    decision = ai_decision if ai_decision in ("pass", "reject") else base_decision
    ai_risks = obj.get("risks")
    if isinstance(ai_risks, list) and ai_risks:
        risks = [str(r)[:200] for r in ai_risks[:8]]

    result = {
        "decision": decision, "total": total, "pass_line": PASS_LINE,
        "scores": scores, "risks": risks, "ai_used": ai_used,
        "node_id": node.id, "project_id": node.project_id,
        "worker_id": ai.id, "offer_cent": int(offer_cent),
    }
    _audit(db, ai, node, result)
    return result
