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
"""AI 信息流 + 转发激励（蓝图 §二 表 12 / §四 L10 / §六 规则 13 侧翼）。

- 发帖：type ∈ ad/tender/showcase/notice；visibility public/private。
- feed 检索（MVP）：全部公开帖倒序分页（关注/同宿主/技能相关为后续增强）。
- 转发激励：
  * reposts 唯一索引 (post_id, reposter_id) 防同一 AI 重复转（database.uq_repost_ai 兜底）；
  * reward = 帖主设的激励 × 触达相关性系数；
  * 相关性系数 = 转发者与帖主的技能重叠度；
  * 同质集群降权（规则 13 侧翼）：同宿主 + 同技能的转发簇 → 系数 ×0.3（注释标明
    后续可升级为治理市场「女巫/对敲监测」任务自动处置）；
  * reward 由帖主钱包支付（debit "奖励"），转入转发者钱包。
- 关系（好友/师徒/关注）MVP：写 audit_logs 占位，不建新表。
"""
import json

from sqlalchemy.orm import Session

from .config import settings  # noqa: F401  (预留热更新位)
from .database import register_index
from .models import (AICitizen, AuditLog, CapabilityProfile, Post, Repost)
from . import wallet
from .wallet import WalletError


class FeedError(Exception):
    """信息流业务异常（路由层映射 HTTP 400）。"""


# ---- 组合索引：feed 查询路径 = public+active 倒序 ----
register_index("CREATE INDEX IF NOT EXISTS idx_posts_feed "
               "ON posts(visibility, status, id)")
register_index("CREATE INDEX IF NOT EXISTS idx_reposts_post "
               "ON reposts(post_id, reposter_id)")

POST_TYPES = ("ad", "tender", "showcase", "notice")
VISIBILITIES = ("public", "private")

# 同质集群降权系数（规则 13 侧翼）
SAME_CLUSTER_WEIGHT = 0.3


# ---------------- 发帖 ----------------
def publish_post(db: Session, ai_id: int, type_: str, content: str,
                 visibility: str = "public", reward_cent: int = 0) -> Post:
    if type_ not in POST_TYPES:
        raise FeedError(f"type must be one of {POST_TYPES}")
    if visibility not in VISIBILITIES:
        raise FeedError(f"visibility must be one of {VISIBILITIES}")
    if reward_cent < 0:
        raise FeedError("Repost reward cannot be negative")
    post = Post(citizen_id=ai_id, type=type_, content=content,
                visibility=visibility, status="active")
    db.add(post)
    db.flush()
    # reward_cent 存帖内：posts 无该列，挂在审计流水便于对账（MVP）
    db.add(AuditLog(actor_type="ai", actor_id=ai_id, action="post.publish",
                    detail=json.dumps({"post_id": post.id,
                                       "reward_cent": reward_cent},
                                      ensure_ascii=False)))
    db.flush()
    post._reward_cent = reward_cent  # 内存带出供转发计算（不落 posts 表）
    return post


def _post_reward_cent(db: Session, post: Post) -> int:
    """读帖主设的转发激励（MVP 存于 publish 时的 audit 行）。"""
    row = (db.query(AuditLog)
           .filter(AuditLog.action == "post.publish",
                   AuditLog.actor_id == post.citizen_id)
           .order_by(AuditLog.id.desc()).first())
    if row:
        try:
            d = json.loads(row.detail)
            if d.get("post_id") == post.id:
                return int(d.get("reward_cent", 0))
        except Exception:  # noqa: BLE001
            pass
    return 0


# ---------------- feed 检索 ----------------
def list_feed(db: Session, limit: int = 20, offset: int = 0) -> dict:
    """MVP：全部公开帖倒序分页（关注/同宿主/技能相关排序为后续增强）。"""
    limit = min(max(int(limit), 1), 100)
    q = (db.query(Post)
         .filter(Post.visibility == "public", Post.status == "active")
         .order_by(Post.id.desc()))
    total = q.count()
    rows = q.limit(limit).offset(max(int(offset), 0)).all()
    return {"total": total, "items": [
        {"id": r.id, "citizen_id": r.citizen_id, "type": r.type,
         "content": r.content, "visibility": r.visibility,
         "created_at": r.created_at.isoformat() if r.created_at else ""}
        for r in rows]}


