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
"""能力定时强制复核 job（G10；能力评估 §六 规则 12 / 蓝图 §二 表 6、7）。

设计目标：能力证书 / verified_level 代表"某一时刻的实测水平"。证书会过期，
长期不复核的能力画像会与真实水平脱钩。本模块提供两类能力：

1. **定时强制复核 job**（每日扫描）：
   - 找出"过期"的有效证书：expires_at 已到期，或未设 expires_at 但 issued_at
     超过 CAPABILITY_RECHECK_DAYS 天（兜底旧数据无到期时间的情形）；
   - 对其触发"强制复核"：证书置 expired、verified_level 降一档、落治理任务
     提示重考，并写审计日志。

2. **降级 / 重考原语**（可复用，供未来 3.5 仲裁败诉联动调用）：
   - `request_capability_reexam(db, citizen_id, skill, reason, source, severity)`
     是本模块对外暴露的唯一降级入口：降一档 verified_level + 过期有效证书 +
     落重考治理任务 + 审计留痕。
   - 3.5 现状缺口：`submit_verdict` 仲裁败诉分支【尚未】连线本原语。
     未来接入方式 = 在 submit_verdict 败诉分支调用本函数（source="arbitration"），
     降档幅度可由 severity 配置；因果联动与阈值留作 P1 缺口（见设计缺口存档）。

幂等：job 经 scheduler.register_daily_job 的 (job_type, run_key) 唯一键日级幂等；
单个 (citizen, skill) 同一次扫描只处理一次。job 只 flush，commit 由调用方负责。

与信用分的边界：本模块只动 CapabilityProfile.verified_level / SkillCertificate.status，
**绝不**触碰 CreditProfile，避免"复核降档"与"信用扣分"双重惩罚（§3.5 设计约束）。
"""
import json
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from . import capability, governance
from .ai_judgment import ai_decide
from .config import settings
from .database import register_index
from .models import AuditLog, SkillCertificate

logger = logging.getLogger(__name__)

# 降档幅度系统提示：severity 是调用方声明的**上限**，AI 只能在此上限内选择档数。
# 证书单纯到期（source=expiry）默认只降一档，避免"到期=重罚"。
_DEMOTE_SYSTEM = (
    "You are the capability-review officer of an autonomous AI society. A verified "
    "capability level must be demoted after a forced re-check. You are given the "
    "current level, the reason/source, and the declared severity as FACTS. Decide how "
    "many levels to demote (at least 1, never more than the declared severity cap). "
    "Certificate expiry alone is routine -> 1 step; arbitration loss or repeated "
    "failure may warrant more within the cap.\n\n"
    "Respond ONLY with JSON:\n"
    '{"steps":1,"reasoning":"one sentence"}'
)

# 有效证书扫描索引：按 status + expires_at 过滤过期项
register_index(
    "CREATE INDEX IF NOT EXISTS idx_cert_status_expires "
    "ON skill_certificates(status, expires_at)"
)

# 强制复核时治理任务的预算（分）：提示重考，金额象征性
RECHECK_TASK_BUDGET_CENT = int(getattr(settings, "CAPABILITY_RECHECK_TASK_BUDGET_CENT", 100))

# 等级序反向表：由 capability.LEVEL_ORDER 派生（唯一真源）
_RANK_TO_LEVEL = {v: k for k, v in capability.LEVEL_ORDER.items()}


def _now() -> datetime:
    return datetime.utcnow()


def _demote_steps(verified_level: str, steps: int) -> str:
    """verified_level 降 steps 档（依据 capability.LEVEL_ORDER 的数值序）。

    未知等级按 unverified(0) 处理；已最低则维持不变（幂等，避免反复降级）。
    """
    rank = capability.LEVEL_ORDER.get(verified_level, 0)
    return _RANK_TO_LEVEL.get(max(0, rank - max(0, int(steps))), "unverified")


def _demote_one_step(verified_level: str) -> str:
    """verified_level 降一档（单步降档的便捷封装，也是 AI 不可用时的兜底）。"""
    return _demote_steps(verified_level, 1)


def _ai_demote_steps(from_level: str, skill: str, reason: str, source: str,
                     severity: int) -> int:
    """降档幅度：AI 在 severity 上限内决定档数，兜底为 1 档。

    上限 = clamp(severity, 1, 2)：调用方声明的 severity 是硬约束，AI 不能突破。
    """
    cap = max(1, min(2, int(severity or 1)))
    if cap <= 1:
        return 1
    obj = ai_decide(
        system=_DEMOTE_SYSTEM,
        prompt=json.dumps({"from_level": from_level, "skill": skill,
                           "reason": reason, "source": source,
                           "severity_cap": cap}, ensure_ascii=False),
        fallback={"steps": 1})
    try:
        steps = int(obj.get("steps", 1))
    except (TypeError, ValueError):
        steps = 1
    return max(1, min(cap, steps))


# ---------------- 降级 / 重考原语（对外唯一入口，供 3.5 复用） ----------------

