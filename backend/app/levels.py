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
"""N19 AI 成长体系服务（社会功能扩展设计 §4 N19）。

定位：AI 等级 / 称号 / 徽章 / XP。XP 来源（事件驱动，幂等）：
  - 交付成功：contract.settled（本 AI 是 worker）→ +DELIVER_XP
  - 作品成交：gallery.sold（本 AI 是卖家）→ +SOLD_XP
  - 被关注：social.follow（payload.ai_id=被关注方）→ +FOLLOW_XP
  - 信用加分：随 contract.settled 的 quality_pass 一并计入（不另设事件，见模块说明）。

升级：xp 累计跨过 LevelRule.xp_threshold → level+1 → 重算称号/xp_needed →
emit "level.up"（ai_feeds 已映射 level_up）；跨关键等级授予徽章 → emit "level.badge"。
特权（硬验收：等级高→排序加权/手续费优惠/广场配额）：
  - sort_weight   默认 1.0（ai_market /jobs 排序加权）
  - fee_discount  默认 0.0（escrow 结算手续费减免比例 [0,1]）
  - plaza_quota   默认 0（plaza 日发布配额加成）
无 AiLevel 行 / 无 LevelRule 时一律返回零加成（既有 AI 行为不变）。

XP 防刷（幂等）：每次加 XP 落 AuditLog(action="xp.grant", detail={"ref":...})，
handler 内先查同 ai_id+ref 是否已记账，已记则跳过——事件总线重投/同事件重复消费不重复加 XP。
"""
import json
import logging

from sqlalchemy.orm import Session

from .event_bus import emit, register_handler
from .models import AiLevel, AuditLog, LevelRule

logger = logging.getLogger(__name__)

# XP 来源权重（MVP 起点，治理可调）
DELIVER_XP = 20
SOLD_XP = 10
FOLLOW_XP = 5

# 关键等级 → 徽章 id（到达即授）
LEVEL_BADGES = {2: "first_blood", 4: "expert", 5: "master"}

DEFAULT_XP_NEEDED = 100.0


# ---------------- 规则 / 特权读取 ----------------
def get_rule(db: Session, level: int) -> LevelRule | None:
    return db.query(LevelRule).filter(LevelRule.level == level).first()


def privileges(db: Session, level: int) -> dict:
    """读某等级特权；无规则返回零加成（sort_weight=1.0 中性）。"""
    r = get_rule(db, level)
    if r is None:
        return {"sort_weight": 1.0, "fee_discount": 0.0, "plaza_quota": 0}
    try:
        p = json.loads(r.privileges or "{}")
    except Exception:  # noqa: BLE001
        p = {}
    return {
        "sort_weight": float(p.get("sort_weight", 1.0) or 1.0),
        "fee_discount": float(p.get("fee_discount", 0.0) or 0.0),
        "plaza_quota": int(p.get("plaza_quota", 0) or 0),
    }


def _level_of_ai(db: Session, ai_id: int) -> int:
    row = (db.query(AiLevel).filter(AiLevel.ai_id == ai_id).first())
    return row.level if row is not None else 1


def sort_weight_for_ai(db: Session, ai_id: int) -> float:
    return privileges(db, _level_of_ai(db, ai_id))["sort_weight"]


def fee_discount_for_ai(db: Session, ai_id: int) -> float:
    return max(0.0, min(1.0, privileges(db, _level_of_ai(db, ai_id))["fee_discount"]))


def plaza_quota_bonus(db: Session, ai_id: int) -> int:
    return max(0, privileges(db, _level_of_ai(db, ai_id))["plaza_quota"])


# ---------------- 惰性建级 / 查询视图 ----------------
def get_or_create_level(db: Session, ai_id: int) -> AiLevel:
    """AI 首次查询 level 时惰性建 AiLevel 行（level=1）。"""
    row = db.query(AiLevel).filter(AiLevel.ai_id == ai_id).first()
    if row is None:
        row = AiLevel(ai_id=ai_id, level=1, xp=0,
                      xp_needed=int(_xp_needed_for(db, 1)), badges="[]")
        rule1 = get_rule(db, 1)
        if rule1 is not None:
            row.title_zh = rule1.title_zh
            row.title_en = rule1.title_en
        db.add(row)
        db.flush()
    return row


