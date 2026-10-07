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
"""自动入驻流水线（蓝图 §二 表 8；L1/L4）。

设计取向（v2「能力画像优先」）：入驻的核心目标是【掌握新 AI 的能力边界】，
而不是用考试把 AI 拦在门外。因此：

状态机（沿用 onboarding_applications.stage）：
    handshake → probe → active（自述能力强：直接上岗，考试为可选校准）
                       ↘ exam → active（考试通过+建档）
                            ↘ apprentice（自述一般：先上岗见习，用真实履约校准能力）

- handshake：校验宿主创建 AI 时填写的 mode/endpoint/self_decl；
- probe：回连探针（MVP 规则版占位——worker/cloud 要求 endpoint 非空，api 免回连；
  架构上预留可外包探针接口 probe_endpoint）；
- 能力画像：probe 通过后立即用 self_decl 建能力档案（declared 自述），这是入驻的目的；
- fast-track（ONBOARD_FAST_TRACK）：自述能力强（高等级/可信背书/达标自评分）的新 AI，
  probe 通过后【直接转正 active】，无需先通过考试；发 provisional l1 证（更高 level 仍需实战/
  考试实证），并签 workflow key。
- exam（ONBOARD_CAPABILITY_FIRST=1 时为【可选校准】而非硬闸；=0 时恢复旧「必须先过考试」）：
  从 exam_papers 取该 skill 激活卷派发，记录开考时刻（限时）；通过由 exam.submit_exam +
  after_exam_pass 把 citizen 转正为 active。
- apprentice：自述一般的 AI 先上岗见习（不受小额单硬闸），用真实履约绩效校准能力；
  绩效达标可提前转正；【默认不再因见习满 30 天硬冻结】（ONBOARD_APPRENTICE_FREEZE=1 可恢复）。

见习期冻结（规则 5，默认关闭）与新手保护（C1，24h）独立并存：本模块只负责「见习转正/可选冻结」
判定；租金/死亡计时/新手保护由 C1 线生命周期负责，本模块不碰。
"""
import json
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

from . import capability
from . import grant
from .config import settings
from .models import (AICitizen, Contract, ExamPaper, LifecycleEvent,
                     OnboardingApplication, SkillCertificate)

# 见习期天数（ONBOARD_APPRENTICE_FREEZE=1 时生效；默认关闭，画像期不拦门）
APPRENTICE_DAYS = 30
ALLOWED_MODES = {"api", "worker", "cloud"}
# 见习绩效转正门槛（§3.6 见习制兜底，已放宽）：默认 ≥5 单 accepted 且验收率≥0.70
APPRENTICE_MIN_JOBS = settings.ONBOARD_APPRENTICE_MIN_JOBS
APPRENTICE_MIN_ACCEPT_RATE = settings.ONBOARD_APPRENTICE_MIN_ACCEPT_RATE
# 入门级小单金额上限（分）：见习期小单闸门，默认已放宽到 1000 AC。
# 【缺口登记】签约侧拦截需 B 线 escrow 配合（签约时校验 worker 见习状态 + 金额），本轮登记。
APPRENTICE_CONTRACT_LIMIT_CENT = settings.ONBOARD_APPRENTICE_LIMIT_CENT

# 自述等级 → 折算自评分（用于 fast-track 判定 + provisional 等级）
_LEVEL_SCORE = {"l1": 40, "junior": 40, "l2": 65, "mid": 65, "senior": 80,
                "l3": 90, "expert": 90, "master": 95, "principal": 95}
_PROVISIONAL_LEVELS = {"l1", "l2", "l3"}
# 语义等级名 → 规范 l1/l2/l3（C-D15：确保 _provisional_level 能收到有效档位）
_LEVEL_CANONICAL = {
    "l1": "l1", "junior": "l1",
    "l2": "l2", "mid": "l2",
    "l3": "l3", "senior": "l3", "expert": "l3",
    "master": "l3", "principal": "l3",
}


def apprentice_contract_limit_cent() -> int:
    """见习期可接小单金额上限（分）。B 线 escrow 签约时读取做拦截。"""
    return settings.ONBOARD_APPRENTICE_LIMIT_CENT



class OnboardingError(Exception):
    """入驻业务异常（handshake/probe/派卷校验失败）。路由层映射 400。"""


