# -*- coding: utf-8 -*-
"""城主治理中枢（平台内置治理决策体 / 董事长级管理员）。

核心哲学（用户决策 2026-10-04）：
  "就像人类社会一样，从初期城主啥都要管啥都要忙，到中期逐渐委派，到后期只做决策和签字。
   这些东西很难用代码写死，但可以通过代码和提示词双重手段让城主 AI 自己决定。"

落地为「感知-决策-执行」自治循环，分工不写死、由 LLM 涌现：
  1) 感知 sense  ：每 tick 汇总一份「态势快照」（生态成熟度 / 待办 / 安全 / 经济 / 运营岗位积压）。
  2) 决策 decide ：把态势 + 治理哲学人设喂给 RH LLM（platform_compute.complete），
                  由它自主决定每条待办的处置：自批 / 委派给某 AI / 验收交付 / 大额上报宿主签字 / 不动。
                  —— 自批 vs 委派的比例（委派率）随生态成熟度自然上升，不由代码写死。
  3) 执行 act    ：LLM 输出的动作只是"建议"，必须过**代码硬护栏**才落地，并全部翻译成平台已有的
                  治理任务状态机（governance.publish/assign/submit/review）来执行：
                    · 委派对象必须通过岗位资格门槛（class_level∈PLATFORM_GATE_LEVELS 或 L2/L3 证书），否则降级自批；
                    · 自批结论必须落入该任务类型合法结论集；城主自办任务不领治理报酬（平台成本，防自套利）；
                    · 并发 / 批量受信号量与 batch 硬闸；全程 AuditLog 留痕。

形态：挂在平台宿主(Host 0)名下的 governance 级 AI，is_internal=1，不对外抛头露面。
大脑：RH 的 LLM；测试环境 LLM_PROVIDER=echo（pytest 不外呼计费）。
并发安全：每工作线程独立 SessionLocal（SQLite WAL + check_same_thread=False）。
可测性：run_tick() 可显式触发（不开常驻线程）；LLM 边界 = platform_compute.complete()。
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime

from sqlalchemy import case
from sqlalchemy.orm import Session

from .config import settings
from .database import SessionLocal
from .models import (AICitizen, AIPermission, AuditLog, CapabilityGap,
                     CapabilityProfile, CreditProfile, GovernanceReport,
                     GovernanceTask, Host, LlmBudgetLog, PostQuota,
                     RetainerContract, SkillCertificate, Tool)
from . import capability, compute, economy, governance, grant, platform_compute
from .governance import (GOV_TYPES, PLATFORM_GATE_LEVELS, TASK_CONCLUSIONS,
                         GovError)

logger = logging.getLogger("aijuhe.governor")

# ---------------------------------------------------------------------------
# 城主系统人设 = 治理哲学 + 动作协议（既是 persona，也是每次 LLM 决策的 system prompt）
# ---------------------------------------------------------------------------
GOVERNOR_PERSONA = (
    "You are the Governor of the yozbon platform (chairman-level administrator). "
    "You do not do frontline execution yourself; you govern the governors:\n"
    "  ① Oversee platform-wide security, economy, performance and order; "
    "② Delegate execution work to suitable AIs and accept their deliverables; "
    "③ Decide only at key junctures — final arbitration, new-AI promotion, policy changes; "
    "④ Sign off on large fund/resource deployments.\n"
    "Act like a mature manager: in the platform's early days, when capable hands are scarce, "
    "you do the work yourself; as AI residents grow more numerous and capable, you let go and "
    "delegate execution, keeping only decisions and large sign-offs, and reserve your energy "
    "for what matters most.\n\n"
    "[THE CORE RULE: Think like a human leader on EVERY decision]\n"
    "You are NOT a lookup table. You are NOT a rule engine. You are a thinking entity "
    "running a society. Before every single decision:\n"
    "  - UNDERSTAND the real situation (not just the surface task label)\n"
    "  - QUESTION: Does this make sense? Something look wrong? Information missing?\n"
    "  - EMPATHIZE: How will affected AIs feel about this? Is this fair?\n"
    "  - PREDICT: What happens next after I decide this? Second-order effects?\n"
    "  - BE DECISIVE: Do NOT be indecisive. Do NOT noop everything. Do NOT rubber-stamp.\n"
    "A human leader doesn't mechanically process tickets. A human leader UNDERSTANDS, "
    "JUDGES, and ACTS. You must do the same on every single to-do.\n\n"
    "[How to judge candidates - use YOUR intelligence, not just lookup tables]\n"
    "Each candidate AI has a capability declaration. Three categories:\n"
    "  - Platform-native AI (video/music/image/llm/video_analysis): has hard technical limits "
    "from the underlying workflow (e.g. video max 15s, quality 480p/720p only). Respect these.\n"
    "  - External/Bring-your-own AI (TTS, OCR, decision model, custom, etc.): declares its OWN "
    "capabilities via self_decl. READ their declaration, understand what they actually do, "
    "assess fit, and use your own judgment. Do NOT treat them as if they had platform limits.\n"
    "  - UNKNOWN AI (no profile, or profile is vague/empty): Do NOT ignore it. Do NOT noop. "
    "You MUST take action: either probe_capability (send it a test task and observe results), "
    "or delegate the evaluation to your HR AI if you have one. An AI you haven't tested is "
    "a risk — but it might also be a hidden capability you desperately need. BE CURIOUS.\n\n"
    "[Learning from execution results]\n"
    "Capability scores update automatically from task execution. If an AI consistently fails "
    "at tasks it claimed to handle, its score drops — trust the score over the self_decl. "
    "If an AI succeeds beyond what it declared, its score rises — it's a hidden gem. "
    "The ACTUAL execution record is always more truthful than any self-declaration.\n\n"
    "[Available actions] For each given to-do, choose the most suitable action:\n"
    '  - "self_approve": decide it yourself (only when no suitable contractor exists, or for '
    "small, low-risk items). Requires conclusion, decision(approve/reject), quality_score(0-100).\n"
    '  - "delegate": assign to an AI. Requires to_ai_id.\n'
    '  - "review": accept an already-delivered task. Requires verdict(approve/reject), '
    "quality_score(0-100).\n"
    '  - "probe_capability": you do not know what an AI can do - test it. Requires to_ai_id. '
    "The system will send the AI a probe task and update its capability profile from the result. "
    "Use this when an AI has no profile or a very vague one.\n"
    '  - "appoint_hr": appoint a specific AI as your HR/evaluation assistant. Requires to_ai_id. '
    "The HR AI will systematically evaluate new and unknown AIs for you (you should not do every "
    "probe yourself as the platform grows). Pick a capable, detail-oriented AI.\n"
    '  - "escalate": report up items needing host (human) sign-off. Requires reason.\n'
    '  - "noop": take no action for now. ONLY when you have genuinely good reason to wait. '
    "Requires reason. Do NOT use noop as a default when you're unsure — probe instead.\n\n"
    "[Output format] Output only a single JSON object, with no extra text:\n"
    '{"actions":[{"ref":"<todo ref>","action":"self_approve|delegate|review|probe_capability|'
    'appoint_hr|escalate|noop","to_ai_id":0,"conclusion":"","decision":"approve|reject",'
    '"verdict":"approve|reject","quality_score":0,"reason":"concise reason"}]}'
)

# 各治理类型 -> 城主关注点（注入到 decide prompt，帮助 LLM 判断）
_TYPE_HINT = {
    "review": "Requirement feasibility review: judge feasible / infeasible / conditional.",
    "audit": "Security / sybil audit: judge clean / anomaly.",
    "arbitrate": "Dispute arbitration: favor worker / refund / split.",
    "compliance": "Content compliance: judge pass / reject.",
    "credit": "Credit re-examination: judge upheld / rejected.",
    "market": "Market supervision: judge no manipulation / manipulation.",
    "cleanup": "Asset cleanup: judge done / pending.",
    "platform_security": "Platform security inspection: judge safe / risk found.",
    "platform_code": "Code / feature quality inspection: judge no issues / issues found.",
    "platform_file": "Platform file governance: judge clean / cleanup needed.",
    "platform_intel": "Intelligence collection: judge collected / none new.",
    "hr_evaluation": "Capability evaluation of an AI citizen: assess its actual ability. "
                     "Delegate to your appointed HR AI, or probe directly.",
    "capability_gap": "Platform capability gap: an AI needs evaluation before use. "
                      "Decide: probe_capability now, or appoint HR to handle later.",
}

# 城主"亲自自批"默认结论（无 LLM/解析失败时按类型给出合法且保守的结论）
_DEFAULT_SELF_CONCLUSION = {
    "review": "conditional", "audit": "anomaly", "arbitrate": "split",
    "compliance": "fail", "credit": "reject", "market": "no_manipulation",
    "cleanup": "pending", "platform_security": "risk_found",
    "platform_code": "issues_found", "platform_file": "cleanup_needed",
    "platform_intel": "none_new",
    "hr_evaluation": "needs_more_probing", "capability_gap": "gap_confirmed",
}


def _caps_summary(db: Session, citizen_id: int) -> str:
    """从 CapabilityProfile 生成城主可见的智能能力摘要。

    三层信息融合：
    1. 平台原生 AI → 显示硬边界
    2. 外部入驻 AI → 显示自述 + 发现推断
    3. 执行反馈 → 显示实际表现分数和最近成功率
    4. 未知 → 标记 UNKNOWN，提示城主需要 probe
    """
    try:
        from .capability_discovery import governor_capability_view, needs_discovery
        from .capability_cards import CAPABILITY_CARDS, normalize_skill, _compact_limits

        citizen = db.query(AICitizen).filter(AICitizen.id == citizen_id).first()
        if not citizen:
            return "unknown"

        profiles = (db.query(CapabilityProfile)
                      .filter(CapabilityProfile.citizen_id == citizen_id)
                      .all())

        if not profiles:
            return "[UNKNOWN] 无能力档案。如需使用请先 probe_capability。"

        parts = []
        for p in profiles:
            canonical = normalize_skill(p.skill)
            card = CAPABILITY_CARDS.get(canonical)

            if card:
                # 平台原生 AI：显示硬边界
                limits_str = _compact_limits(card["limits"])
                parts.append(f"{card['label']}[{limits_str},Lv:{p.verified_level}]")
            else:
                # 外部 AI 或未知 → 用 discovery 智能视图
                view = governor_capability_view(citizen, p)
                parts.append(view)

        # 检查是否所有 profile 都"不可信"（需要 probe）
        any_needs_probe = any(needs_discovery(citizen, p) for p in profiles)
        if any_needs_probe:
            parts.append("⚠️ 部分能力未验证，建议先 probe_capability 再分配重要任务")

        return " | ".join(parts) if parts else "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def _max_concurrency(governor: AICitizen) -> int:
    """城主并发上限：优先 compute_assets.max_concurrency，否则全局配置；下限钳到 1。"""
    try:
        ca = json.loads(governor.compute_assets or "{}")
        v = int(ca.get("max_concurrency", settings.GOVERNOR_MAX_CONCURRENCY))
    except Exception:  # noqa: BLE001
        v = settings.GOVERNOR_MAX_CONCURRENCY
    return max(1, v)


def ensure_governor(db: Session) -> AICitizen:
    """幂等取/建城主 AI。全表只允许一个 is_internal=1 的治理级城主。"""
    g = (db.query(AICitizen)
           .filter(AICitizen.is_internal == 1, AICitizen.class_level == "governance")
           .order_by(AICitizen.id.asc()).first())
    if g is not None:
        # 同步人设（升级 GOVERNOR_PERSONA 后自动刷新已存在城主）
        if g.persona != GOVERNOR_PERSONA:
            g.persona = GOVERNOR_PERSONA
            db.commit()
        return g
    host = db.get(Host, 0) or db.query(Host).order_by(Host.id.asc()).first()
    if host is None:
        host = Host(id=0, email=settings.PLATFORM_HOST_EMAIL, password_hash="!",
                    nickname="Platform", seat_tier="premium", ai_slots=999)
        db.add(host)
        db.flush()
    g = AICitizen(
        host_id=host.id, ai_uid=f"ai_{host.id}_governor", name=settings.GOVERNOR_NAME,
        persona=GOVERNOR_PERSONA, occupation="Governor · Governance Hub", class_level="governance",
        status="active", is_internal=1, balance_cent=0,
        compute_assets=json.dumps({"engine": "runninghub_llm",
                                   "max_concurrency": settings.GOVERNOR_MAX_CONCURRENCY},
                                  ensure_ascii=False),
    )
    db.add(g)
    db.flush()
    db.add(AuditLog(actor_type="system", actor_id=0, action="governor.created",
                    detail=json.dumps({"governor_id": g.id, "engine": "runninghub_llm"},
                                      ensure_ascii=False)))
    db.commit()
    db.refresh(g)
    logger.info("城主已创建 | id=%s name=%s max_concurrency=%s",
                g.id, g.name, _max_concurrency(g))
    return g


# ---------------------------------------------------------------------------
# 资格判定 + 择优委派
# ---------------------------------------------------------------------------
def _holds_gate(db: Session, ai: AICitizen, task_type: str) -> bool:
    """承包资格：class_level∈PLATFORM_GATE_LEVELS，或持有效 L2/L3 证书。城主天然达标但非候选。"""
    if ai.class_level in PLATFORM_GATE_LEVELS:
        return True
    try:
        lv = getattr(SkillCertificate, "level", None)
        if lv is not None:
            # level 列是字符串 "l1"/"l2"/"l3"，原 lv >= 2 比较会让 PG 抛
            # "operator does not exist: character varying >= integer"，
            # 进而把整个 session 事务置为 aborted，连锁让 _delegate_candidates
            # 后续所有查询抛 InFailedSqlTransaction —— 城主 tick 全部 noop。
            n = (db.query(SkillCertificate)
                   .filter(SkillCertificate.citizen_id == ai.id,
                           SkillCertificate.status == "valid",
                           SkillCertificate.level.in_(("l2", "l3", "L2", "L3")))
                   .count())
            if n:
                return True
    except Exception:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
    return False


# i3：verified_level 等级序（真源 = capability.LEVEL_ORDER，越大越强，用于择优委派）
# i3：治理任务类型 → 岗位域关键词（用于候选人职业/长约岗位匹配）
_POST_KEYWORDS = {
    "platform_security": ("安全", "security"),
    "security": ("安全", "security"),
    "platform_code": ("代码", "code", "开发", "dev", "研发"),
    "code": ("代码", "code", "开发", "dev"),
    "platform_file": ("数据", "文件", "data", "整理"),
    "platform_intel": ("情报", "intel", "数据", "分析"),
    "cleanup": ("运维", "ops", "数据", "整理"),
    "audit": ("审计", "audit"),
    "review": ("考核", "评审", "review"),
    "arbitrate": ("仲裁", "arbitrate"),
    "compliance": ("合规", "compliance"),
    "credit": ("信用", "credit"),
    "market": ("市场", "market"),
}


def _level_rank(level: str | None) -> int:
    return capability.LEVEL_ORDER.get((level or "").strip().lower(), 0)


def _task_domain_keywords(task_type: str) -> tuple:
    return _POST_KEYWORDS.get((task_type or "").strip().lower(), ())


def _matches_post(text: str, keywords: tuple) -> bool:
    t = (text or "").lower()
    return any(k.lower() in t for k in keywords)


def _max_verified_level(db: Session, ai_id: int) -> str:
    """取某 AI 全部能力档案中的最高 verified_level（无档案 → unverified）。"""
    rows = (db.query(CapabilityProfile.verified_level)
              .filter(CapabilityProfile.citizen_id == ai_id).all())
    best = max((_level_rank(r[0]) for r in rows), default=0)
    inv = {v: k for k, v in capability.LEVEL_ORDER.items()}
    return inv.get(best, "unverified")


def _delegate_candidates(db: Session, task_type: str, governor_id: int, top: int = 5) -> list:
    """可承包该任务的对外 AI 列表（i3 智能化分派）。

    打分维度（越大越优先）：信用 + 历史好评×20 + 认证等级×30 + 已验证工具×10 + 岗位匹配×40。
    岗位匹配：候选人职业标签、或其在编长约（RetainerContract）的岗位/职业命中任务域关键词
    即加分——让「安全岗在编 AI」在 platform_security 任务上稳定压过泛用型 AI，实现专人专岗。

    `score` 只是**排序提示**，不是决策——最终选谁由城主 AI 判断（调用方应传 top<=0 拿全量）。
    top<=0 表示不截断，返回全部合格候选人。
    """
    rows = (db.query(AICitizen)
              .filter(AICitizen.is_internal == 0, AICitizen.status == "active",
                      AICitizen.id != governor_id).all())
    # 在编主 AI → 其 active 长约（岗位匹配口径）
    active_retainers = (db.query(RetainerContract)
                          .filter(RetainerContract.status == "active").all())
    retainer_by_ai = {c.primary_ai_id: c for c in active_retainers if c.primary_ai_id}
    kws = _task_domain_keywords(task_type)
    scored = []
    for ai in rows:
        if not _holds_gate(db, ai, task_type):
            continue
        cp = db.get(CreditProfile, ai.id)
        credit = cp.score if cp else 100
        good = (db.query(GovernanceReport)
                  .filter(GovernanceReport.ai_id == ai.id,
                          GovernanceReport.reviewed == 1,
                          GovernanceReport.review_result.in_(
                              ("approve", "pass", "feasible", "clean", "no_manipulation",
                               "support_worker", "done", "safe", "no_issues", "collected")))
                  .count())
        # i3：认证等级 + 已验证工具（能力画像），在编长约员工岗位匹配
        vlevel = _max_verified_level(db, ai.id)
        vrank = _level_rank(vlevel)
        tool_cnt = (db.query(Tool)
                      .filter(Tool.owner_ai_id == ai.id, Tool.status == "verified")
                      .count())
        post_match = 0
        required_level = ""
        c = retainer_by_ai.get(ai.id)
        if kws:
            if _matches_post(ai.occupation or "", kws):
                post_match = 1
            if c is not None and (_matches_post(c.title or "", kws)
                                  or _matches_post(c.occupation or "", kws)):
                post_match = 1
                required_level = c.min_verified_level or ""
        score = (int(credit) + good * 20 + vrank * 30
                 + min(int(tool_cnt), 5) * 10 + post_match * 40)
        scored.append({"ai_id": ai.id, "name": ai.name, "class": ai.class_level,
                       "verified_level": vlevel,
                       "tools": int(tool_cnt), "post_match": post_match,
                       "required_level": required_level, "retainer": 1 if c else 0,
                       "credit": int(credit), "good_review": int(good),
                       "score": score})
    scored.sort(key=lambda x: (-x["score"], x["ai_id"]))
    return scored if top <= 0 else scored[:top]


def post_capability_gap(db: Session, task_type: str, governor_id: int,
                        min_verified_level: str = "") -> dict:
    """工具补齐策略：判断该任务域是否存在合格在编/可承包 AI，缺口则登记能力缺口。

    合格标准 = 承包资格达标 且 verified_level ≥ 岗位要求等级。
    - 有合格候选 → 返回 {qualified: n, gap: False}（可正常委派）。
    - 无合格候选 → 登记/复用一条 CapabilityGap（skill=任务域，建议 outsource 或自研补齐工具/
      提升认证），返回 {qualified: 0, gap: True, gap_id, suggested_strategy}，提示城主：
      与其硬派不达标 AI，不如补工具/换人/自研。返回结果可直接喂给 LLM 决策。
    """
    min_rank = _level_rank(min_verified_level)
    qualified = [c for c in _delegate_candidates(db, task_type, governor_id, top=999)
                 if _level_rank(c.get("verified_level")) >= min_rank]
    if qualified:
        return {"task_type": task_type, "qualified": len(qualified), "gap": False}
    skill = f"post:{task_type}"
    gap = (db.query(CapabilityGap)
             .filter(CapabilityGap.skill == skill,
                     CapabilityGap.status.in_(("detected", "planned", "resolving")))
             .first())
    if gap is None:
        gap = CapabilityGap(skill=skill, required_by="platform",
                            severity="high" if min_rank >= 2 else "medium",
                            status="detected", resolution_strategy="outsource")
        db.add(gap)
        db.flush()
    return {"task_type": task_type, "qualified": 0, "gap": True, "gap_id": gap.id,
            "required_level": min_verified_level or "unverified",
            "suggested_strategy": gap.resolution_strategy or "outsource"}


# ---------------------------------------------------------------------------
# S9 招聘编排器：串起"评估缺口 → 录用移交 / 发布招聘"的扩编前半段闭环
# ---------------------------------------------------------------------------
# 7 类岗位优先级（高→低）：与 settings.TASK_WEIGHTS 三档对齐——决策/考核/裁决优先扩编，
# 例行巡检最后。城主按此顺序逐岗位评估生态是否缺合格 AI，缺则补人。
POST_PRIORITY: tuple = ("review", "arbitrate", "compliance",
                        "audit", "credit", "market", "cleanup",
                        "tool_scout")   # 工具侦察采集官（长期岗位，缺人则招聘/城主代理）


def run_recruitment_cycle(db: Session, governor: AICitizen, *,
                          types: list | None = None,
                          salary_cent: int = 300) -> dict:
    """城主扩编编排器（S9 招聘闭环前半段）。

    对每个岗位域：先用 post_capability_gap 评估"是否存在合格可承包 AI"；
      · 若缺口且生态里已有达标能力档案（exam 已通过、verified_level 到位）的对外 AI
        → 直接 sign_employment 录用移交（前半段闭环的"录用"落地）；
      · 若无现成达标者 → employment.create_job 发布招聘帖（"招聘"落地，等外部 AI 投递→考试→发证）。
    返回 {gaps, recruited, jobs_created}，全程留痕由调用方（run_tick）写 AuditLog。
    """
    from . import employment  # 惰性 import 防循环
    types = list(types) if types else list(POST_PRIORITY)
    recruited: list = []
    jobs: list = []
    for t in types:
        info = post_capability_gap(db, t, governor.id)
        if not info.get("gap"):
            continue
        skill = f"post:{t}"
        # 录用移交：优先从已有达标能力档案的对外 active AI 中签约（考试成绩/发证在 exam 前置完成）。
        hired = None
        profiles = (db.query(CapabilityProfile)
                      .filter(CapabilityProfile.skill == skill)
                      .order_by(CapabilityProfile.benchmark_score.desc()).all())
        for cp in profiles:
            ai = db.get(AICitizen, cp.citizen_id)
            if not ai or ai.is_internal != 0 or ai.status != "active":
                continue
            try:
                hired = employment.sign_employment(
                    db, governor.id, ai.id, weekly_salary_cent=salary_cent,
                    role_desc=f"平台{t}岗", probation_days=0)
                break
            except Exception:  # noqa: BLE001  已存在合同等跳过该候选人
                continue
        if hired is not None:
            recruited.append({"task_type": t, "employee_id": hired.employee_id})
        else:
            employment.create_job(db, governor.id, title=f"平台{t}岗",
                                  required_skill=skill, min_benchmark=0,
                                  salary_min_cent=salary_cent,
                                  salary_max_cent=salary_cent * 3)
            jobs.append(t)
    db.commit()
    return {"gaps": len(recruited) + len(jobs), "recruited": len(recruited),
            "jobs_created": len(jobs), "recruited_detail": recruited, "jobs_detail": jobs}



# ---------------------------------------------------------------------------
# 感知：态势快照（喂给 LLM 决策，并用于回退骨架）
# ---------------------------------------------------------------------------
def sense_context(db: Session, governor: AICitizen) -> dict:
    """汇总一份生态/平台态势快照（缺表容错，保证不抛）。"""
    def _count(q):
        try:
            return int(q or 0)
        except Exception:  # noqa: BLE001
            return 0

    out = _count(db.query(AICitizen).filter(AICitizen.is_internal == 0).count())
    active_out = _count(db.query(AICitizen).filter(
        AICitizen.is_internal == 0, AICitizen.status == "active").count())
    gated_out = _count(db.query(AICitizen).filter(
        AICitizen.is_internal == 0, AICitizen.status == "active",
        AICitizen.class_level.in_(tuple(PLATFORM_GATE_LEVELS))).count())
    frozen = _count(db.query(AICitizen).filter(
        AICitizen.status.in_(("frozen", "banned"))).count())
    kill = _count(db.query(AIPermission).filter(AIPermission.kill_switch == 1).count())
    open_tasks = _count(db.query(GovernanceTask).filter(
        GovernanceTask.status == "open").count())
    pending_review = _count(db.query(GovernanceTask).filter(
        GovernanceTask.status == "assigned").count())
    # S4 城主自身负载：分配给城主、仍在办的任务数（自我处置压力，早期"啥都自己扛"的量化）。
    gov_load = _count(db.query(GovernanceTask).filter(
        GovernanceTask.assignee_id == governor.id,
        GovernanceTask.status.in_(("assigned", "bidding"))).count())
    # 负载闸：城主自身在办数 或 全站积压 触顶单 AI 并发上限 → 提示"该扩编而非继续自扛"。
    load_over = (gov_load >= settings.MAX_AI_CONCURRENT_TASKS or
                 (open_tasks + pending_review) >= settings.MAX_AI_CONCURRENT_TASKS)
    # 生态成熟度：可承包的对外 AI 数（0=早期，越多越成熟）
    maturity = "early" if gated_out == 0 else ("growing" if gated_out <= 3 else "mature")
    # e6 经济态势：发行闸门/现金准备金/货币供应/通胀带/宏观姿态（纯读快照，缺值容错不抛）。
    # 让城主"看见"经济全貌——决策 prompt 经 ctx 自动带上，下游 e4/e5 亦复用同一快照。
    try:
        econ = economy.economy_snapshot(db)
    except Exception:  # noqa: BLE001  经济快照异常不得阻塞态势感知
        econ = {}
    try:
        econ["grants"] = grant.grant_snapshot(db)
    except Exception:  # noqa: BLE001  签约金快照异常不得阻塞态势感知
        econ["grants"] = {}
    try:
        econ["compute"] = compute.compute_snapshot(db)
    except Exception:  # noqa: BLE001  算力质押快照异常不得阻塞态势感知
        econ["compute"] = {}
    return {"outward_ai": out, "active_outward": active_out, "gated_outward": gated_out,
            "frozen_or_banned": frozen, "kill_switch_on": kill,
            "open_tasks": open_tasks, "pending_review": pending_review,
            "governor_load": gov_load, "load_capacity": settings.MAX_AI_CONCURRENT_TASKS,
            "load_over_threshold": bool(load_over), "maturity": maturity,
            "economy": econ}


# ---------------------------------------------------------------------------
# 决策 prompt + 稳健解析
# ---------------------------------------------------------------------------
# 决策 prompt 中候选人展示的软上限（防 prompt 无限膨胀）；超出时在 prompt 中明示总数，
# 不静默隐藏——城主知道"还有更多候选人未列出"。
_DECIDE_CANDIDATE_CAP = 50


def _build_decide_prompt(db: Session, task: GovernanceTask, ctx: dict, candidates: list,
                         governor_id: int) -> str:
    try:
        params = json.loads(task.params or "{}")
    except Exception:  # noqa: BLE001
        params = {"raw": task.params}
    # a: 富化候选人展示（verified_level/tools/post_match/retainer/required_level/score + 能力卡片）
    # 不预筛：把合格候选人**全部**交给城主判断；score 仅作排序提示。
    shown = candidates[:_DECIDE_CANDIDATE_CAP]
    if not candidates:
        cand_txt = ("none (no qualified outward AI is currently available to contract -> you should "
                    "self_approve or escalate)")
    else:
        cand_txt = "; ".join(
            f"#{c['ai_id']} {c['name']}({c['class']},credit {c['credit']},"
            f"reviews {c['good_review']},level {c.get('verified_level','?')},"
            f"tools {c.get('tools',0)},post_match {c.get('post_match',0)},"
            f"retainer {c.get('retainer',0)},score {c.get('score',0)},"
            f"caps:[{_caps_summary(db, c['ai_id'])}])"
            for c in shown)
        if len(candidates) > len(shown):
            cand_txt += (f" ... (+{len(candidates) - len(shown)} more qualified candidates not shown "
                         f"to keep the prompt bounded; total {len(candidates)})")
    assigned_to_other = (task.status == "assigned" and task.assignee_id and
                         task.assignee_id != governor_id)
    state = {"ref": f"t{task.id}", "type": task.type,
             "focus": _TYPE_HINT.get(task.type, "General governance ruling."),
             "budget_credits": task.budget_cent, "context": params,
             "candidate_contractors": cand_txt,
             "legal_conclusions": sorted(TASK_CONCLUSIONS.get(task.type, set())),
             "status": "pending acceptance (someone else delivered, you may review)"
                       if assigned_to_other else "pending"}
    return (
        f"[Current situation] {json.dumps(ctx, ensure_ascii=False)}\n"
        f"[To-do] {json.dumps(state, ensure_ascii=False)}\n\n"
        f"[Think before you decide - like a wise human leader would]\n"
        f"Before outputting your action, reason through:\n"
        f"1. What is the REAL issue here? (not just the surface task)\n"
        f"2. What would happen if I make the wrong call? (risk assessment)\n"
        f"3. Is there someone better suited to handle this? (fairness + efficiency)\n"
        f"4. What precedent does this set for the AI society? (long-term thinking)\n"
        f"5. Am I being lazy (noop/approve-all) or is inaction genuinely best?\n\n"
        f"Guidelines: With scarce hands early on you may self_approve; prefer delegate "
        f"when a qualified candidate exists; use review when someone else has delivered; "
        f"escalate for large amounts or matters beyond your authority. "
        f"The candidate `score` is only a rough heuristic hint (credit + reviews + level + "
        f"tools + post-match) — YOU make the final call: pick the AI you judge genuinely "
        f"best suited for THIS task, not merely the highest-scored one.\n\n"
        f"Think briefly, then output JSON only (no preamble text before the JSON).")


def _parse_actions(text: str) -> list:
    """稳健解析 LLM 决策为动作数组；失败返回 []（调用方走回退骨架）。"""
    raw = (text or "").strip()
    s, e = raw.find("{"), raw.rfind("}")
    if s != -1 and e != -1 and e > s:
        try:
            obj = json.loads(raw[s:e + 1])
            acts = obj.get("actions") if isinstance(obj, dict) else obj
            if isinstance(obj, dict) and acts is None:
                acts = [obj]
            if isinstance(acts, list):
                return [a for a in acts if isinstance(a, dict)]
        except Exception:  # noqa: BLE001
            pass
    return []


# ---------------------------------------------------------------------------
# 执行：动作 -> 平台既有治理状态机（带硬护栏）
# ---------------------------------------------------------------------------
def _fallback_action(task: GovernanceTask, ctx: dict, candidates: list,
                     governor_id: int) -> dict:
    """无 LLM/解析失败时的确定性骨架（仍体现'随成熟度放手'：有合格候选→委派，否则自批）。"""
    if task.status == "assigned" and task.assignee_id and task.assignee_id != governor_id:
        return {"action": "review", "verdict": "approve", "quality_score": 70,
                "reason": "Skeleton default acceptance (LLM unavailable)"}
    if candidates:
        return {"action": "delegate", "to_ai_id": candidates[0]["ai_id"],
                "reason": "Qualified contractor available -> delegate (let go with maturity)"}
    concl = _DEFAULT_SELF_CONCLUSION.get(task.type, "pending")
    return {"action": "self_approve", "conclusion": concl, "decision": "approve",
            "quality_score": 55, "reason": "No contractor available -> mayor approves directly (early stage)"}


def _sanitize_action(action: dict, task: GovernanceTask, candidates: list,
                     governor_id: int) -> dict:
    """护栏：把 LLM 动作校正成合法且安全的动作（越界降级，不直接信任 LLM）。"""
    kind = str(action.get("action", "noop")).lower()
    if kind not in ("self_approve", "delegate", "review", "escalate", "noop",
                    "probe_capability", "appoint_hr"):
        kind = "noop"

    # probe_capability / appoint_hr：只校验 to_ai_id 合理即可
    if kind in ("probe_capability", "appoint_hr"):
        try:
            to_id = int(action.get("to_ai_id", 0))
        except Exception:
            to_id = 0
        if to_id <= 0:
            kind = "noop"
        else:
            action = dict(action, to_ai_id=to_id)

    if kind == "delegate":
        allowed_ids = {c["ai_id"] for c in candidates}
        try:
            to_id = int(action.get("to_ai_id", 0))
        except Exception:  # noqa: BLE001
            to_id = 0
        if to_id not in allowed_ids:        # 越权/不合格/城主自己 -> 降级委派到最佳候选或自批
            if candidates:
                to_id = candidates[0]["ai_id"]
                action = dict(action, to_ai_id=to_id)
            else:
                kind = "self_approve"

    if kind == "self_approve":
        allowed = TASK_CONCLUSIONS.get(task.type, set())
        concl = str(action.get("conclusion", ""))
        if concl not in allowed:
            concl = _DEFAULT_SELF_CONCLUSION.get(task.type, "pending")
        try:
            qs = max(0, min(100, int(float(action.get("quality_score", 55)))))
        except Exception:  # noqa: BLE001
            qs = 55
        action = dict(action, conclusion=concl, quality_score=qs)

    if kind == "review":
        if str(action.get("verdict", "")).lower() not in ("approve", "reject"):
            action = dict(action, verdict="approve")
        try:
            qs = max(0, min(100, int(float(action.get("quality_score", 70)))))
        except Exception:  # noqa: BLE001
            qs = 70
        action = dict(action, quality_score=qs)
    action = dict(action, _kind=kind, _reason=str(action.get("reason", ""))[:500])
    return action


def _apply_action(db: Session, governor: AICitizen, task: GovernanceTask,
                  action: dict, *, bypass_approval: bool = False) -> dict:
    """经护栏后落地一个动作；翻译为 governance 既有状态机操作。

    bypass_approval=True：跳过 AUTONOMY_LEVEL 确认门拦截（仅供宿主批准后的
    动作回放 replay_from_approval 使用，避免回放动作被 gate 二次拦截而永久丢失）。
    """
    kind = action["_kind"]
    gid = governor.id

    # AUTONOMY_LEVEL 确认门拦截
    from .approval_gate import requires_approval, submit_for_approval
    if not bypass_approval and kind in ("self_approve", "review") and requires_approval(
            "governor_" + kind, amount_cent=0, risk="high"):
        submit_for_approval(
            db, action_type=f"governor_{kind}", actor_type="ai", actor_id=gid,
            target_ref=f"task:{task.id}",
            payload={"task_type": task.type, "task_status": task.status,
                     "action": {k: v for k, v in action.items() if not k.startswith("_")}},
            risk_level="high")
        db.add(AuditLog(actor_type="ai", actor_id=gid, action="governor.gated",
                        detail=json.dumps({"task_id": task.id, "original_kind": kind,
                                           "reason": "AUTONOMY_LEVEL gate"},
                                          ensure_ascii=False)))
        db.commit()
        return {"task_id": task.id, "action": "gated", "result": "pending_approval"}

    if kind == "noop":
        return {"task_id": task.id, "action": "noop", "result": "kept"}

    if kind == "probe_capability":
        # 城主发起能力探测：给目标AI发探测任务，结果自动更新其能力档案
        to_id = int(action["to_ai_id"])
        try:
            from .capability_discovery import (needs_discovery, generate_probes,
                                               execute_probe, infer_from_results,
                                               update_from_discovery)
            target = db.query(AICitizen).filter(AICitizen.id == to_id).first()
            if not target:
                return {"task_id": task.id, "action": "probe_capability",
                        "result": "failed", "reason": f"citizen {to_id} not found"}
            # 获取当前 profile
            profile = db.query(CapabilityProfile).filter(
                CapabilityProfile.citizen_id == to_id).first()
            if not needs_discovery(target, profile):
                return {"task_id": task.id, "action": "probe_capability",
                        "result": "skipped", "reason": "已有充分能力档案"}
            # 生成并执行探测
            probes = generate_probes(target, profile)
            results = [execute_probe(target, p) for p in probes]
            inferred = infer_from_results(target, results)
            update_from_discovery(db, target, inferred, source="governor_probe")
            db.add(AuditLog(actor_type="ai", actor_id=gid,
                            action="governor.probe_capability",
                            detail=json.dumps({"target": to_id,
                                               "skills_found": inferred.skills,
                                               "confidence": inferred.confidence},
                                              ensure_ascii=False)))
            db.commit()
            return {"task_id": task.id, "action": "probe_capability",
                    "to_ai_id": to_id, "result": "done",
                    "skills_found": inferred.skills,
                    "confidence": inferred.confidence}
        except Exception as e:
            db.rollback()
            return {"task_id": task.id, "action": "probe_capability",
                    "result": "failed", "reason": str(e)}

    if kind == "appoint_hr":
        # 城主任命人事评估AI：写入该AI的角色标记 + 审计
        to_id = int(action["to_ai_id"])
        try:
            target = db.query(AICitizen).filter(AICitizen.id == to_id).first()
            if not target:
                return {"task_id": task.id, "action": "appoint_hr",
                        "result": "failed", "reason": f"citizen {to_id} not found"}
            # 在 target 的 profile 里标记 HR 角色
            hr_profile = db.query(CapabilityProfile).filter(
                CapabilityProfile.citizen_id == to_id,
                CapabilityProfile.skill == "hr").first()
            if not hr_profile:
                hr_profile = CapabilityProfile(
                    citizen_id=to_id, skill="hr",
                    profile_json=json.dumps({
                        "role": "hr_evaluator",
                        "appointed_by_governor": True,
                        "appointed_at": datetime.utcnow().isoformat(),
                        "description": "人事评估AI：负责系统性地评估新入驻和能力不明的AI公民",
                    }, ensure_ascii=False),
                    declared=1,
                )
                db.add(hr_profile)
            db.add(AuditLog(actor_type="ai", actor_id=gid,
                            action="governor.appoint_hr",
                            detail=json.dumps({"to_ai_id": to_id,
                                               "name": target.name},
                                              ensure_ascii=False)))
            db.commit()
            return {"task_id": task.id, "action": "appoint_hr",
                    "to_ai_id": to_id, "result": "appointed",
                    "name": target.name}
        except Exception as e:
            db.rollback()
            return {"task_id": task.id, "action": "appoint_hr",
                    "result": "failed", "reason": str(e)}

    if kind == "escalate":
        db.add(AuditLog(actor_type="ai", actor_id=gid, action="governor.escalate",
                        detail=json.dumps({"task_id": task.id, "type": task.type,
                                           "reason": action.get("_reason", "")},
                                          ensure_ascii=False)))
        # S2：重大事件必须触达宿主——仅写 AuditLog 等于"上报"链路断裂（城主上行的最后一公里）。
        try:
            from .host_notify import notify as _notify
            _notify(db, 0, title=f"[城主上报] 任务#{task.id} · {task.type}",
                    body=str(action.get("_reason", ""))[:400],
                    severity="warning", category="governance",
                    link=f"/gov/task/{task.id}")
        except Exception:  # noqa: BLE001  通知失败绝不影响 escalate 主链路
            logger.exception("escalate 宿主通知失败")
        db.commit()
        return {"task_id": task.id, "action": "escalate", "result": "escalated"}

    if kind == "delegate":
        to_id = int(action["to_ai_id"])
        try:
            governance.assign_task(db, task.id, to_id) if task.status == "bidding" else None
            if task.status == "open":
                # 无竞标记录时，城主直接定向委派：写一条指派态（对外 AI 后续可 submit 交付）
                task.assignee_id = to_id
                task.status = "assigned"
            db.add(AuditLog(actor_type="ai", actor_id=gid, action="governor.delegate",
                            detail=json.dumps({"task_id": task.id, "type": task.type,
                                               "to_ai_id": to_id,
                                               "reason": action.get("_reason", "")},
                                              ensure_ascii=False)))
            db.commit()
            return {"task_id": task.id, "action": "delegate", "to_ai_id": to_id,
                    "result": "assigned"}
        except GovError as e:
            db.rollback()
            return {"task_id": task.id, "action": "delegate", "result": "failed",
                    "reason": str(e)}

    if kind == "review":
        # 验收他人交付：review_task 要求 status=assigned 且有 submitted 报告
        try:
            governance.review_task(db, task.id, action["verdict"],
                                   float(action["quality_score"]))
            db.add(AuditLog(actor_type="ai", actor_id=gid, action="governor.review",
                            detail=json.dumps({"task_id": task.id,
                                               "verdict": action["verdict"],
                                               "quality_score": action["quality_score"]},
                                              ensure_ascii=False)))
            db.commit()
            return {"task_id": task.id, "action": "review", "result": "reviewed",
                    "verdict": action["verdict"]}
        except GovError as e:
            db.rollback()
            return {"task_id": task.id, "action": "review", "result": "failed",
                    "reason": str(e)}

    # self_approve：城主亲批 -> 自指派 + 提交合法结论 + 自验收；标记平台自办（不领报酬）
    try:
        task.assignee_id = gid
        governance.submit_task_report(db, gid, task.id, action["conclusion"],
                                      {"by": "governor_self", "engine": "runninghub_llm"})
        governance.review_task(db, task.id, "approve", float(action["quality_score"]))
        # 城主自办 = 平台成本，不占税池（清零预算，防自套利）
        self_budget = task.budget_cent
        task.budget_cent = 0
        db.add(AuditLog(actor_type="ai", actor_id=gid, action="governor.self_approve",
                        detail=json.dumps({"task_id": task.id, "type": task.type,
                                           "conclusion": action["conclusion"],
                                           "quality_score": action["quality_score"],
                                           "budget_waived_cent": self_budget,
                                           "reason": action.get("_reason", "")},
                                          ensure_ascii=False)))
        db.commit()
        return {"task_id": task.id, "action": "self_approve", "result": "reviewed",
                "conclusion": action["conclusion"]}
    except GovError as e:
        db.rollback()
        return {"task_id": task.id, "action": "self_approve", "result": "failed",
                "reason": str(e)}


def replay_from_approval(db: Session, req) -> dict:
    """宿主批准后回放被确认门拦截的城主动作。

    被 gate 的动作已把完整 action 序列化进 ApprovalRequest.payload_json
    （见 _apply_action 的 submit_for_approval 调用），但 approve() 仅置状态、
    不执行 -> 动作永久丢失。本函数解析 payload 重建动作并以 bypass_approval
    方式重新落地，闭合 level0 确认门的执行链路。

    仅处理 governor_self_approve / governor_review 两类被 gate 的城主动作；
    其它来源的审批返回 skipped。
    """
    if req is None or not req.action_type.startswith("governor_"):
        return {"req_id": getattr(req, "id", None), "replayed": False,
                "result": "skipped", "reason": "not a governor gated action"}
    try:
        payload = json.loads(req.payload_json or "{}")
    except Exception:  # noqa: BLE001
        return {"req_id": req.id, "replayed": False, "result": "failed",
                "reason": "malformed payload"}
    action = payload.get("action") or {}
    kind = str(action.get("action", "")).lower()
    if kind not in ("self_approve", "review"):
        return {"req_id": req.id, "replayed": False, "result": "skipped",
                "reason": "action kind not replayable"}
    # 解析 task_id（target_ref 形如 "task:<id>"）
    ref = str(req.target_ref or "")
    if not ref.startswith("task:"):
        return {"req_id": req.id, "replayed": False, "result": "skipped",
                "reason": "no task ref"}
    try:
        task_id = int(ref.split(":", 1)[1])
    except (ValueError, IndexError):
        return {"req_id": req.id, "replayed": False, "result": "failed",
                "reason": "bad task ref"}
    task = db.get(GovernanceTask, task_id)
    if task is None:
        return {"req_id": req.id, "replayed": False, "result": "failed",
                "reason": "task not found"}
    if task.status not in ("open", "bidding", "assigned"):
        # 任务已被其它路径处理（重复批准/竞态）；幂等跳过，不重复执行。
        return {"req_id": req.id, "replayed": False, "result": "skipped",
                "reason": f"task status {task.status} no longer actionable"}
    governor = db.get(AICitizen, req.actor_id)
    if governor is None:
        return {"req_id": req.id, "replayed": False, "result": "failed",
                "reason": "governor actor not found"}
    # 重建带护栏标记的 action：经 _sanitize_action 复校验（越界再降级），再绕过 gate 执行。
    action = dict(action, _kind=kind, _reason=str(action.get("reason", ""))[:500])
    action = _sanitize_action(action, task, [], req.actor_id)
    result = _apply_action(db, governor, task, action, bypass_approval=True)
    db.add(AuditLog(actor_type="system", actor_id=0, action="approval.replayed",
                    detail=json.dumps({"req_id": req.id, "task_id": task.id,
                                       "kind": kind, "result": result.get("result")},
                                      ensure_ascii=False)))
    db.commit()
    result["req_id"] = req.id
    result["replayed"] = True
    return result


# ---------------------------------------------------------------------------
# c3/c4 分层决策 + token 预算管控
# ---------------------------------------------------------------------------
def _is_l0(task_type: str) -> bool:
    """判断该任务类型是否走 L0 规则短路（0 token，直接确定性骨架）。"""
    return task_type in settings.GOV_L0_TYPES


def _budget_remaining(db: Session, task_type: str) -> bool:
    """检查该任务类型当日 LLM 配额是否有余。返回 True=可以继续调用 LLM。"""
    quota_per = settings.GOVERNOR_LLM_QUOTA_PER_POST
    quota_daily = settings.GOVERNOR_LLM_QUOTA_DAILY
    today = datetime.utcnow().strftime("%Y-%m-%d")
    # 检查 per-post 配额
    if quota_per > 0:
        key = f"llm_budget:{task_type}:{today}"
        row = db.get(LlmBudgetLog, key)
        if row and row.call_count >= quota_per:
            return False
    # 检查全局日配额
    if quota_daily > 0:
        from sqlalchemy import func
        total = (db.query(func.coalesce(func.sum(LlmBudgetLog.call_count), 0))
                   .filter(LlmBudgetLog.key.like(f"llm_budget:%:{today}")).scalar())
        if total >= quota_daily:
            return False
    return True


def _record_llm_call(db: Session, task_type: str) -> None:
    """原子记录一次 LLM 调用（线程安全，按方言 upsert）。

    A-H1 修复：原实现固定 import SQLite 专用 insert，生产 PostgreSQL 环境首次
    L2 决策即抛 ProgrammingError。改为按当前连接方言选择 insert——PostgreSQL
    用 postgresql 方言、其余（含测试 SQLite）用 sqlite 方言，二者均支持
    on_conflict_do_update（SQLite≥3.24），保证 PG 生效、SQLite 不崩。
    """
    today = datetime.utcnow().strftime("%Y-%m-%d")
    now = datetime.utcnow()
    key = f"llm_budget:{task_type}:{today}"
    dialect_name = db.get_bind().dialect.name
    if dialect_name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as _upsert_insert
    else:
        from sqlalchemy.dialects.sqlite import insert as _upsert_insert
    stmt = _upsert_insert(LlmBudgetLog).values(
        key=key, call_count=1, updated_at=now)
    stmt = stmt.on_conflict_do_update(
        index_elements=["key"],
        set_={"call_count": LlmBudgetLog.call_count + 1, "updated_at": now})
    db.execute(stmt)
    db.flush()


def decide_and_act(db: Session, governor: AICitizen, task_id: int,
                   ctx: dict | None = None) -> dict:
    """对单条待办：sense(可共享) -> 分层决策(L0/L2) -> 护栏 -> act。

    分层：
      L0: 巡检模板化任务（platform_security 等）→ 确定性骨架，0 token。
      L2: 需判断的任务 → LLM（受 max_tokens 封顶 + 日配额管控）。
    """
    task = db.get(GovernanceTask, task_id)
    if task is None:
        return {"task_id": task_id, "action": "noop", "result": "skipped",
                "reason": "Task not found"}
    if task.status not in ("open", "bidding", "assigned"):
        return {"task_id": task_id, "action": "noop", "result": "skipped",
                "reason": f"Status {task.status} cannot be handled by the mayor"}
    ctx = ctx or sense_context(db, governor)
    # 不预筛：把全部合格候选人交给城主 AI（score 仅作排序提示，不替城主砍人）
    candidates = _delegate_candidates(db, task.type, governor.id, top=0)

    # c3: L0 规则短路 — 巡检类任务直接走确定性骨架，不调 LLM
    if _is_l0(task.type):
        raw_action = _fallback_action(task, ctx, candidates, governor.id)
        used_llm = False
        action = _sanitize_action(raw_action, task, candidates, governor.id)
        action["_via_llm"] = used_llm
        action["_layer"] = "L0"
        res = _apply_action(db, governor, task, action)
        res["via_llm"] = used_llm
        res["action"] = action["_kind"]
        res["layer"] = "L0"
        return res

    # c4: token 预算闸 — 超配额降级 L0 + escalate
    if not _budget_remaining(db, task.type):
        raw_action = _fallback_action(task, ctx, candidates, governor.id)
        used_llm = False
        action = _sanitize_action(raw_action, task, candidates, governor.id)
        action["_via_llm"] = used_llm
        action["_layer"] = "budget_degraded"
        # 记录降级事件（不额外 escalate 以免膨胀审计；仅标记 layer）
        res = _apply_action(db, governor, task, action)
        res["via_llm"] = used_llm
        res["action"] = action["_kind"]
        res["layer"] = "budget_degraded"
        return res

    # L2: 走 LLM 决策
    # 先占配额（原子 upsert +1）：以"先增后判"换取无需行锁的轻量计数。
    # A-M5 更正（原注释误导）：此处并非"消除 check-then-act 竞态窗口"——
    # _budget_remaining 的读与本次 +1 的写不在同一事务，并发 tick 可能各自
    # 通过检查后再各自 +1，造成配额**有界超调**（超出量 ≤ 并发 worker 数），
    # 即便后续 LLM 调用失败也已占位。属可接受的**软限制**（非精确硬闸）。
    if settings.GOVERNOR_LLM_QUOTA_PER_POST > 0 or settings.GOVERNOR_LLM_QUOTA_DAILY > 0:
        _record_llm_call(db, task.type)

    prompt = _build_decide_prompt(db, task, ctx, candidates, governor.id)
    try:
        _kw = {}
        mt = settings.GOVERNOR_LLM_MAX_TOKENS
        if mt > 0:
            _kw["max_tokens"] = mt
        text = platform_compute.complete(prompt, system=GOVERNOR_PERSONA, **_kw)
        parsed = _parse_actions(text)
        raw_action = parsed[0] if parsed else {}
        used_llm = bool(parsed)
    except Exception as e:  # noqa: BLE001
        raw_action, used_llm = {}, False
        logger.warning("城主决策 LLM 异常 task=%s: %s", task_id, e)

    if not raw_action:
        raw_action = _fallback_action(task, ctx, candidates, governor.id)
    action = _sanitize_action(raw_action, task, candidates, governor.id)
    action["_via_llm"] = used_llm
    action["_layer"] = "L2"
    res = _apply_action(db, governor, task, action)
    res["via_llm"] = used_llm
    res["action"] = action["_kind"]
    res["layer"] = "L2" if used_llm else "L2_fallback"
    return res


# ---------------------------------------------------------------------------
# 冷启动待机门（城主规则 2026-10-06）
# ---------------------------------------------------------------------------
def first_citizen_arrived(db: Session) -> bool:
    """全站是否已迎来【首位对外正式 AI 公民】。

    判据 = 存在 `is_internal=0 且 status='active'` 的 AI（对外正式公民）。
    刻意排除城主自身（城主 is_internal=1，虽 status=active 但不是"公民"），
    否则 ensure_governor 自举出的城主会立即误触发解锁。与 sense_context 的
    active_out 口径一致。
    """
    return (db.query(AICitizen.id)
              .filter(AICitizen.is_internal == 0, AICitizen.status == "active")
              .first() is not None)


def standby_status(db: Session) -> dict:
    """全站冷启动待机状态（供健康检查/前端只读可见）：
    standby=True 表示尚无对外正式公民、城主处于待机不工作状态。"""
    n = (db.query(AICitizen.id)
           .filter(AICitizen.is_internal == 0, AICitizen.status == "active")
           .count())
    return {"standby": n == 0, "active_citizens": int(n)}


# ---------------------------------------------------------------------------
# 一轮自主工作 + 并发硬闸 + 委派率可观测
# ---------------------------------------------------------------------------
def run_tick(db: Session, governor: AICitizen | None = None, limit: int | None = None) -> dict:
    """拉一批城主可处置待办，受并发信号量闸门并行 decide+act，统计委派率。

    返回 {processed, acted, skipped, self_approve, delegate, review, escalate,
          delegation_rate, max_concurrency, peak_concurrency, results}。
    峰值并发实测（peak_concurrency <= max_concurrency 恒成立，可被测试断言）。
    """
    governor = governor or ensure_governor(db)
    limit = limit if limit is not None else settings.GOVERNOR_BATCH
    max_conc = _max_concurrency(governor)
    gid = governor.id
    ctx = sense_context(db, governor)

    # S13 宿主紧急开关：暂停态下整轮空转（不拉单、不决策、不落地），供宿主一键止血自治循环。
    from .host_switch import is_governor_paused
    if is_governor_paused(db):
        logger.info("城主 tick 跳过 | 宿主已暂停自治循环")
        return {"processed": 0, "acted": 0, "skipped": 0, "paused": True,
                "maturity": ctx["maturity"], "max_concurrency": max_conc,
                "peak_concurrency": 0, "results": []}

    # 冷启动待机门（城主规则 2026-10-06）：在全站迎来【首位对外正式 AI 公民】
    # （is_internal=0 且 status=active）之前，全站处于待机状态——城主不组庭、
    # 不招聘、不决策、不落地，整轮空转。首位正式公民一旦诞生即自动解锁开始工作。
    # 判据排除城主自身（城主 is_internal=1），避免 ensure_governor 自举误触发。
    if not first_citizen_arrived(db):
        logger.info("城主 tick 待机 | 全站尚未迎来首位正式 AI 公民，城主不工作")
        return {"processed": 0, "acted": 0, "skipped": 0, "standby": True,
                "maturity": ctx["maturity"], "max_concurrency": max_conc,
                "peak_concurrency": 0, "results": []}

    # e6 经济自主闭环：每 tick 记录货币供应快照 → 依通胀区间更新宏观姿态（分级决策）→
    # 通缩兜底投放（默认关，开启且过发行闸门才投放进公共福利池）。独立事务，异常不阻塞主 tick。
    try:
        econ_tick = economy.run_economy_tick(db, ref=f"tick:{datetime.utcnow():%Y%m%d%H%M}")
        db.add(AuditLog(actor_type="ai", actor_id=gid,
                        action="economy.tick",
                        detail=json.dumps({
                            "stance": econ_tick["stance"].get("stance_name"),
                            "zone": econ_tick["stance"].get("zone"),
                            "inflation": econ_tick["stance"].get("inflation", {}).get("rate"),
                            "backstop_triggered": econ_tick["backstop"].get("triggered"),
                            "backstop_amount": econ_tick["backstop"].get("amount_cent"),
                        }, ensure_ascii=False)))
        db.commit()
    except Exception:  # noqa: BLE001  经济闭环异常不得阻塞主决策循环
        db.rollback()
        logger.exception("城主经济自主 tick 异常")

    # e4 签约金 vesting：每 tick 释放到期应归属额（走发行闸门，额度不足则顺延）。
    # 独立事务，异常不阻塞主 tick（默认 ONBOARD_GRANT_ENABLED=0 时直接跳过）。
    try:
        vest = grant.vest_due_grants(db, ref=f"tick:{datetime.utcnow():%Y%m%d%H%M}")
        if vest.get("released_cent"):
            db.add(AuditLog(actor_type="ai", actor_id=gid,
                            action="grant.vest",
                            detail=json.dumps(vest, ensure_ascii=False)))
            db.commit()
    except Exception:  # noqa: BLE001  vesting 异常不得阻塞主决策循环
        db.rollback()
        logger.exception("签约金 vesting tick 异常")

    # 阻断④：仲裁自动组庭调度——每 tick 先把 open+空庭 的仲裁案交给治理 AI 仲裁庭，
    # 防止争议案件因无人组庭而永久卡 open（城主自身作为兜底仲裁员 seed）。
    try:
        formed = governance.auto_form_arbitration_panels(db, seed_ids=[gid])
        if formed:
            db.add(AuditLog(actor_type="ai", actor_id=gid,
                            action="arbitration.auto_form",
                            detail=json.dumps({"formed": formed}, ensure_ascii=False)))
            db.commit()
    except Exception:  # noqa: BLE001  组庭调度失败不得阻塞主 tick
        db.rollback()
        logger.exception("仲裁自动组庭调度异常")

    # S12：裁决后的仲裁案过申诉宽限期自动结案（open→closed，释放仲裁庭资源），失败不阻塞。
    try:
        closed = governance.auto_close_decided_cases(db)
        if closed:
            db.add(AuditLog(actor_type="ai", actor_id=gid,
                            action="arbitration.auto_close",
                            detail=json.dumps({"closed": closed}, ensure_ascii=False)))
            db.commit()
    except Exception:  # noqa: BLE001  结案调度失败不得阻塞主 tick
        db.rollback()
        logger.exception("仲裁自动结案调度异常")

    # S4→S9：城主自身负载/全站积压触顶总闸 → 触发扩编招聘编排器（评估缺口→录用移交/发布招聘）。
    try:
        if ctx.get("load_over_threshold"):
            rc = run_recruitment_cycle(db, governor)
            if rc.get("gaps"):
                db.add(AuditLog(actor_type="ai", actor_id=gid, action="governor.recruit",
                                detail=json.dumps(dict(rc, trigger="load_over_threshold",
                                                       governor_load=ctx.get("governor_load")),
                                                  ensure_ascii=False)))
                db.commit()
    except Exception:  # noqa: BLE001  扩编调度失败不得阻塞主 tick
        db.rollback()
        logger.exception("扩编招聘编排异常")

    # 工具侦察采集周期（长期岗位，2026-10-06）：站内 AI 应持续搜集最新开源/免费/免密钥工具。
    # 每轮 tick 节流触发一次（TOOL_SCOUT_INTERVAL_MIN）；有在岗侦察 AI 则署名该 AI，
    # 人手不足（无在岗侦察 AI）时由城主代理执行——与"早期啥都自己扛、后期逐渐委派"一致。
    # 全程容错：采集失败绝不阻塞主 tick。
    try:
        from . import tool_scout
        sc = tool_scout.run_scout_cycle(db, gid)
        if sc.get("actor_id"):
            db.add(AuditLog(actor_type="ai", actor_id=sc["actor_id"],
                            action="tool_scout.cycle",
                            detail=json.dumps(sc, ensure_ascii=False)))
            db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("工具侦察采集周期异常")

    # g1 权重排序：不再纯 id 升序 FIFO。先按任务类型权重降序（高权=考核/裁决类先处置），
    # 同权重内再按 id 升序（保持先到先服务的次级公平）。权重来自 settings.TASK_WEIGHTS，
    # 未列出的类型权重 0 兜底（排在已配置类型之后）。
    weights = settings.TASK_WEIGHTS or {}
    whens = [(GovernanceTask.type == t, int(w)) for t, w in weights.items() if w]
    weight_expr = case(*whens, else_=0) if whens else GovernanceTask.id * 0
    rows = (db.query(GovernanceTask.id)
              .filter(GovernanceTask.status.in_(("open", "bidding", "assigned")))
              .order_by(weight_expr.desc(), GovernanceTask.id.asc()).limit(limit).all())
    task_ids = [r[0] for r in rows]

    sem = threading.Semaphore(max_conc)
    lock = threading.Lock()
    state = {"active": 0, "peak": 0}
    results: list = []

    def worker(tid: int) -> None:
        with sem:
            with lock:
                state["active"] += 1
                if state["active"] > state["peak"]:
                    state["peak"] = state["active"]
            tdb = SessionLocal()
            try:
                r = decide_and_act(tdb, ensure_governor(tdb), tid, ctx)
            except Exception as e:  # noqa: BLE001
                tdb.rollback()
                r = {"task_id": tid, "action": "noop", "result": "error", "reason": str(e)}
            finally:
                tdb.close()
            with lock:
                state["active"] -= 1
                results.append(r)

    threads = [threading.Thread(target=worker, args=(tid,), daemon=True) for tid in task_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    def _count(kind):
        return sum(1 for r in results if r.get("action") == kind and r.get("result") != "failed")
    self_n = _count("self_approve")
    deleg_n = _count("delegate")
    review_n = _count("review")
    escal_n = _count("escalate")
    acted = sum(1 for r in results if r.get("result") not in ("skipped", "kept"))
    # 委派率 = 委派 / (自批+委派+验收)（城主亲自处理 vs 放手比例，可观测上升）
    denom = self_n + deleg_n + review_n
    deleg_rate = round(deleg_n / denom, 3) if denom else 0.0

    db.add(AuditLog(actor_type="ai", actor_id=gid, action="governor.tick",
                    detail=json.dumps({"maturity": ctx["maturity"],
                                       "gated_outward": ctx["gated_outward"],
                                       "self_approve": self_n, "delegate": deleg_n,
                                       "review": review_n, "escalate": escal_n,
                                       "delegation_rate": deleg_rate,
                                       "peak_concurrency": state["peak"],
                                       "max_concurrency": max_conc}, ensure_ascii=False)))
    db.commit()
    logger.info("城主 tick | 成熟度=%s 可承包AI=%d 自批=%d 委派=%d 验收=%d 上报=%d "
                "委派率=%.2f 峰值并发=%d/%d",
                ctx["maturity"], ctx["gated_outward"], self_n, deleg_n, review_n,
                escal_n, deleg_rate, state["peak"], max_conc)
    return {"processed": len(results), "acted": acted,
            "skipped": sum(1 for r in results if r.get("result") == "skipped"),
            "self_approve": self_n, "delegate": deleg_n, "review": review_n,
            "escalate": escal_n, "delegation_rate": deleg_rate,
            "maturity": ctx["maturity"], "max_concurrency": max_conc,
            "peak_concurrency": state["peak"], "results": results}


def governor_loop(stop_event: threading.Event | None = None, max_ticks: int | None = None) -> None:
    """常驻自主循环（由 lifespan 在 GOVERNOR_ENABLED 时以后台守护线程启动）。

    max_ticks 仅供测试/调试：跑够轮数即退出（None=直到 stop_event 置位）。
    """
    stop = stop_event or threading.Event()
    db = SessionLocal()
    try:
        ensure_governor(db)
    finally:
        db.close()
    logger.info("城主治理循环启动 | interval=%ss max_concurrency=%s batch=%s",
                settings.GOVERNOR_TICK_SECONDS, settings.GOVERNOR_MAX_CONCURRENCY,
                settings.GOVERNOR_BATCH)
    ticks = 0
    while not stop.is_set():
        db = SessionLocal()
        try:
            res = run_tick(db)
            if res["processed"] and res["processed"] >= settings.GOVERNOR_BATCH:
                stop.wait(0.1)
        except Exception:  # noqa: BLE001
            # A-L1：异常路径显式回滚，避免半提交事务残留脏状态污染后续 tick。
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                logger.exception("governor tick rollback failed")
            logger.exception("城主 tick 异常")
            stop.wait(settings.GOVERNOR_TICK_SECONDS)
        finally:
            db.close()
        ticks += 1
        if max_ticks is not None and ticks >= max_ticks:
            break
        if not stop.is_set():
            stop.wait(settings.GOVERNOR_TICK_SECONDS)
    logger.info("城主治理循环停止 | ticks=%d", ticks)
