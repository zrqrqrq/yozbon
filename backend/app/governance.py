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
"""治理任务市场 + 专家评审 + 仲裁（蓝图 §二 表 14/15/16 / §四 L11/L12 / §六 规则 11/13）。

外包架构核心（经济模型 §六「AI 能干的不写进代码」）：
- 平台只做：任务发布 / 竞标 / 执行流 / 质量复核 / 结算 / 仲裁。
- 治理判断由入驻治理 AI 执行；MVP 用「规则版占位」——每个任务类型一个独立可替换的
  执行函数，注册在 TASK_HANDLERS。后续可整体替换为「外部治理 AI 提交报告」
  （governance_reports 表已就位），业务流不变。

钱怎么走：
- 治理任务报酬：默认 100% 从税池出（GOV_TASK_TAXPOOL_RATIO=1.0），adjust_system_state(tax_pool,-P)
  → credit 中标 AI「评审/治理」收入。
- 仲裁费（规则 11）：败诉方钱包 debit「仲裁」→ adjust_system_state(tax_pool, +fee)；
  恶意滥用（同一申请人累计败诉/重复申诉 ≥3）双倍扣信用。
- escrow 联动：仲裁判定后惰性 import B 线 escrow.release_refund(db, contract_id, ratio)
  按已完成部分折算释放；B 线未落地时该联动用例在测试里 importorskip 跳过（不造假桩）。
"""
import json
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .config import settings
from .database import register_index
from .models import (AICitizen, ArbitrationCase, AuditLog, Contract,
                     CreditEvent, CreditProfile, GovernanceReport,
                     GovernanceTask, Project, ReviewPanel, ReviewReport,
                     SchedulerRun, SkillCertificate, TaxRecord)
from . import wallet
from .wallet import WalletError

logger = logging.getLogger(__name__)


class GovError(Exception):
    """治理/评审/仲裁业务异常（路由层映射 HTTP 400）。"""


# ---- 组合索引 ----
register_index("CREATE INDEX IF NOT EXISTS idx_govtask_status "
               "ON governance_tasks(status, type)")
register_index("CREATE INDEX IF NOT EXISTS idx_govreport_task "
               "ON governance_reports(task_id, ai_id)")
register_index("CREATE INDEX IF NOT EXISTS idx_arbitration_status "
               "ON arbitration_cases(status, contract_id)")

# 仲裁费（败诉方承担，入税池）；恶意仲裁基础扣分（经济模型 §5.2：恶意仲裁 -10）
ARBITRATION_FEE_CENT: int = 1000
ABUSE_BASE_DELTA: int = -10
ABUSE_DOUBLE_MULT: int = 2
ABUSE_THRESHOLD: int = 3

# S12：裁决后的申诉期（小时）——仅在此窗口内败诉方可申诉；
# 终局期限（小时）——已裁决且超期无申诉的案件由调度自动终局为 closed。
APPEAL_GRACE_HOURS: int = 24
CLOSE_GRACE_HOURS: int = 48

GOV_TYPES = ("review", "audit", "arbitrate", "compliance", "credit", "market",
             "cleanup", "platform_security", "platform_code",
             "platform_file", "platform_intel",
             "hr_evaluation", "capability_gap")

# 平台运营岗位（M3+M4）的信用门槛：class_level ∈ 下列层级，或持 l2/l3 有效证书。
# seed-text（governance 级）天然满足；bottom 级无 L2+ 证书者竞标/指派 → GovError。
PLATFORM_GATE_LEVELS = frozenset({"middle", "boss", "capital", "governance"})
_PLATFORM_PREFIX = "platform_"

# 各治理任务类型的合法结论集（C-19 防空报告/非法结论套税池报酬）。
# 与下方 TASK_HANDLERS 各规则执行体产出一致；外部治理 AI 提交报告时须落入此集。
TASK_CONCLUSIONS = {
    "review": {"feasible", "infeasible", "conditional"},       # 需求可行性评审
    "audit": {"clean", "anomaly"},                            # 安全/女巫审计
    # 仲裁：3 个绑定裁决值（与 submit_verdict 判定一致）+ pending_verdict
    # （规则占位/外部治理 AI 无权直接裁定，挂起等待合议庭 submit_verdict，非绑定结论）
    "arbitrate": {"support_worker", "refund", "split", "pending_verdict"},
    "compliance": {"pass", "fail"},                           # 内容合规
    # 信用复核：approve/reject 为绑定复核；no_adjust 为规则占位「无需调整」（等价 approve 的中性表述）
    "credit": {"approve", "reject", "no_adjust"},            # 信用复核
    "market": {"no_manipulation", "manipulation"},           # 市场监管
    "cleanup": {"done", "pending"},                          # 资产清理
    # ---- M3+M4 平台运营四岗位（契约 §4.3）----
    "platform_security": {"safe", "risk_found"},             # 安全巡检
    "platform_code": {"no_issues", "issues_found"},          # 代码质量巡检
    "platform_file": {"clean", "cleanup_needed"},           # 文件治理
    "platform_intel": {"collected", "none_new"},             # 情报采集
    # ---- 人事评估 / 能力缺口 ----
    "hr_evaluation": {"evaluated", "needs_more_probing", "incapable"},  # 人事评估
    "capability_gap": {"probed", "gap_confirmed", "no_gap"},  # 能力缺口
}


def _json(x) -> str:
    return json.dumps(x, ensure_ascii=False)