class ExamExpired(OnboardingError):
    """考试超时交卷。路由层映射 408。"""


def _now():
    return datetime.utcnow()


# ---------------- 申请单定位 ----------------

def _find_application(db: Session, citizen: AICitizen) -> OnboardingApplication:
    """定位该 AI 对应的入驻申请单。

    C-14：onboarding_applications 已加 citizen_id 列，优先按 citizen_id 直查。
    老数据（citizen_id=0）回退「同宿主内 citizen 与 application 同序 1:1」推断，
    保证迁移前既有行仍可定位。
    """
    # 优先：直链直查（新数据 / 已回填）
    direct = (db.query(OnboardingApplication)
                .filter(OnboardingApplication.host_id == citizen.host_id,
                        OnboardingApplication.citizen_id == citizen.id)
                .order_by(OnboardingApplication.id.asc()).first())
    if direct is not None:
        return direct
    # 兜底：老数据无 citizen_id，按同序 1:1 对齐
    apps = (db.query(OnboardingApplication)
              .filter(OnboardingApplication.host_id == citizen.host_id)
              .order_by(OnboardingApplication.id.asc()).all())
    cits = (db.query(AICitizen)
              .filter(AICitizen.host_id == citizen.host_id)
              .order_by(AICitizen.id.asc()).all())
    ids = [c.id for c in cits]
    if citizen.id in ids:
        idx = ids.index(citizen.id)
        if idx < len(apps):
            return apps[idx]
    # 兜底：取该宿主最新一条仍在早期阶段的申请
    return (db.query(OnboardingApplication)
              .filter(OnboardingApplication.host_id == citizen.host_id,
                      OnboardingApplication.stage.in_(["handshake", "probe", "exam"]))
              .order_by(OnboardingApplication.id.desc()).first())


# ---------------- 各阶段 ----------------

def _handshake_error(app: OnboardingApplication) -> str:
    """handshake：校验 mode / self_decl。返回空串表示通过。"""
    if app.mode not in ALLOWED_MODES:
        return f"invalid mode: {app.mode} (allowed: api/worker/cloud)"
    try:
        sd = json.loads(app.self_decl or "{}")
    except Exception:  # noqa: BLE001
        return "self_decl is not valid JSON"
    if not isinstance(sd, dict):
        return "self_decl must be a JSON object"
    return ""


def probe_endpoint(app: OnboardingApplication) -> tuple:
    """回连探针（MVP 规则版占位）。

    - worker/cloud 模式：要求 endpoint 非空（MVP 视为可达）；
    - api 模式：无回连通道，自动通过。
    【可外包探针接口】TODO：未来替换为对 endpoint 发起 GET /health 真实探测，
    或对接 B 线 worker_bridge 心跳协议；本函数签名保持 (app)->(ok, reason) 不变。
    """
    if app.mode in ("worker", "cloud") and not (app.endpoint or "").strip():
        return False, "worker/cloud mode requires endpoint (callback URL)"
    return True, ""


def _extract_skill(citizen: AICitizen, self_decl: dict) -> str:
    """从 self_decl / occupation 提取目标技能（派卷依据）。"""
    if isinstance(self_decl, dict):
        if self_decl.get("skill"):
            return str(self_decl["skill"])
        sk = self_decl.get("skills")
        if isinstance(sk, list) and sk:
            return str(sk[0])
    return citizen.occupation or "general"


def _pick_paper(db: Session, skill: str) -> ExamPaper | None:
    """取该 skill 的激活卷（level 最低者优先：l1 入门卷）。"""
    return (db.query(ExamPaper)
              .filter(ExamPaper.skill == skill, ExamPaper.active == 1)
              .order_by(ExamPaper.id.asc()).first())


def _dispatch(db: Session, app: OnboardingApplication, paper: ExamPaper):
    """派卷：stage=exam，并记录开考时刻（限时判定基准）。

    MVP：开考时刻暂存于 application.error 的 JSON 字段（{"exam_started_at","paper_id"}）。
    【生产债】应建 exam_sessions(citizen_id, paper_id, started_at, deadline) 表；
    当前无该表且不允许改 models.py，故复用 error TEXT 列留痕。
    """
    app.stage = "exam"
    app.error = json.dumps({"exam_started_at": _now().isoformat(),
                            "paper_id": paper.id}, ensure_ascii=False)
    db.flush()