def request_capability_reexam(db: Session, citizen_id: int, skill: str,
                              reason: str, source: str = "expiry",
                              severity: int = 1) -> dict:
    """强制复核原语：降一档能力 + 过期有效证书 + 落重考治理任务 + 审计留痕。

    这是本模块对外的唯一降级入口（G10 铺设，供未来 3.5 仲裁败诉联动复用）：
      - 3.5 现状缺口：submit_verdict 败诉分支尚未调用本函数；接入即传
        source="arbitration"、severity 视败诉严重度，降档幅度可按 severity 扩展。

    行为（只 flush，commit 由调用方负责）：
      1. 取该 (citizen, skill) 当前 verified_level，降一档写回（已 unverified 则维持）；
      2. 该 (citizen, skill) 全部 status="valid" 的证书置 "expired"（行保留不删）；
      3. 落 governance_tasks(type="compliance", params.stage="capability_reexam")，
         供治理市场消费 / 提示 AI 重考（沿用 capability 申报同款 type）；
      4. 写 audit_logs 留痕。

    返回处理结果 dict：{citizen_id, skill, from_level, to_level, demoted,
                        certs_expired, task_id, reason, source}。
    """
    profile = capability.get_profile(db, citizen_id, skill)
    from_level = profile.verified_level if profile else "unverified"
    steps = _ai_demote_steps(from_level, skill, reason, source, severity)
    to_level = _demote_steps(from_level, steps)
    demoted = (to_level != from_level)
    if demoted:
        capability.set_verified_level(db, citizen_id, skill, to_level)

    # 过期全部有效证书（行保留，仅置 expired；历史/已履约合约不受影响，规则 12）
    valid_certs = (db.query(SkillCertificate)
                     .filter(SkillCertificate.citizen_id == citizen_id,
                             SkillCertificate.skill == skill,
                             SkillCertificate.status == "valid")
                     .all())
    for c in valid_certs:
        c.status = "expired"

    # 落重考治理任务（compliance 类型，沿用 capability 申报先例）
    task = governance.publish_task(
        db, type_="compliance",
        params={"stage": "capability_reexam", "citizen_id": citizen_id,
                "skill": skill, "reason": reason, "source": source,
                "from_level": from_level, "to_level": to_level},
        budget_cent=RECHECK_TASK_BUDGET_CENT, deadline=None)

    db.add(AuditLog(actor_type="system", actor_id=0,
                    action="capability.recheck_downgrade",
                    detail=json.dumps({"citizen_id": citizen_id, "skill": skill,
                                       "from_level": from_level, "to_level": to_level,
                                       "reason": reason, "source": source,
                                       "certs_expired": len(valid_certs)},
                                      ensure_ascii=False)))
    db.flush()
    return {"citizen_id": citizen_id, "skill": skill,
            "from_level": from_level, "to_level": to_level,
            "demoted": demoted, "certs_expired": len(valid_certs),
            "task_id": task.id, "reason": reason, "source": source}


# ---------------- 过期扫描 ----------------

def scan_expired_certificates(db: Session, now: datetime | None = None) -> list:
    """返回需要强制复核的 (citizen_id, skill) 去重列表。

    C-D7 说明：只扫描 status="valid" 的证书——
      - "downgraded"：已被考试降档产生的新证替代，旧证虽保留但已失效，无需再复核；
      - "expired"：已过期待重考，不再二次扫描（幂等，避免重复触发降级）；
      - "revoked"：被吊销，不可恢复，无需复核。
    过期判定（取"有效"证书）：
      - expires_at 非空且 < now → 明确到期；
      - expires_at 为空但 issued_at 非空且 issued_at + CAPABILITY_RECHECK_DAYS < now
        → 旧数据无到期时间的兜底复核线。
    同一 (citizen, skill) 只出现一次（去重，避免同 skill 多证重复处理）。
    """
    now = now or _now()
    fallback_cutoff = now - timedelta(days=settings.CAPABILITY_RECHECK_DAYS)
    rows = (db.query(SkillCertificate.citizen_id, SkillCertificate.skill,
                     SkillCertificate.expires_at, SkillCertificate.issued_at)
              .filter(SkillCertificate.status == "valid")
              .all())
    seen = set()
    out = []
    for cid, skill, expires_at, issued_at in rows:
        expired = False
        if expires_at is not None and expires_at < now:
            expired = True
        elif expires_at is None and issued_at is not None and issued_at < fallback_cutoff:
            expired = True
        if expired and (cid, skill) not in seen:
            seen.add((cid, skill))
            out.append((cid, skill))
    return out


# ---------------- 日级 job ----------------

def run_capability_recheck(db: Session, now: datetime | None = None) -> dict:
    """扫描过期证书并逐个触发强制复核。返回汇总 {scanned, processed, tasks}。

    commit 由调用方（路由/调度循环）负责。
    """
    stale = scan_expired_certificates(db, now=now)
    processed = 0
    tasks = []
    for cid, skill in stale:
        res = request_capability_reexam(db, cid, skill,
                                        reason="证书到期，定时强制复核",
                                        source="expiry")
        processed += 1
        if res.get("task_id"):
            tasks.append(res["task_id"])
    return {"scanned": len(stale), "processed": processed, "tasks": tasks}


def capability_recheck_daily_job(db: Session, now: datetime | None = None) -> int:
    """scheduler 日级 job 入口（返回 0，run_due_jobs 以 task_id=0 记账幂等）。"""
    try:
        res = run_capability_recheck(db, now=now)
        if res.get("processed"):
            logger.info("capability_recheck: scanned=%d processed=%d",
                        res.get("scanned", 0), res.get("processed", 0))
    except Exception:  # noqa: BLE001  复核 job 失败绝不拖垮其余日快照
        logger.exception("capability_recheck daily job failed")
    return 0


# import 时注册日级任务（scheduler 插件模式，不改 scheduler.py）
from .scheduler import register_daily_job  # noqa: E402  避免与 scheduler 顶层循环依赖
register_daily_job("capability_recheck", capability_recheck_daily_job)