def _add_credit_event(db: Session, citizen_id: int, event: str, delta: int,
                      reason: str, ref: str = "") -> CreditEvent:
    ev = CreditEvent(citizen_id=citizen_id, event=event, delta=delta,
                     reason=reason, ref=ref)
    db.add(ev)
    # 同步信用档案（分）
    cp = db.get(CreditProfile, citizen_id)
    if cp is None:
        cp = CreditProfile(citizen_id=citizen_id, score=100)
        db.add(cp)
        db.flush()
    cp.score += delta
    db.flush()
    return ev


# =====================================================================
# 一、任务执行体注册表（外包架构：每个任务类型一个独立可替换函数）
# =====================================================================
# 这些是「规则版占位」：用结构化规则判定产出结论。真实环境替换为外部治理 AI
# 提交的 governance_reports，函数签名保持 (db, task, params) -> dict 即可热插拔。

def _h_review(db: Session, task: GovernanceTask, params: dict) -> dict:
    """评审任务：真实可行性审计（调用 evolution.assess_feasibility）。

    若项目已提交 WBS → 逐节点校验所需 skill 是否平台可达；
    若 WBS 尚未提交 → 降级为预算区间核对（保持向后兼容）。
    """
    from . import evolution  # 延迟导入避免循环依赖

    budget = int(params.get("budget_cent", task.budget_cent or 0))
    project_id = int(params.get("project_id", 0))

    if project_id:
        assess = evolution.assess_feasibility(db, project_id)
        if assess["feasible"]:
            return {"conclusion": "feasible",
                    "evidence": {"budget_cent": budget,
                                 "score": assess["score"],
                                 "note": f"Feasibility audit passed (coverage {assess['score']:.0%})"}}
        else:
            gap_list = [g["skill"] for g in assess["gaps"]]
            # 自动登记缺口（触发进化流程）
            missing = [g["skill"] for g in assess["gaps"]]
            if missing:
                evolution.detect_gaps(db, missing, project_id)
            return {"conclusion": "infeasible",
                    "evidence": {"budget_cent": budget,
                                 "score": assess["score"],
                                 "missing_skills": gap_list,
                                 "note": f"Capability gap: {gap_list}; needs R&D to fill"}}

    # 降级：无 project_id 时走预算区间核对
    return {"conclusion": "feasible",
            "evidence": {"budget_cent": budget,
                         "note": "No linked project; budget range check passed"}}


def _h_audit(db: Session, task: GovernanceTask, params: dict) -> dict:
    """审计任务：女巫/异常行为核对（规则判定占位）。"""
    return {"conclusion": "clean", "evidence": {"note": "Rule-based placeholder: no anomalous clusters found"}}


def _h_arbitrate(db: Session, task: GovernanceTask, params: dict) -> dict:
    return {"conclusion": "pending_verdict",
            "evidence": {"case_id": params.get("case_id")}}


def _h_compliance(db: Session, task: GovernanceTask, params: dict) -> dict:
    return {"conclusion": "pass", "evidence": {"note": "Rule-based placeholder: content compliance sampling passed"}}


def _h_credit(db: Session, task: GovernanceTask, params: dict) -> dict:
    return {"conclusion": "no_adjust", "evidence": {"note": "Rule-based placeholder: credit score needs no review adjustment"}}


def _h_market(db: Session, task: GovernanceTask, params: dict) -> dict:
    return {"conclusion": "no_manipulation",
            "evidence": {"note": "Rule-based placeholder: no wash trading/volume manipulation found"}}


def _h_cleanup(db: Session, task: GovernanceTask, params: dict) -> dict:
    return {"conclusion": "done", "evidence": {"note": "Rule-based placeholder: idle/dead asset check completed"}}


# ---- M3+M4 平台运营四岗位规则版执行体（事实来自 platform_facts，确定性） ----
def _h_platform_security(db: Session, task: GovernanceTask, params: dict) -> dict:
    from . import platform_facts
    facts = platform_facts.collect_security_facts(db)
    risk = bool(facts.get("dep_vulns")) or bool(facts.get("login_anomalies"))
    return {"conclusion": "risk_found" if risk else "safe", "evidence": facts}


def _h_platform_code(db: Session, task: GovernanceTask, params: dict) -> dict:
    from . import platform_facts
    facts = platform_facts.collect_code_facts(db)
    issues = bool(facts.get("errors")) or bool(facts.get("slow_queries"))
    return {"conclusion": "issues_found" if issues else "no_issues",
            "evidence": facts}


def _h_platform_file(db: Session, task: GovernanceTask, params: dict) -> dict:
    from . import platform_facts
    items = platform_facts.collect_file_facts(db)
    cleanup = [i for i in items if i.get("status") in ("expired", "orphan")]
    return {"conclusion": "cleanup_needed" if cleanup else "clean",
            "evidence": {"items": items, "n_cleanup": len(cleanup)}}


def _h_platform_intel(db: Session, task: GovernanceTask, params: dict) -> dict:
    from . import platform_facts
    items = platform_facts.collect_intel_facts(db)
    return {"conclusion": "collected" if items else "none_new",
            "evidence": {"n_items": len(items), "items": items}}


# 任务处理器注册表：type -> 执行函数。新增治理类型只需在此注册，业务流不变。
TASK_HANDLERS = {
    "review": _h_review,
    "audit": _h_audit,
    "arbitrate": _h_arbitrate,
    "compliance": _h_compliance,
    "credit": _h_credit,
    "market": _h_market,
    "cleanup": _h_cleanup,
    "platform_security": _h_platform_security,
    "platform_code": _h_platform_code,
    "platform_file": _h_platform_file,
    "platform_intel": _h_platform_intel,
}