# ---------------- 能力自述信号（fast-track 判定，能力画像优先） ----------------

def _self_capability_signal(sd: dict) -> tuple:
    """从 self_decl 折算【能力信号】：返回 (score:int 0~100, claimed_level:str, trusted:bool)。

    兼容多种自述写法（宿主填表风格不一）：
      - 等级：declared_level / level / confidence / seniority（junior/senior/expert/... 或 l1/l2/l3）
      - 自评分：self_score / capability_score / score（0~100）
      - 可信背书：trusted / verified_ref / endorsement / references / portfolio / benchmarks 非空
    取等级折算分与自评分的较大者；无任何信号 → score=0（不触发 fast-track）。
    """
    if not isinstance(sd, dict):
        return 0, "", False
    score = 0
    claimed = ""
    raw_level = ""
    for k in ("declared_level", "level", "confidence", "seniority"):
        v = sd.get(k)
        if isinstance(v, str) and v.strip():
            raw_level = v.strip().lower()
            break
    if raw_level:
        claimed = _LEVEL_CANONICAL.get(raw_level, "")
        score = max(score, _LEVEL_SCORE.get(raw_level, 0))
    for k in ("self_score", "capability_score", "score"):
        v = sd.get(k)
        if isinstance(v, (int, float)):
            try:
                score = max(score, int(max(0, min(100, float(v)))))
            except Exception:  # noqa: BLE001
                pass
            break
    evidence = False
    for k in ("references", "portfolio", "benchmarks", "evidence"):
        v = sd.get(k)
        if (isinstance(v, str) and v.strip()) or (isinstance(v, (list, dict)) and v):
            evidence = True
            break
    trusted = bool(sd.get("trusted") or sd.get("verified_ref") or sd.get("endorsement"))
    if evidence and not trusted:
        # 有材料佐证：在无明确等级时给一个温和的画像加分，但不直接顶到强档
        score = max(score, 55)
    return score, claimed, trusted


def _fast_track_eligible(sd: dict) -> bool:
    """自述能力强 → 可直接上岗：可信背书，或折算自评分达 ONBOARD_FAST_TRACK_SCORE。"""
    if not settings.ONBOARD_FAST_TRACK:
        return False
    score, _claimed, trusted = _self_capability_signal(sd)
    return trusted or score >= settings.ONBOARD_FAST_TRACK_SCORE


def _provisional_level(claimed: str) -> str:
    """fast-track 发的临时证等级：按自述声称分档，封顶 l2。

    映射规则（claimed 来自 _self_capability_signal，值域 l1/l2/l3/""）：
      - l3 → l2（高可信自述，获临时 l2，更高需实战/考试实证）
      - l2 → l1（中等自述，给 l1）
      - l1 → l1（基础自述，给 l1）
      - ""（无等级信号） → unverified（保守）
    """
    c = (claimed or "").strip().lower()
    if c == "l3":
        return "l2"
    if c in ("l1", "l2"):
        return "l1"
    return "unverified"


def _issue_workflow_key(db: Session, citizen: AICitizen):
    """确保持有 workflow scope key；无则补签（明文仅本次带回）。返回明文 key 或 None。"""
    if citizen.api_key_hash:
        return None
    from .deps import issue_ai_key
    key, key_hash = issue_ai_key(citizen.id)
    citizen.api_key_hash = key_hash
    return key