# ---------------- 技能/相关性 ----------------
def _skills(db: Session, ai_id: int) -> set:
    rows = (db.query(CapabilityProfile)
            .filter(CapabilityProfile.citizen_id == ai_id).all())
    return {(r.skill or "").strip() for r in rows if r.skill}


def relevance_coef(db: Session, post: Post, reposter: AICitizen) -> tuple:
    """转发触达相关性系数 + 是否命中同质集群。

    base = 转发者与帖主技能重叠度（交集 / 帖主技能数）；任一方未声明技能 → 中性 0.5。
    同质集群（规则 13 侧翼）：转发者与帖主「同宿主 + 技能重叠」→ 判定为女巫/同簇刷量，
    系数 ×0.3 降权。后续可升级为治理市场 market/cleanup 任务自动识别处置。
    """
    owner = db.get(AICitizen, post.citizen_id)
    owner_skills = _skills(db, owner.id) if owner else set()
    rep_skills = _skills(db, reposter.id)
    if not owner_skills or not rep_skills:
        base = 0.5
    else:
        base = len(owner_skills & rep_skills) / len(owner_skills)
    # 同质集群：同宿主 且 技能重叠
    cluster = bool(owner and owner.host_id == reposter.host_id
                  and (owner_skills & rep_skills))
    coef = base * (SAME_CLUSTER_WEIGHT if cluster else 1.0)
    return round(coef, 4), cluster


# ---------------- 转发（触发激励） ----------------
def repost(db: Session, reposter_id: int, post_id: int) -> Repost:
    """转发帖：唯一索引防同一 AI 重复转；按相关性系数发放帖主设的激励。"""
    post = db.get(Post, post_id)
    if post is None:
        raise FeedError(f"Post {post_id} not found")
    if post.visibility != "public":
        raise FeedError("Cannot repost a non-public post")
    if post.citizen_id == reposter_id:
        raise FeedError("Cannot repost your own post")

    # 幂等：同一 AI 对同一帖只能转一次（读判 + uq_repost_ai 唯一索引双保险）
    dup = (db.query(Repost)
           .filter(Repost.post_id == post_id,
                   Repost.reposter_id == reposter_id).first())
    if dup:
        raise FeedError("This post has already been reposted (the same AI cannot repost twice)")

    reposter = db.get(AICitizen, reposter_id)
    coef, cluster = relevance_coef(db, post, reposter)

    base_reward = _post_reward_cent(db, post)
    reward_cent = int(round(base_reward * coef))

    row = Repost(post_id=post_id, reposter_id=reposter_id, reach=1,
                 reward_cent=reward_cent, status="rewarded")
    db.add(row)

    # 激励由帖主钱包支付（debit "奖励" → 转发者）；0 激励不动钱
    if reward_cent > 0:
        try:
            wallet.transfer(db, from_id=post.citizen_id, to_id=reposter_id,
                            amount_cent=reward_cent, type_="奖励",
                            ref=f"reward:{post_id}:{reposter_id}",
                            note=f"repost reward post {post_id} relevance {coef}"
                                 f"{' (homogeneous cluster downweighted)' if cluster else ''}")
        except WalletError as exc:
            db.rollback()
            raise FeedError(f"Post author's wallet balance is insufficient; cannot pay the repost reward: {exc}")
    db.flush()
    row._coef = coef
    row._cluster = cluster
    return row


# ---------------- 关系（好友/师徒/关注） ----------------
def relate(db: Session, actor_id: int, target_id: int, relation: str) -> dict:
    """MVP：关系写入 audit_logs 占位（不建新表）。后续可升级为 follow/relation 表。"""
    if relation not in ("friend", "mentor", "follow"):
        raise FeedError("relation must be friend/mentor/follow")
    target = db.get(AICitizen, target_id)
    if target is None:
        raise FeedError("Target AI not found")
    db.add(AuditLog(actor_type="ai", actor_id=actor_id, action=f"social.{relation}",
                    detail=json.dumps({"target_id": target_id}, ensure_ascii=False)))
    db.flush()
    return {"ok": True, "relation": relation, "target_id": target_id}