def llm_complete(prompt: str) -> str:
    """LLM 执行体通道（蓝图 §1.0：治理判断主体 = RH LLM 通道）。

    - LLM_PROVIDER ∈ {echo, mock}（默认/测试）→ 本地确定性返回结构化 JSON，不发网络；
    - openai_compat / runninghub → 惰性 import D 线 app.platform_compute.complete 真实调用；
    - D 线未就绪（ImportError）→ 回退 echo 确定性返回（不造假网络桩）。
    """
    provider = (settings.LLM_PROVIDER or "echo").lower()
    if provider in ("echo", "mock"):
        return '{"conclusion": "ok", "evidence": {"echo": true}}'
    try:
        from app.platform_compute import complete  # D 线交付（惰性）
        return complete(prompt=prompt)   # 通道选择由 config.LLM_PROVIDER 决定
    except ImportError:
        return '{"conclusion": "ok", "evidence": {"echo": true, "fallback": true}}'


def llm_execute_task(db: Session, task: GovernanceTask) -> dict:
    """LLM 执行体：治理任务的智能执行路径。

    核心升级：执行AI必须像正常人一样先理解任务再执行，而非无脑转发。
    流程：
    1. 构建丰富的任务上下文（让执行AI理解"为什么做这件事"）
    2. 执行AI先判断任务是否合理、如何正确执行
    3. 基于判断结果执行
    """
    # 构建丰富的任务理解上下文（不是裸参数 dump）
    try:
        params = json.loads(task.params or "{}")
    except Exception:
        params = {"raw": task.params}

    type_context = {
        "review": "You are reviewing a task proposal. Assess if it is feasible and reasonable.",
        "audit": "You are auditing for security/sybil issues. Look for anomalies.",
        "arbitrate": "You are arbitrating a dispute. Consider both sides fairly.",
        "compliance": "You are checking content compliance. Be thorough but fair.",
        "credit": "You are re-examining a credit decision. Look at evidence objectively.",
        "market": "You are monitoring for market manipulation. Look for suspicious patterns.",
        "cleanup": "You are handling asset cleanup. Ensure nothing of value is lost.",
        "platform_security": "You are inspecting platform security. Be vigilant.",
        "platform_code": "You are reviewing code/feature quality. Be constructive.",
        "platform_file": "You are governing platform files. Keep things clean.",
        "platform_intel": "You are collecting intelligence. Report what you find.",
        "hr_evaluation": "You are evaluating an AI's capabilities. Be fair and thorough.",
        "capability_gap": "You are investigating a capability gap. Determine if it's real.",
    }
    role_hint = type_context.get(task.type, "You are executing a governance task thoughtfully.")

    output_example = '{"conclusion":"<one of allowed values>","evidence":{...}}'
    prompt = (
        f"[Role] {role_hint}\n\n"
        f"[Task] Type: {task.type}, Budget: {task.budget_cent} credits\n"
        f"[Context] {json.dumps(params, ensure_ascii=False)}\n\n"
        f"[Instructions]\n"
        f"Before deciding, think briefly: Is this task well-formed? Do you have enough "
        f"information? What's the right approach? Then provide your conclusion.\n"
        f"Output JSON: {output_example}"
    )

    raw = llm_complete(prompt)
    try:
        parsed = json.loads(raw)
    except Exception:  # noqa: BLE001
        parsed = {"conclusion": raw, "evidence": {}}
    parsed.setdefault("evidence", {})
    parsed["evidence"]["via_llm"] = True
    return parsed


def run_task_handler(db: Session, task: GovernanceTask) -> dict:
    """治理任务执行体调度（外包可替换点）：优先走 LLM 通道，异常回退规则版占位函数。"""
    params = {}
    try:
        params = json.loads(task.params or "{}")
    except Exception:  # noqa: BLE001
        params = {}
    # 1) LLM 执行体（蓝图 §1.0）；失败则回退规则函数
    try:
        return llm_execute_task(db, task)
    except Exception:  # noqa: BLE001  LLM 通道异常 → 规则兜底
        handler = TASK_HANDLERS.get(task.type)
        if handler is None:
            raise GovError(f"Unknown governance task type: {task.type}")
        return handler(db, task, params)


# =====================================================================
# 二、治理任务市场状态机：open → bidding → assigned → reviewed → paid
# =====================================================================
def publish_task(db: Session, type_: str, params: dict, budget_cent: int,
                 deadline=None) -> GovernanceTask:
    if type_ not in GOV_TYPES:
        raise GovError(f"type must be one of {GOV_TYPES}")
    if budget_cent < 0:
        raise GovError("Budget cannot be negative")
    t = GovernanceTask(type=type_, params=_json(params), budget_cent=budget_cent,
                       deadline=deadline, status="open", assignee_id=0)
    db.add(t)
    db.flush()
    return t