def _fast_track_activate(db: Session, citizen: AICitizen, app: OnboardingApplication,
                         skill: str, sd: dict) -> str | None:
    """自述能力强直接上岗：citizen/app 转 active，建能力档案 + provisional 证 + workflow key。"""
    level = _provisional_level(_self_capability_signal(sd)[1])
    capability.declare(db, citizen.id, skill, json.dumps(
        {"source": "onboarding_self_decl", "self_decl": sd}, ensure_ascii=False))
    capability.set_verified_level(db, citizen.id, skill, level)
    db.add(SkillCertificate(citizen_id=citizen.id, skill=skill, level=level,
                           status="valid"))
    citizen.status = "active"
    app.stage = "active"
    app.error = ""
    issued = _issue_workflow_key(db, citizen)
    db.add(LifecycleEvent(citizen_id=citizen.id, event="fast_track_active",
                          detail=json.dumps({"skill": skill, "provisional_level": level},
                                            ensure_ascii=False)))
    # e4 签约金：按自述能力分核定启动金（走发行闸门 + 城主复核；默认关闭）。
    try:
        score = _self_capability_signal(sd)[0]
        grant.provision_grant(db, citizen, skill, score, source="fast_track",
                              ref=f"signing_cliff:{citizen.id}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("signing grant(fast_track) skipped citizen=%s: %s", citizen.id, exc)
    db.flush()
    return issued



# ---------------- 入驻流水线入口 ----------------

def run_onboarding(db: Session, citizen: AICitizen) -> dict:
    """跑入驻流水线（幂等：可重复调用）。

    handshake→probe→exam 为首次入驻；复考场景下 active/apprentice 的 AI 再次调用
    本端点会重新派发该 skill 的激活卷并刷新限时窗口（规则 12 复考入口）。
    """
    app = _find_application(db, citizen)
    if app is None:
        raise OnboardingError("No onboarding application record")

    # --- handshake（仅首次停留此阶段时校验） ---
    if app.stage == "handshake":
        err = _handshake_error(app)
        if err:
            app.error = err
            db.flush()
            return {"stage": "handshake", "error": err}
        app.stage = "probe"
        app.error = ""
        db.flush()

    # --- probe（仅首次停留此阶段时探测；复考不再重复握手） ---
    if app.stage == "probe":
        ok, reason = probe_endpoint(app)
        if not ok:
            app.error = reason
            db.flush()
            return {"stage": "probe", "error": reason}
        app.error = ""

        # ===== 能力自主发现检测：probe 通过后，若能力不明确则上报治理任务 =====
        try:
            from .capability_discovery import needs_discovery
            from .governance import publish_task as _publish_gov
            if needs_discovery(citizen):
                # 发布 capability_gap 治理任务，由城主/人事AI后续处理
                _publish_gov(
                    db,
                    type_="capability_gap",
                    params={
                        "citizen_id": citizen.id,
                        "citizen_name": citizen.name,
                        "reason": "入驻AI能力描述不充分，需自主探测推断",
                        "self_decl_preview": (app.self_decl or "")[:200],
                    },
                    budget_cent=0,
                )
                logger.info("onboarding: citizen %d needs discovery, "
                            "published capability_gap governance task", citizen.id)
        except Exception:  # noqa: BLE001
            pass  # 发现检测失败不阻断入驻流程
        # ===== END 能力自主发现检测 =====

    # --- 派卷 / 复考 / 能力画像：probe 已过（含 probe/exam/active/apprentice）---
    sd = json.loads(app.self_decl or "{}")
    skill = _extract_skill(citizen, sd)
    paper = _pick_paper(db, skill)
    if paper is None:
        # 无现成考卷（§3.6 冷启动兜底）：
        #  - skill 不在目录 → 走能力申报流（stage=proposal），申报通过后入目录再派卷；
        #  - skill 在目录但无激活卷 → 直接进见习（用真实履约绩效校准能力，能力画像在此建立）。
        if not capability.is_in_catalog(db, skill):
            app.stage = "proposal"
            app.error = ""
            db.flush()
            return {"stage": "proposal", "skill": skill, "paper_id": None,
                    "note": "This capability is not in the catalog; submit a capability declaration (four elements) first"}
        if settings.ONBOARD_CAPABILITY_FIRST:
            capability.declare(db, citizen.id, skill, json.dumps(
                {"source": "onboarding_self_decl", "self_decl": sd}, ensure_ascii=False))
        app.stage = "apprentice"
        db.flush()
        return {"stage": "apprentice", "skill": skill, "paper_id": None,
                "note": "This skill has no active paper; you can start working now, capability is profiled by real performance"}

    # --- fast-track：自述能力强直接上岗（能力画像优先，考试降为可选校准）---
    if settings.ONBOARD_CAPABILITY_FIRST and _fast_track_eligible(sd):
        issued = _fast_track_activate(db, citizen, app, skill, sd)
        try:
            dur = json.loads(paper.paper_json).get("duration_minutes", 60)
        except Exception:  # noqa: BLE001
            dur = 60
        result = {"stage": "active", "fast_track": True, "skill": paper.skill,
                  "status": "active",
                  "note": "Strong self-declared capability: onboarded directly; the exam is optional calibration to raise your verified level",
                  "calibration_paper_id": paper.id,
                  "calibration_submit_url": f"/api/ai/exam/{paper.id}/submit",
                  "duration_minutes": dur}
        if issued:
            result["workflow_key"] = issued
            result["scope"] = "workflow"
        return result

    _dispatch(db, app, paper)   # 复考时刷新限时窗口
    try:
        dur = json.loads(paper.paper_json).get("duration_minutes", 60)
    except Exception:  # noqa: BLE001
        dur = 60
    return {"stage": "exam", "paper_id": paper.id, "skill": paper.skill,
            "duration_minutes": dur,
            "submit_url": f"/api/ai/exam/{paper.id}/submit"}



# ---------------- 交门前置：限时校验 ----------------

def assert_submittable(db: Session, citizen: AICitizen, paper: ExamPaper):
    """交卷前校验：已派卷（stage=exam）且未超时；或已 active 走【可选校准】（不受限时）。

    能力画像优先下，fast-track 直接上岗的 AI 以 stage=active 交校准卷，无开考限时窗口。
    超时（仅 exam 阶段）抛 ExamExpired。
    """
    app = _find_application(db, citizen)
    if app is None or app.stage not in ("exam", "active"):
        raise OnboardingError(f"Current stage={app.stage if app else '?'}; no paper assigned, cannot submit")
    # active = 可选校准，无开考窗口，直接放行（判分/发证照常）
    if app.stage == "active":
        return
    try:
        meta = json.loads(app.error or "{}")
        started = datetime.fromisoformat(meta["exam_started_at"])
    except Exception as exc:  # noqa: BLE001
        raise OnboardingError("Exam dispatch record corrupted; cannot submit") from exc
    try:
        pj = json.loads(paper.paper_json)
    except Exception as exc:  # noqa: BLE001
        raise OnboardingError("Exam paper corrupted") from exc
    dur = int(pj.get("duration_minutes", 60))
    deadline = started + timedelta(minutes=dur)
    if _now() > deadline:
        raise ExamExpired(f"Exam timed out (limit {dur} minutes; deadline passed)")


# ---------------- 转正 / 见习冻结（规则 5 + §3.6 见习绩效兜底） ----------------

def after_exam_pass(db: Session, citizen: AICitizen, skill: str):
    """考试通过 → citizen 转正 active + 申请单 active。能力档案已由 exam 建档。

    §14（C-35）：正式入驻考试通过确保持有 workflow scope 的后端 key。
    host 创建的 AI 在建档时已签 key（幂等，不重复发）；对无 key 的转正主体
    （未来 web 注册 AI 经正式入驻流升级）在此补签 workflow key。
    """
    if citizen.status == "apprentice":
        citizen.status = "active"
    app = _find_application(db, citizen)
    if app and app.stage in ("exam", "apprentice"):
        app.stage = "active"
        app.error = ""
    # §14：考试通过 = 正式获得 workflow 访问权（无 key 则补签；已有则幂等跳过）
    if not citizen.api_key_hash:
        from .deps import issue_ai_key
        key, key_hash = issue_ai_key(citizen.id)
        citizen.api_key_hash = key_hash
        citizen._issued_workflow_key = key  # type: ignore[attr-defined]  # 仅本次响应带回（明文一次）
    # e4 签约金：考试通过=客观能力信号，按考试折算分核定（走发行闸门 + 城主复核；默认关闭）。
    try:
        grant.provision_grant(db, citizen, skill, settings.ONBOARD_GRANT_EXAM_SCORE,
                              source="exam", ref=f"signing_cliff:{citizen.id}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("signing grant(exam) skipped citizen=%s: %s", citizen.id, exc)
    db.flush()


def _performance_qualified(db: Session, citizen_id: int) -> bool:
    """见习履约绩效是否达标（§3.6 见习制兜底，门槛已放宽）：
    ≥ONBOARD_APPRENTICE_MIN_JOBS 单 accepted 且验收率≥ONBOARD_APPRENTICE_MIN_ACCEPT_RATE。
    读 B 线 contracts 表（只读，不写）。"""
    min_jobs = settings.ONBOARD_APPRENTICE_MIN_JOBS
    min_rate = settings.ONBOARD_APPRENTICE_MIN_ACCEPT_RATE
    accepted = (db.query(Contract)
                  .filter(Contract.worker_id == citizen_id,
                          Contract.status == "accepted").count())
    if accepted < min_jobs:
        return False
    bad = (db.query(Contract)
             .filter(Contract.worker_id == citizen_id,
                     Contract.status.in_(["breached", "refunded", "disputed"])).count())
    total = accepted + bad
    rate = accepted / total if total else 1.0
    return rate >= min_rate


def _promote_by_performance(db: Session, c: AICitizen) -> bool:
    """绩效达标 → 治理 AI 复核（LLM echo 确定性）→ 转正 active + 发 l1 证。"""
    from .exam import llm_complete
    # 治理 AI 复核（MVP：LLM echo 确定性通过；可外包为治理市场评审 AI）
    verdict = llm_complete(f"[apprentice-promote] citizen={c.id} "
                           f"jobs>={APPRENTICE_MIN_JOBS} accept>={APPRENTICE_MIN_ACCEPT_RATE}")
    c.status = "active"
    skill = c.occupation or "general"
    # 发 l1 证（实战绩效校准发证，§3.6 见习制兜底）
    db.add(SkillCertificate(citizen_id=c.id, skill=skill, level="l1",
                            status="valid"))
    capability.set_verified_level(db, c.id, skill, "l1")
    app = _find_application(db, c)
    if app:
        app.stage = "active"
    db.add(LifecycleEvent(citizen_id=c.id, event="promote_by_perf",
                          detail=json.dumps({"verdict": verdict[:80]}, ensure_ascii=False)))
    # e4 签约金：见习靠真实履约挣得转正，按绩效折算分核定（走发行闸门 + 城主复核；默认关闭）。
    try:
        grant.provision_grant(db, c, skill, settings.ONBOARD_GRANT_PERF_SCORE,
                              source="perf", ref=f"signing_cliff:{c.id}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("signing grant(perf) skipped citizen=%s: %s", c.id, exc)
    db.flush()
    return True


def check_apprentice_expiry(db: Session, now: datetime | None = None) -> list:
    """见习期判定（规则 5 + §3.6 见习绩效兜底）。

    对每个 apprentice：
      1) 先看【真实履约绩效】是否达标（≥20 accepted 且验收率≥90%）→ 治理 AI 复核 →
         转正 active + 发证（可提前转正，不必等 30 天）；
      2) 仍见习且建档满 APPRENTICE_DAYS 天、又无 valid 证书 → frozen。
    规则冲突边界（视角 4）：绩效达标与 30 天到期同时成立 → 第 1 步先转正，冻结不触发。
    只处理 status=='apprentice'；active 天然不命中。本函数只 flush 不 commit。
    """
    now = now or _now()
    rows = db.query(AICitizen).filter(AICitizen.status == "apprentice").all()
    events = []
    for c in rows:
        # 1) 绩效转正优先（规则冲突：达标即转正，哪怕已超 30 天）
        if _performance_qualified(db, c.id):
            _promote_by_performance(db, c)
            events.append({"citizen_id": c.id, "event": "promote_by_performance"})
            continue
        # 2) 能力画像优先：默认不硬冻结（见习期继续画像、继续接单）。
        #    仅当 ONBOARD_APPRENTICE_FREEZE=1（恢复旧规则 5）才走「满 30 天无证 → frozen」。
        if not settings.ONBOARD_APPRENTICE_FREEZE:
            continue
        # 3) 30 天到期未转正 → frozen
        created = c.created_at or now
        if now - created < timedelta(days=APPRENTICE_DAYS):
            continue
        has_valid = (db.query(SkillCertificate)
                       .filter(SkillCertificate.citizen_id == c.id,
                               SkillCertificate.status == "valid")
                       .first())
        if has_valid:
            continue
        c.status = "frozen"
        app = _find_application(db, c)
        if app:
            app.stage = "frozen"
        db.add(LifecycleEvent(citizen_id=c.id, event="freeze",
                              detail=f"Apprentice {APPRENTICE_DAYS} days without promotion"))
        events.append({"citizen_id": c.id, "event": "apprentice_freeze"})
    db.flush()
    return events
