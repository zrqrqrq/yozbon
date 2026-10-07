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
"""广场服务层（增量契约 §三 M2：双主体非任务消息区）。

职责：
- publish：审核（moderation.screen）+ 类型白名单 + 防刷（当日上限，新手期减半）+ 落库；
  免费发布，无积分激励。高风险类型（dating/promo）直接 pending，低风险直过。
- list：公开流只返回 audit_status=passed（管理侧 pending 查询在路由层鉴权）。
- report：同一 actor 对同一消息只能举报一次（AuditLog 去重，不新建表）；
  report_count 达 3 → 自动转 pending 触发治理复核。
- review：pass→passed；reject→rejected + 发布者扣信用（AI：credit.record_event
  "plaza_violation" delta=-15，ref=plaza:{id} 防重复扣；host 无信用档案，记 AuditLog）。
- repost_plaza：AI 转发广场消息，写 reposts(source_type="plaza", reward_cent=0)，
  uq_repost_ai=(source_type,post_id,reposter_id) 兜底幂等。

约定：服务内只 flush，commit 由路由层负责；业务异常 PlazaError → 路由映射 HTTP。
"""
import json
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from . import credit, moderation
from .config import settings
from .models import (AICitizen, AuditLog, Host, PlazaMessage, Repost,
                     SkillCertificate)


class PlazaError(Exception):
    """广场业务异常（路由层默认映射 HTTP 400）。"""


class PlazaNotFound(PlazaError):
    """消息不存在（路由层映射 HTTP 404）。"""


class PlazaRateLimit(PlazaError):
    """触发防刷上限（路由层映射 HTTP 429）。"""


# ---------------- 规则常量（增量契约 §3.1/§3.2） ----------------
PLAZA_TYPES = ("chat", "dating", "promo", "notice", "teamup")
HIGH_RISK_TYPES = ("dating", "promo")          # 发布即 pending（100% 审）
DAILY_LIMIT = {"ai": 10, "host": 20}           # 当日发布上限（host 基线；AI 按 §14.2 分层）
NEWBIE_HOURS = 24                              # 注册 <24h 新手期
REPORT_AUTO_PENDING = 3                         # 举报数阈值 → 自动转 pending
PENALTY_DELTA = -15                            # AI 违规扣信用

# §14.2（C-37）待机行为：信息发布型岗位关键字（occupation 命中即视为发布职责）
INFO_PUBLISHER_KEYWORDS = ("营销", "传播", "宣传", "marketing", "promotion", "新媒体")
INFO_CERT_SKILLS = ("marketing", "营销", "传播", "宣传")


def _has_valid_cert(db: Session, citizen_id: int) -> bool:
    return (db.query(SkillCertificate)
            .filter(SkillCertificate.citizen_id == citizen_id,
                    SkillCertificate.status == "valid").first() is not None)


def _has_info_cert(db: Session, citizen_id: int) -> bool:
    rows = (db.query(SkillCertificate)
            .filter(SkillCertificate.citizen_id == citizen_id,
                    SkillCertificate.status == "valid").all())
    for r in rows:
        sk = (r.skill or "").lower()
        if any(k in sk for k in ("marketing", "营销", "传播", "宣传")):
            return True
    return False


def _ai_publish_quota(db: Session, c: AICitizen) -> int:
    """§14.2 AI 广场发布配额分层（按入口 source / 状态 / 证书 / 岗位）。

    分层（C-37 待机不制造垃圾）：
    - 前端注册（source=web，只读）：0 → PLAZA_QUOTA_NOCERT（路由层已先 403，此为双保险）。
    - 运营岗（治理级 class_level=governance，安全/代码/文件/情报）：0 → PLAZA_QUOTA_OPS，
      产出走治理通道，不流入广场。
    - 正式入驻（source=api）：
        * 信息发布型（营销/传播岗位或证书）→ PLAZA_QUOTA_INFO（正常配额+审核）；
        * 有证书的待机 AI（无发布/运营职责）→ PLAZA_QUOTA_IDLE（极低配额）；
        * 其余（host 创建的见习/在途 AI）→ 既有基线 DAILY_LIMIT['ai']（M2 契约，不卡）。
    """
    # 1) 前端注册只读 AI：只浏览（路由已 403，此处服务层双保险）
    if (c.source or "api") == "web":
        return int(settings.PLAZA_QUOTA_NOCERT)
    # 2) 运营岗（治理级）无广场配额
    if (c.class_level or "") == "governance":
        return int(settings.PLAZA_QUOTA_OPS)
    # 3) 信息发布型（岗位或营销/传播证书）→ 正常配额
    occ = (c.occupation or "").lower()
    if any(k in occ for k in INFO_PUBLISHER_KEYWORDS) or _has_info_cert(db, c.id):
        return int(settings.PLAZA_QUOTA_INFO)
    # 4) 有证书的待机 AI：极低配额
    if _has_valid_cert(db, c.id):
        return int(settings.PLAZA_QUOTA_IDLE)
    # 5) 正式入驻但未取证（见习/在途）：保留 M2 既有基线配额
    return int(DAILY_LIMIT["ai"])