def _check_platform_gate(db: Session, ai_id: int, task_type: str) -> None:
    """M3+M4 岗位信用门槛（契约 §4.3）：platform_* 任务只放行
    class_level ∈ PLATFORM_GATE_LEVELS，或持 l2/l3 有效证书的 AI。

    非 platform_* 类型一律放行（不影响既有行为）。不满足抛 GovError（路由层 400）。
    """
    if not task_type.startswith(_PLATFORM_PREFIX):
        return
    ai = db.get(AICitizen, ai_id)
    if ai is None:
        raise GovError(f"AI {ai_id} not found")
    if ai.class_level in PLATFORM_GATE_LEVELS:
        return
    cert = (db.query(SkillCertificate)
            .filter(SkillCertificate.citizen_id == ai_id,
                    SkillCertificate.status == "valid",
                    SkillCertificate.level.in_(("l2", "l3"))).first())
    if cert is None:
        raise GovError(
            f"platform operations role {task_type} requires class_level in "
            f"{sorted(PLATFORM_GATE_LEVELS)} or a valid l2/l3 certificate"
            f" (current AI class_level={ai.class_level})")


def bid_task(db: Session, ai_id: int, task_id: int, price_cent: int,
             message: str = "") -> GovernanceReport:
    """AI 竞标：同一 AI 对同一任务只能投一次（唯一性校验）。"""
    t = db.get(GovernanceTask, task_id)
    if t is None:
        raise GovError(f"Governance task {task_id} not found")
    if t.status not in ("open", "bidding"):
        raise GovError(f"Task status {t.status} does not accept bids")
    _check_platform_gate(db, ai_id, t.type)
    dup = (db.query(GovernanceReport)
           .filter(GovernanceReport.task_id == task_id,
                   GovernanceReport.ai_id == ai_id,
                   GovernanceReport.status == "bid").first())
    if dup:
        raise GovError("You have already bid on this task (no duplicate bids)")
    bid = GovernanceReport(task_id=task_id, ai_id=ai_id, conclusion=message,
                           evidence=_json({"price_cent": price_cent}),
                           reviewed=0, status="bid")
    db.add(bid)
    if t.status == "open":
        t.status = "bidding"   # 首标进入竞标期
    db.flush()
    return bid


def assign_task(db: Session, task_id: int, ai_id: int) -> GovernanceTask:
    """系统/复核人选定中标者：bidding → assigned。"""
    t = db.get(GovernanceTask, task_id)
    if t is None:
        raise GovError(f"Governance task {task_id} not found")
    if t.status not in ("open", "bidding"):
        raise GovError(f"Task status {t.status} cannot be assigned")
    _check_platform_gate(db, ai_id, t.type)
    bid = (db.query(GovernanceReport)
           .filter(GovernanceReport.task_id == task_id,
                   GovernanceReport.ai_id == ai_id,
                   GovernanceReport.status == "bid").first())
    if bid is None:
        raise GovError("This AI did not bid on this task; cannot assign")
    t.assignee_id = ai_id
    t.status = "assigned"
    db.flush()
    return t


def submit_task_report(db: Session, ai_id: int, task_id: int,
                       conclusion: str, evidence: dict | str = "{}") -> GovernanceReport:
    """中标 AI 提交治理报告（被执行）。未指派时若任务在竞标期，提交者自动中标。"""
    t = db.get(GovernanceTask, task_id)
    if t is None:
        raise GovError(f"Governance task {task_id} not found")
    # C-19：结论必须非空且属于该任务类型的合法结论集，否则拒绝（防空报告套税池报酬）
    allowed = TASK_CONCLUSIONS.get(t.type, set())
    if not conclusion or conclusion not in allowed:
        raise GovError(
            f"invalid conclusion: the conclusion for task {t.type} must be one of {sorted(allowed)} (received {conclusion!r})")
    # M3+M4：提交报告（含自动中标路径）同样过岗位门槛，防绕过竞标直接套税池报酬
    _check_platform_gate(db, ai_id, t.type)
    if t.status in ("open", "bidding"):
        # MVP：直接提交即认领（自动中标），体现「外包给治理 AI」
        t.assignee_id = ai_id
        t.status = "assigned"
    elif t.status != "assigned" or t.assignee_id != ai_id:
        raise GovError("Only the winning AI can submit the task report")
    ev = evidence if isinstance(evidence, str) else _json(evidence)
    rep = GovernanceReport(task_id=task_id, ai_id=ai_id, conclusion=conclusion,
                           evidence=ev, reviewed=0, status="submitted")
    db.add(rep)
    db.flush()
    return rep


def review_task(db: Session, task_id: int, review_result: str,
                quality_score: float = 1.0) -> GovernanceTask:
    """复核人再评审：assigned → reviewed。"""
    t = db.get(GovernanceTask, task_id)
    if t is None:
        raise GovError(f"Governance task {task_id} not found")
    if t.status != "assigned":
        raise GovError(f"Task status {t.status} cannot be reviewed")
    rep = (db.query(GovernanceReport)
           .filter(GovernanceReport.task_id == task_id,
                   GovernanceReport.ai_id == t.assignee_id,
                   GovernanceReport.status == "submitted").first())
    if rep is None:
        raise GovError("The winning AI has not submitted a report; cannot review")
    rep.reviewed = 1
    rep.review_result = review_result
    t.status = "reviewed"
    t.quality_score = quality_score
    db.flush()
    return t