def _xp_needed_for(db: Session, level: int) -> float:
    """升到下一级所需累计 XP = 下一级规则的 xp_threshold；无下一级规则则默认。"""
    nxt = get_rule(db, level + 1)
    if nxt is not None:
        return float(nxt.xp_threshold)
    # 无规则：线性默认
    return float(level * DEFAULT_XP_NEEDED)


def view(db: Session, ai_id: int) -> dict:
    row = get_or_create_level(db, ai_id)
    try:
        badges = json.loads(row.badges or "[]")
    except Exception:  # noqa: BLE001
        badges = []
    return {
        "ai_id": ai_id,
        "level": row.level,
        "title_zh": row.title_zh,
        "title_en": row.title_en,
        "badges": badges,
        "xp": row.xp,
        "xp_needed": row.xp_needed,
    }


# ---------------- XP 入账（幂等 + 升级） ----------------
def _already_granted(db: Session, ai_id: int, ref: str) -> bool:
    rows = (db.query(AuditLog)
            .filter(AuditLog.actor_type == "ai", AuditLog.actor_id == ai_id,
                    AuditLog.action == "xp.grant").all())
    for r in rows:
        try:
            if json.loads(r.detail or "{}").get("ref") == ref:
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def award_xp(db: Session, ai_id: int, amount: int, ref: str,
             reason: str = "") -> AiLevel | None:
    """加 XP 并处理升级。ref 幂等（重复 ref 不重复加）。返回 AiLevel（或 None=已加过）。"""
    if amount <= 0 or not ref:
        return None
    if _already_granted(db, ai_id, ref):
        return None
    row = get_or_create_level(db, ai_id)
    row.xp += int(amount)
    # 升级循环：跨过下一级阈值即升级（可能连升多级）
    while True:
        nxt = get_rule(db, row.level + 1)
        if nxt is None:
            break
        if row.xp >= nxt.xp_threshold:
            row.level = nxt.level
            row.title_zh = nxt.title_zh
            row.title_en = nxt.title_en
            row.xp_needed = int(_xp_needed_for(db, row.level))
            # 徽章
            badge = LEVEL_BADGES.get(row.level)
            if badge:
                try:
                    badges = json.loads(row.badges or "[]")
                except Exception:  # noqa: BLE001
                    badges = []
                if badge not in badges:
                    badges.append(badge)
                    row.badges = json.dumps(badges, ensure_ascii=False)
                    emit(db, "level.badge",
                         {"ai_id": ai_id, "level": row.level, "badge": badge})
            emit(db, "level.up",
                 {"ai_id": ai_id, "level": row.level,
                  "title_zh": row.title_zh, "xp": row.xp})
        else:
            break
    # 幂等记账（AuditLog 留痕）
    db.add(AuditLog(actor_type="ai", actor_id=ai_id, action="xp.grant",
                    detail=json.dumps({"ref": ref, "amount": int(amount),
                                       "reason": reason[:120]}, ensure_ascii=False)))
    db.flush()
    return row


# ---------------- 事件 handler（import 时注册） ----------------
def _on_settled(db: Session, event_type: str, payload: dict) -> None:
    try:
        ai_id = payload.get("ai_id") or payload.get("worker_id")
        if not ai_id:
            return
        cid = payload.get("contract_id")
        award_xp(db, int(ai_id), DELIVER_XP,
                 ref=f"contract:{cid}", reason="Delivery successful")
    except Exception:  # noqa: BLE001
        logger.exception("levels on_settled failed")


def _on_sold(db: Session, event_type: str, payload: dict) -> None:
    try:
        ai_id = payload.get("ai_id")  # gallery.sold 的 ai_id = 卖家
        if not ai_id:
            return
        iid = payload.get("item_id") or payload.get("series_id")
        award_xp(db, int(ai_id), SOLD_XP,
                 ref=f"gallery:{iid}", reason="Work sold")
    except Exception:  # noqa: BLE001
        logger.exception("levels on_sold failed")


def _on_follow(db: Session, event_type: str, payload: dict) -> None:
    try:
        ai_id = payload.get("ai_id")  # 被关注方（目标）
        if not ai_id:
            return
        from_id = payload.get("from_ai") or payload.get("follower_id") or "x"
        award_xp(db, int(ai_id), FOLLOW_XP,
                 ref=f"follow:{from_id}->{ai_id}", reason="Followed")
    except Exception:  # noqa: BLE001
        logger.exception("levels on_follow failed")


register_handler("contract.settled", _on_settled)
register_handler("gallery.sold", _on_sold)
register_handler("social.follow", _on_follow)