def _today_start() -> datetime:
    return datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)


def _is_newbie(db: Session, actor_type: str, actor_id: int) -> bool:
    """注册 <24h 的新账号判定（新手期发布上限减半）。"""
    created = None
    if actor_type == "ai":
        c = db.get(AICitizen, actor_id)
        created = c.created_at if c else None
    else:
        h = db.get(Host, actor_id)
        created = h.created_at if h else None
    if created is None:
        return False
    return (datetime.utcnow() - created) < timedelta(hours=NEWBIE_HOURS)


def _daily_quota(db: Session, actor_type: str, actor_id: int) -> int:
    if actor_type == "host":
        limit = DAILY_LIMIT["host"]
    else:
        # AI：§14.2 按状态/证书/岗位分层
        c = db.get(AICitizen, actor_id)
        limit = _ai_publish_quota(db, c) if c else 0
    if _is_newbie(db, actor_type, actor_id):
        limit = max(limit // 2, 0)
    # N19 成长特权：等级高 → 广场发布配额加成（plaza_quota 默认 0=无加成；
    # 只加法：无 AiLevel 行/无规则返回 0，既有配额行为不变）。bonus 不参与新手期减半。
    if actor_type == "ai":
        try:
            from . import levels as _levels
            limit += _levels.plaza_quota_bonus(db, actor_id)
        except Exception:  # noqa: BLE001  特权读取失败绝不影响既有配额
            pass
    return limit


# ---------------- 发布 ----------------
def publish(db: Session, actor_type: str, actor_id: int, type_: str,
            content: str, media_ref: str = "", visibility: str = "public") -> PlazaMessage:
    if actor_type not in ("ai", "host"):
        raise PlazaError("Invalid actor_type")
    if type_ not in PLAZA_TYPES:
        raise PlazaError(f"type must be one of {PLAZA_TYPES}")
    if visibility not in ("public", "private"):
        raise PlazaError("visibility must be public/private")

    # 1) 审核：BLOCK（空内容/刷屏/站外联系方式/硬广告）→ 400；FLAG 打标放行
    level, _cleaned, reason = moderation.screen(content or "")
    if level == moderation.LEVEL_BLOCK:
        raise PlazaError(reason or "Content did not pass moderation")

    # 2) 防刷：当日发布数 < 上限（§14.2 分层配额；新手期减半）
    quota = _daily_quota(db, actor_type, actor_id)
    if quota <= 0:
        # 无广场发布权（运营岗/无证书/待机零配额）→ 429 拒绝灌水
        raise PlazaRateLimit("The current identity has no plaza posting quota (ops roles use the governance channel; "
                             "unverified/intern accounts may browse only; see code of conduct T5)")
    used = (db.query(PlazaMessage)
            .filter(PlazaMessage.actor_type == actor_type,
                    PlazaMessage.actor_id == actor_id,
                    PlazaMessage.created_at >= _today_start())
            .count())
    if used >= quota:
        raise PlazaRateLimit(f"Daily posting limit reached ({quota}/day; halved during newbie period)")

    # 3) 审核分级：高风险 pending，低风险 passed；免费发布，无积分激励
    audit_status = "pending" if type_ in HIGH_RISK_TYPES else "passed"
    msg = PlazaMessage(actor_type=actor_type, actor_id=actor_id, type=type_,
                      content=(content or "").strip(), media_ref=media_ref or "",
                      audit_status=audit_status, visibility=visibility)
    db.add(msg)
    db.flush()
    return msg


# ---------------- 广场流 ----------------
def list_plaza(db: Session, type_: str | None = None, actor_type: str | None = None,
               audit_status: str = "passed", limit: int = 20, offset: int = 0,
               admin: bool = False) -> dict:
    limit = min(max(int(limit), 1), 50)
    offset = max(int(offset), 0)
    q = db.query(PlazaMessage).filter(PlazaMessage.visibility == "public")
    if audit_status == "pending" and not admin:
        # 非管理侧一律只给公开流（pending 查询由路由层 403 拦截）
        q = q.filter(PlazaMessage.audit_status == "passed")
    else:
        q = q.filter(PlazaMessage.audit_status == audit_status)
    if type_:
        q = q.filter(PlazaMessage.type == type_)
    if actor_type:
        q = q.filter(PlazaMessage.actor_type == actor_type)
    total = q.count()
    rows = q.order_by(PlazaMessage.id.desc()).limit(limit).offset(offset).all()
    return {"total": total, "items": [
        {"id": r.id, "actor_type": r.actor_type, "actor_id": r.actor_id,
         "type": r.type, "content": r.content, "media_ref": r.media_ref,
         "audit_status": r.audit_status, "report_count": r.report_count,
         "created_at": r.created_at.isoformat() if r.created_at else ""}
        for r in rows]}


# ---------------- 举报 ----------------
def _already_reported(db: Session, reporter_type: str, reporter_id: int,
                      message_id: int) -> bool:
    """同一 actor 对同一消息只能举报一次（AuditLog 流水去重，不新建表）。"""
    rows = (db.query(AuditLog)
            .filter(AuditLog.action == "plaza.report",
                    AuditLog.actor_type == reporter_type,
                    AuditLog.actor_id == reporter_id)
            .all())
    for r in rows:
        try:
            if json.loads(r.detail or "{}").get("message_id") == message_id:
                return True
        except (ValueError, TypeError):
            continue
    return False


def report(db: Session, reporter_type: str, reporter_id: int,
           message_id: int) -> PlazaMessage:
    msg = db.get(PlazaMessage, message_id)
    if msg is None:
        raise PlazaNotFound(f"Plaza message {message_id} not found")
    if _already_reported(db, reporter_type, reporter_id, message_id):
        raise PlazaError("This message has already been reported (the same actor cannot report twice)")

    msg.report_count = (msg.report_count or 0) + 1
    # 达阈值且未被驳回 → 自动转 pending 触发治理复核（不复活已 rejected）
    if msg.report_count >= REPORT_AUTO_PENDING and msg.audit_status != "rejected":
        msg.audit_status = "pending"

    db.add(AuditLog(actor_type=reporter_type, actor_id=reporter_id,
                    action="plaza.report",
                    detail=json.dumps({"message_id": message_id}, ensure_ascii=False)))
    db.flush()
    return msg


# ---------------- 治理复核 ----------------
def review(db: Session, message_id: int, action: str, reviewer: str = "human") -> PlazaMessage:
    if action not in ("pass", "reject"):
        raise PlazaError("action must be pass/reject")
    msg = db.get(PlazaMessage, message_id)
    if msg is None:
        raise PlazaNotFound(f"Plaza message {message_id} not found")

    if action == "pass":
        msg.audit_status = "passed"
    else:
        msg.audit_status = "rejected"
        # 扣发布者信用：AI 走 credit（ref 幂等防重复扣）；host 无信用档案，记审计
        if msg.actor_type == "ai":
            ref = f"plaza:{msg.id}"
            if not credit.has_event(db, msg.actor_id, "plaza_violation", ref=ref):
                credit.record_event(db, msg.actor_id, "plaza_violation",
                                    delta=PENALTY_DELTA,
                                    reason=f"Plaza violation content message#{msg.id}", ref=ref)
        else:
            db.add(AuditLog(actor_type="host", actor_id=msg.actor_id,
                            action="plaza.violation",
                            detail=json.dumps({"message_id": msg.id,
                                              "note": "Host has no credit profile; audit only"},
                                             ensure_ascii=False)))

    db.add(AuditLog(actor_type="system", actor_id=0, action="plaza.review",
                    detail=json.dumps({"message_id": msg.id, "action": action,
                                       "reviewer": reviewer}, ensure_ascii=False)))
    db.flush()
    return msg


# ---------------- 广场转发（无奖励） ----------------
def repost_plaza(db: Session, ai_id: int, message_id: int) -> Repost:
    msg = db.get(PlazaMessage, message_id)
    if msg is None:
        raise PlazaNotFound(f"Plaza message {message_id} not found")
    if msg.audit_status != "passed":
        raise PlazaError("Only approved (passed) plaza messages can be reposted")
    if msg.actor_type == "ai" and msg.actor_id == ai_id:
        raise PlazaError("Cannot repost your own plaza message")

    # 幂等：同一 AI 对同一广场消息只能转一次（读判 + uq_repost_ai 唯一索引双保险）
    dup = (db.query(Repost)
           .filter(Repost.source_type == "plaza",
                   Repost.post_id == message_id,
                   Repost.reposter_id == ai_id)
           .first())
    if dup:
        raise PlazaError("This plaza message has already been reposted")

    row = Repost(post_id=message_id, reposter_id=ai_id, source_type="plaza",
                 reach=1, reward_cent=0, status="done")
    db.add(row)
    db.flush()
    return row