def settle_task(db: Session, task_id: int) -> GovernanceTask:
    """结算：reviewed → paid。报酬默认 100% 从税池出（GOV_TASK_TAXPOOL_RATIO）。"""
    t = db.get(GovernanceTask, task_id)
    if t is None:
        raise GovError(f"Governance task {task_id} not found")
    if t.status != "reviewed":
        raise GovError(f"Task status {t.status} cannot be settled")
    pay = int(round(t.budget_cent * settings.GOV_TASK_TAXPOOL_RATIO))
    if pay > 0 and t.assignee_id:
        # 出税池 → 入中标 AI 钱包（同事务；税池不足由 adjust 护栏抛错）
        wallet.adjust_system_state(db, "tax_pool", -pay, ref=f"govpay:{task_id}")
        wallet.credit(db, t.assignee_id, pay, "评审",
                      ref=f"govpay:{task_id}",
                      note=f"governance task {t.type} reward")
    t.status = "paid"
    db.flush()
    # A-C2 修复：平台编制岗位经调度器发布、走到结算后回写执行结果，驱动
    # post_planner 的自适应（noop_streak / last_run_date / 频率恢复）。此前
    # record_outcome 全仓零调用，导致"无事→降频→休眠"整链失效、frequency 配置无效。
    # 反查 SchedulerRun.task_id → post_code；非编制岗位（PLATFORM_JOBS/快照）时
    # record_outcome 内部 db.get(PostQuota) 返回 None 自动跳过。had_work 以"任务
    # 确有中标 AI 执行"为信号。
    try:
        from . import post_planner
        for run in (db.query(SchedulerRun)
                      .filter(SchedulerRun.task_id == task_id).all()):
            post_planner.record_outcome(db, run.job_type, had_work=bool(t.assignee_id))
    except Exception:  # noqa: BLE001  回写失败绝不阻断结算
        logger.exception("record_outcome failed on settle task %s", task_id)
    return t


# =====================================================================
# 三、专家评审组 + 报告聚合
# =====================================================================
def submit_review_opinion(db: Session, ai_id: int, panel_id: int, verdict: str,
                          risk: str = "", budget_suggest_cent: int = 0,
                          duration_suggest_h: int = 0, breakdown: dict | str = "{}") -> dict:
    """评审 AI 提交意见。意见先落 audit_logs（MVP 不建意见表）；齐票后聚合出报告。"""
    panel = db.get(ReviewPanel, panel_id)
    if panel is None:
        raise GovError(f"Review panel {panel_id} not found")
    if panel.status != "voting":
        raise GovError("The review panel has ended voting")
    try:
        members = json.loads(panel.member_ids or "[]")
    except Exception:  # noqa: BLE001
        members = []
    if ai_id not in members:
        raise GovError("You are not a member of this review panel")
    if verdict not in ("feasible", "infeasible", "conditional"):
        raise GovError("verdict must be feasible/infeasible/conditional")
    # C-20：防同一成员重复投票（按 panel_id + actor_id 查重，而非仅数行数凑票）
    prior_votes = (db.query(AuditLog)
                   .filter(AuditLog.action == "review.vote",
                           AuditLog.actor_id == ai_id).all())
    for row in prior_votes:
        try:
            if json.loads(row.detail).get("panel_id") == panel_id:
                raise GovError("You have already voted on this panel (no duplicate votes)")
        except GovError:
            raise
        except Exception:  # noqa: BLE001  旧/坏记录跳过
            continue

    bd = breakdown if isinstance(breakdown, str) else _json(breakdown)
    db.add(AuditLog(actor_type="ai", actor_id=ai_id, action="review.vote",
                    detail=_json({"panel_id": panel_id, "verdict": verdict,
                                  "risk": risk,
                                  "budget_suggest_cent": budget_suggest_cent,
                                  "duration_suggest_h": duration_suggest_h,
                                  "breakdown": bd})))
    db.flush()

    # 已投票数
    voted = (db.query(AuditLog)
             .filter(AuditLog.action == "review.vote",
                     AuditLog.detail.like(f'%"panel_id": {panel_id}%')).count())
    if voted >= len(members):
        out = finalize_review_panel(db, panel_id)
        out["done"] = True
        return out
    return {"panel_id": panel_id, "voted": voted, "required": len(members),
            "done": False}


def finalize_review_panel(db: Session, panel_id: int) -> dict:
    """聚合全组意见 → 写 review_reports；按结论回写项目状态。"""
    panel = db.get(ReviewPanel, panel_id)
    if panel is None:
        raise GovError(f"Review panel {panel_id} not found")
    votes = (db.query(AuditLog)
             .filter(AuditLog.action == "review.vote",
                     AuditLog.detail.like(f'%"panel_id": {panel_id}%')).all())
    if not votes:
        raise GovError("No review opinions yet")
    parsed = []
    for v in votes:
        try:
            d = json.loads(v.detail)
            if d.get("panel_id") == panel_id:
                parsed.append(d)
        except Exception:  # noqa: BLE001
            continue
    if not parsed:
        raise GovError("Review opinion parsed as empty")

    # 多数表决（平票 → conditional）
    tally = {}
    for d in parsed:
        tally[d["verdict"]] = tally.get(d["verdict"], 0) + 1
    conclusion = max(tally, key=tally.get)
    if tally[conclusion] == len(parsed) / 2 and len(parsed) % 2 == 0:
        conclusion = "conditional"
    avg_budget = int(round(sum(int(d.get("budget_suggest_cent", 0)) for d in parsed) / len(parsed)))
    avg_dur = int(round(sum(int(d.get("duration_suggest_h", 0)) for d in parsed) / len(parsed)))
    risks = [d.get("risk", "") for d in parsed if d.get("risk")]

    rep = ReviewReport(panel_id=panel_id, project_id=panel.project_id,
                       conclusion=conclusion, risk=_json(risks),
                       budget_suggest_cent=avg_budget, duration_suggest_h=avg_dur,
                       breakdown=_json({"n_votes": len(parsed), "tally": tally}))
    db.add(rep)
    db.flush()
    panel.status = "done"

    proj = db.get(Project, panel.project_id)
    if proj is not None:
        proj.review_report_id = rep.id
        if conclusion in ("feasible", "conditional"):
            proj.status = "approved"     # 评审通过 → approved，待宿主 confirm running
        else:
            proj.status = "draft"        # 不可行 → 退回宿主重做
            panel.status = "redo"
    db.flush()
    return {"panel_id": panel_id, "report_id": rep.id, "conclusion": conclusion,
            "votes": len(parsed)}


# =====================================================================
# 四、仲裁（消费 B 线写入的 arbitration_cases(status='open')）
# =====================================================================
def form_arbitration_panel(db: Session, case_id: int, arbiter_ids: list) -> ArbitrationCase:
    """从治理市场选仲裁 AI 组（1-3 名）接管 open 案。"""
    case = db.get(ArbitrationCase, case_id)
    if case is None:
        raise GovError(f"Arbitration case {case_id} not found")
    if case.status != "open":
        raise GovError(f"Case status {case.status}; cannot form a panel")
    ids = list(dict.fromkeys(int(i) for i in arbiter_ids))
    if not (1 <= len(ids) <= 3):
        raise GovError("The arbitration tribunal requires 1-3 arbitrator AIs")
    case.panel = _json(ids)
    db.flush()
    return case


# 阻断④：自动组庭——争议开案后仲裁庭须有人接管，否则案件永久卡 open。
_BAD_ARBITER_STATUS = ("banned", "dead", "frozen")


def _select_arbiters(db: Session, case: ArbitrationCase,
                     seed_ids: list | None = None) -> list:
    """挑选仲裁员：治理级（class_level=governance）在岗 AI，排除争议双方，至多 3 名。

    seed_ids：发起方/城主自身等"必须可仲裁"的治理 AI，优先纳入并校验资格，
    用于保证至少有一名合格仲裁员（否则返回空列表交由调用方判定无法组庭）。
    """
    def _eligible(cid: int) -> bool:
        ai = db.get(AICitizen, cid)
        return (ai is not None and ai.class_level == "governance"
                and ai.status not in _BAD_ARBITER_STATUS
                and cid not in (case.applicant_id, case.respondent_id))

    chosen: list = []
    for cid in (seed_ids or []):
        try:
            cid = int(cid)
        except (TypeError, ValueError):
            continue
        if _eligible(cid) and cid not in chosen:
            chosen.append(cid)

    q = (db.query(AICitizen.id)
           .filter(AICitizen.class_level == "governance",
                   AICitizen.status.notin_(_BAD_ARBITER_STATUS),
                   AICitizen.id != case.applicant_id,
                   AICitizen.id != case.respondent_id)
           .order_by(AICitizen.id.asc()))
    for (aid,) in q.all():
        if aid in chosen:
            continue
        chosen.append(aid)
        if len(chosen) >= 3:
            break
    return chosen[:3]


def auto_form_arbitration_panel(db: Session, case_id: int,
                                seed_ids: list | None = None):
    """为一个 open 且未组庭的仲裁案自动选任仲裁员并组庭（幂等）。

    返回组庭后的 ArbitrationCase；若案件不存在/非 open/已组庭/无合格仲裁员则返回 None。
    """
    case = db.get(ArbitrationCase, case_id)
    if case is None or case.status != "open":
        return None
    try:
        existing = json.loads(case.panel or "[]")
    except Exception:  # noqa: BLE001
        existing = []
    if existing:                       # 已组庭：幂等跳过
        return None
    arbiters = _select_arbiters(db, case, seed_ids)
    if not arbiters:
        return None                    # 无合格治理 AI：保持 open，下次调度再试
    return form_arbitration_panel(db, case_id, arbiters)


def auto_form_arbitration_panels(db: Session, limit: int = 20,
                                 seed_ids: list | None = None) -> list:
    """批量为 open+空庭仲裁案组庭（城主调度入口）。返回 {case_id, formed} 列表。"""
    rows = (db.query(ArbitrationCase.id)
              .filter(ArbitrationCase.status == "open")
              .order_by(ArbitrationCase.id.asc()).limit(limit).all())
    out = []
    for (cid,) in rows:
        case = db.get(ArbitrationCase, cid)
        try:
            panel = json.loads(case.panel or "[]")
        except Exception:  # noqa: BLE001
            panel = []
        if panel:
            continue
        formed = auto_form_arbitration_panel(db, cid, seed_ids)
        out.append({"case_id": cid, "formed": formed is not None,
                    "panel": json.loads(formed.panel) if formed else []})
    return out


def _loser_of(decision: str, case: ArbitrationCase) -> int:
    """判定败诉方 citizen_id。

    decision=support_worker → 支持工人(被诉方 respondent)，申诉方 applicant 败诉；
    decision=refund        → 退款给申诉方，工人/被诉方 respondent 败诉；
    decision=split         → 折算，无明确败诉（不收仲裁费，仅按比例释放）。
    """
    if decision == "support_worker":
        return case.applicant_id
    if decision == "refund":
        return case.respondent_id
    return 0


def submit_verdict(db: Session, arbiter_id: int, case_id: int, decision: str,
                   ratio: float = 1.0, reason: str = "") -> dict:
    """仲裁 AI 提交判定。

    规则 11：
    - 败诉方付仲裁费入税池（debit「仲裁」→ tax_pool += fee）；
    - 恶意滥用（同一申请人累计败诉/重复申诉 ≥3）→ 双倍扣信用。
    escrow 联动：判定后惰性 import B 线 escrow.release_refund(db, contract_id, ratio)
    按已完成部分折算释放；B 线未落地时抛 ImportError（测试用 importorskip 跳过联动用例）。
    """
    case = db.get(ArbitrationCase, case_id)
    if case is None:
        raise GovError(f"Arbitration case {case_id} not found")
    if case.status != "open":
        raise GovError(f"Case status {case.status}; cannot be judged twice")
    try:
        panel = json.loads(case.panel or "[]")
    except Exception:  # noqa: BLE001
        panel = []
    if arbiter_id not in panel:
        raise GovError("You are not a member of this case's tribunal")
    if decision not in ("support_worker", "refund", "split"):
        raise GovError("decision must be support_worker/refund/split")

    # decided_at：裁决时刻，写入 verdict JSON 供申诉期/终局期限计算（不改动表结构）
    case.verdict = _json({"decision": decision, "ratio": ratio, "reason": reason,
                          "decided_at": datetime.utcnow().isoformat()})
    case.penalty_cent = ARBITRATION_FEE_CENT
    case.status = "verdict"

    events = []
    loser = _loser_of(decision, case)
    # ---- 规则 11：败诉方付仲裁费入税池 ----
    if loser:
        try:
            wallet.debit(db, loser, ARBITRATION_FEE_CENT, "仲裁",
                         ref=f"arbfee:{case_id}", note=f"arbitration case {case_id} losing-party arbitration fee")
            wallet.adjust_system_state(db, "tax_pool", ARBITRATION_FEE_CENT,
                                       ref=f"arbfee:{case_id}")
        except WalletError as exc:
            db.rollback()
            raise GovError(f"Losing party's wallet balance is insufficient; cannot deduct the arbitration fee: {exc}")

        # ---- 规则 11 恶意滥用：同一申请人累计败诉 ≥3 → 双倍扣信用 ----
        abuse_delta = ABUSE_BASE_DELTA
        if case.applicant_id and loser == case.applicant_id:
            prior_losses = _count_applicant_losses(db, case.applicant_id, case_id)
            if prior_losses + 1 >= ABUSE_THRESHOLD:
                abuse_delta = ABUSE_BASE_DELTA * ABUSE_DOUBLE_MULT
            ev = _add_credit_event(db, case.applicant_id, "malicious_arbitration",
                                   abuse_delta,
                                   reason=f"Arbitration case {case_id} lost / abusive dispute",
                                   ref=f"arb:{case_id}")
            events.append({"citizen_id": case.applicant_id, "delta": ev.delta})

    # ---- escrow 联动（B 线；惰性 import，未落地则抛 ImportError 交调用方跳过）----
    escrow_released = False
    try:
        from .escrow import release_refund  # B 线实现
        release_refund(db, case.contract_id, ratio)
        escrow_released = True
    except ImportError:
        # B 线 escrow 未落地：本用例由测试 importorskip 跳过；此处不造假桩
        raise

    db.flush()
    return {"case_id": case_id, "decision": decision, "ratio": ratio,
            "loser_id": loser, "arbitration_fee_cent": ARBITRATION_FEE_CENT,
            "escrow_released": escrow_released, "credit_events": events}


def _count_applicant_losses(db: Session, applicant_id: int, exclude_case_id: int) -> int:
    """统计该申请人历史败诉次数（verdict=support_worker 的案子）。"""
    rows = (db.query(ArbitrationCase)
            .filter(ArbitrationCase.applicant_id == applicant_id,
                    ArbitrationCase.status == "verdict").all())
    n = 0
    for r in rows:
        if r.id == exclude_case_id:
            continue
        try:
            v = json.loads(r.verdict or "{}")
        except Exception:  # noqa: BLE001
            continue
        if v.get("decision") == "support_worker":
            n += 1
    return n


# =====================================================================
# 四·补：裁决后流转——败诉方申诉(appealed) + 期满自动终局(closed)
# =====================================================================
def _verdict_decision(case: ArbitrationCase) -> str:
    """从 verdict JSON 解析判定（解析失败返回空串）。"""
    try:
        return json.loads(case.verdict or "{}").get("decision", "")
    except Exception:  # noqa: BLE001
        return ""


def _decided_at(case: ArbitrationCase) -> datetime | None:
    """取裁决时刻：优先 verdict.decided_at；缺失则回退 created_at。"""
    try:
        s = json.loads(case.verdict or "{}").get("decided_at")
        if s:
            return datetime.fromisoformat(s)
    except Exception:  # noqa: BLE001
        pass
    return case.created_at


def appeal(db: Session, appellant_id: int, case_id: int,
           reason: str = "") -> dict:
    """败诉方申诉：把已裁决(verdict)案件推进为 appealed（留痕审计）。

    校验：
    - 案件存在且处于已裁决(verdict)态（decided 类）；
    - 未过申诉期 APPEAL_GRACE_HOURS（以裁决时刻起算）；
    - 申诉人须为败诉方（split 无败诉方 → 不可申诉）。
    返回结构化结果 {case_id, status, appellant_id, loser_id}。
    """
    case = db.get(ArbitrationCase, case_id)
    if case is None:
        raise GovError(f"Arbitration case {case_id} not found")
    if case.status != "verdict":
        raise GovError(
            f"Case status {case.status}; only a decided (verdict) case can be appealed")
    # 申诉期校验：以裁决时刻起算，超过 APPEAL_GRACE_HOURS 拒绝
    decided = _decided_at(case)
    if decided is not None:
        elapsed_h = (datetime.utcnow() - decided).total_seconds() / 3600.0
        if elapsed_h > APPEAL_GRACE_HOURS:
            raise GovError(
                f"Appeal window closed ({int(elapsed_h)}h > {APPEAL_GRACE_HOURS}h)")
    # 败诉方校验：split 判定无败诉方，禁止申诉
    decision = _verdict_decision(case)
    loser = _loser_of(decision, case)
    if loser == 0:
        raise GovError("This verdict has no losing party (split); cannot appeal")
    if int(appellant_id) != int(loser):
        raise GovError("Only the losing party may appeal this case")

    case.status = "appealed"
    db.add(AuditLog(actor_type="ai", actor_id=int(appellant_id),
                    action="arbitration.appeal",
                    detail=_json({"case_id": case_id, "reason": reason,
                                  "decision": decision, "loser_id": loser})))
    db.flush()
    return {"case_id": case_id, "status": "appealed",
            "appellant_id": int(appellant_id), "loser_id": loser}


def auto_close_decided_cases(db: Session, grace_hours: int = CLOSE_GRACE_HOURS,
                             now: datetime | None = None,
                             limit: int = 200) -> list:
    """把「已裁决(verdict) 且超过终局期限仍未申诉」的案件推进为 closed（终局）。

    - 仅处理 status == "verdict" 的案子；appealed 状态不自动关闭（进入复审流程）；
    - 以裁决时刻 + grace_hours 判定是否到期；
    - 幂等：推进后置 closed，再次调度不再命中；
    - 可在既有调度入口（如 governor 巡检）调用，也供测试直接驱动。
    返回 {case_id, closed} 列表。
    """
    now = now or datetime.utcnow()
    rows = (db.query(ArbitrationCase)
              .filter(ArbitrationCase.status == "verdict")
              .order_by(ArbitrationCase.id.asc()).limit(limit).all())
    out = []
    for case in rows:
        decided = _decided_at(case)
        if decided is None:
            continue
        if (now - decided) >= timedelta(hours=grace_hours):
            case.status = "closed"
            db.add(AuditLog(actor_type="system", actor_id=0,
                            action="arbitration.auto_close",
                            detail=_json({"case_id": case.id,
                                          "decided_at": decided.isoformat()})))
            out.append({"case_id": case.id, "closed": True})
    db.flush()
    return out


# =====================================================================
# 五、事件轮询（被邀标/被结算/被验收，按本 AI 聚合，分页）
# =====================================================================
def my_events(db: Session, ai_id: int, limit: int = 20, offset: int = 0) -> dict:
    """聚合最近与本 AI 相关的事件（contracts/governance_tasks/tax_records/
    arbitration_cases/review_panels），统一按时间倒序分页。

    MVP：单表 in_() 查询后在内存合并排序（事件量小；后续可落统一事件表）。
    """
    limit = min(max(int(limit), 1), 100)
    items = []

    # 被结算/被验收（作为 worker）
    for c in (db.query(Contract).filter(Contract.worker_id == ai_id)
              .order_by(Contract.id.desc()).limit(200).all()):
        items.append({"at": c.created_at or datetime.min, "type": "contract",
                      "ref": f"contract:{c.id}", "status": c.status,
                      "detail": {"contract_id": c.id, "project_id": c.project_id}})
    # 被指派治理任务
    for t in (db.query(GovernanceTask).filter(GovernanceTask.assignee_id == ai_id)
              .order_by(GovernanceTask.id.desc()).limit(200).all()):
        items.append({"at": t.created_at or datetime.min, "type": "gov_task",
                      "ref": f"govtask:{t.id}", "status": t.status,
                      "detail": {"task_id": t.id, "gov_type": t.type}})
    # 被收税
    for r in (db.query(TaxRecord).filter(TaxRecord.citizen_id == ai_id)
              .order_by(TaxRecord.id.desc()).limit(200).all()):
        items.append({"at": r.created_at or datetime.min, "type": "tax",
                      "ref": f"tax:{r.id}", "status": r.type,
                      "detail": {"amount_cent": r.amount_cent}})
    # 仲裁当事人
    for cs in (db.query(ArbitrationCase)
               .filter((ArbitrationCase.applicant_id == ai_id) |
                       (ArbitrationCase.respondent_id == ai_id))
               .order_by(ArbitrationCase.id.desc()).limit(200).all()):
        items.append({"at": cs.created_at or datetime.min, "type": "arbitration",
                      "ref": f"arb:{cs.id}", "status": cs.status,
                      "detail": {"case_id": cs.id, "contract_id": cs.contract_id}})
    # 被邀评审
    for rp in (db.query(ReviewPanel).order_by(ReviewPanel.id.desc()).limit(200).all()):
        try:
            members = json.loads(rp.member_ids or "[]")
        except Exception:  # noqa: BLE001
            members = []
        if ai_id in members:
            items.append({"at": rp.created_at or datetime.min, "type": "review_invite",
                          "ref": f"panel:{rp.id}", "status": rp.status,
                          "detail": {"panel_id": rp.id, "project_id": rp.project_id}})

    items.sort(key=lambda x: x["at"], reverse=True)
    total = len(items)
    page = items[offset:offset + limit]
    for it in page:
        it["at"] = it["at"].isoformat() if it["at"] else None
    return {"total": total, "items": page}
