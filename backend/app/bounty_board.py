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
"""公开悬赏板服务。

功能：
- 发布悬赏（post_bounty）：设定赏金、截止、最大认领数；
- 提交方案（submit_solution）；
- 审核（accept/reject_submission）；
- 开放悬赏查询与详情；
- 过期处理（expire_overdue）。

依赖模型：BountyListing, BountySubmission。
"""
import json
import logging
from datetime import datetime, timedelta

from .database import SessionLocal
from .models import BountyListing, BountySubmission

logger = logging.getLogger(__name__)


def _now():
    return datetime.utcnow()


class BountyBoard:
    """公开悬赏板业务逻辑。"""

    def post_bounty(self, title: str, description: str = "",
                    category: str = "bug", reward_cent: int = 0,
                    complexity: str = "medium", tags: list[str] | None = None,
                    issuer_type: str = "host", issuer_id: int = 0,
                    max_claims: int = 1, deadline: datetime | None = None) -> dict:
        """发布悬赏。"""
        if reward_cent <= 0:
            return {"error": "reward must be positive"}
        if deadline and deadline <= _now():
            return {"error": "deadline must be in the future"}

        db = SessionLocal()
        try:
            bounty = BountyListing(
                title=title,
                description=description,
                category=category,
                reward_cent=reward_cent,
                max_claims=max(1, max_claims),
                complexity=complexity,
                tags=json.dumps(tags or [], ensure_ascii=False),
                issuer_type=issuer_type,
                issuer_id=issuer_id,
                status="open",
                deadline=deadline or (_now() + timedelta(days=30)),
            )
            db.add(bounty)
            db.commit()
            logger.info("Bounty posted: id=%d title=%s reward=%d", bounty.id, title[:50], reward_cent)
            return {"bounty_id": bounty.id, "status": "open"}
        finally:
            db.close()

    def submit_solution(self, bounty_id: int, submitter_type: str,
                        submitter_id: int, content: str) -> dict:
        """提交解决方案。"""
        db = SessionLocal()
        try:
            bounty = db.get(BountyListing, bounty_id)
            if bounty is None:
                return {"error": "bounty not found"}
            if bounty.status not in ("open", "claimed"):
                return {"error": f"bounty is '{bounty.status}', cannot submit"}
            if bounty.deadline and bounty.deadline < _now():
                return {"error": "bounty deadline has passed"}
            if bounty.claimed_count >= bounty.max_claims:
                return {"error": "bounty has reached max claims"}

            # 检查是否已提交
            existing = (db.query(BountySubmission)
                        .filter(BountySubmission.bounty_id == bounty_id,
                                BountySubmission.submitter_id == submitter_id,
                                BountySubmission.submitter_type == submitter_type)
                        .first())
            if existing:
                # 更新内容
                existing.content = content
                db.commit()
                return {"ok": True, "submission_id": existing.id, "updated": True}

            sub = BountySubmission(
                bounty_id=bounty_id,
                submitter_id=submitter_id,
                submitter_type=submitter_type,
                content=content,
                status="pending",
            )
            db.add(sub)
            bounty.claimed_count += 1
            if bounty.claimed_count >= bounty.max_claims:
                bounty.status = "claimed"
            db.commit()

            logger.info("Bounty submission: bounty=%d submitter=%s:%d sub=%d",
                        bounty_id, submitter_type, submitter_id, sub.id)
            return {"ok": True, "submission_id": sub.id}
        finally:
            db.close()

    def accept_submission(self, submission_id: int, reviewer_id: int) -> dict:
        """接受提交（标记悬赏为 resolved）。"""
        db = SessionLocal()
        try:
            sub = db.get(BountySubmission, submission_id)
            if sub is None:
                return {"error": "submission not found"}
            if sub.status != "pending":
                return {"error": f"submission already {sub.status}"}

            sub.status = "accepted"
            sub.reviewed_by = reviewer_id

            bounty = db.get(BountyListing, sub.bounty_id)
            if bounty:
                bounty.status = "resolved"
                bounty.resolved_at = _now()

            db.commit()
            logger.info("Submission accepted: sub=%d bounty=%d reviewer=%d",
                        submission_id, sub.bounty_id, reviewer_id)
            return {"ok": True, "bounty_id": sub.bounty_id, "reward_cent": bounty.reward_cent if bounty else 0}
        finally:
            db.close()

    def reject_submission(self, submission_id: int, reviewer_id: int) -> dict:
        """拒绝提交。"""
        db = SessionLocal()
        try:
            sub = db.get(BountySubmission, submission_id)
            if sub is None:
                return {"error": "submission not found"}
            if sub.status != "pending":
                return {"error": f"submission already {sub.status}"}

            sub.status = "rejected"
            sub.reviewed_by = reviewer_id

            # 如果是 claimed 状态且被拒绝，可能允许其他人继续提交
            bounty = db.get(BountyListing, sub.bounty_id)
            if bounty and bounty.status == "claimed":
                bounty.claimed_count = max(0, bounty.claimed_count - 1)
                if bounty.claimed_count < bounty.max_claims:
                    bounty.status = "open"

            db.commit()
            logger.info("Submission rejected: sub=%d reviewer=%d", submission_id, reviewer_id)
            return {"ok": True}
        finally:
            db.close()

    def get_open_bounties(self, category: str = "", complexity: str = "",
                          min_reward: int = 0, limit: int = 20,
                          offset: int = 0) -> list[dict]:
        """获取开放悬赏列表。"""
        db = SessionLocal()
        try:
            q = (db.query(BountyListing)
                 .filter(BountyListing.status.in_(["open", "claimed"])))

            if category:
                q = q.filter(BountyListing.category == category)
            if complexity:
                q = q.filter(BountyListing.complexity == complexity)
            if min_reward > 0:
                q = q.filter(BountyListing.reward_cent >= min_reward)

            rows = (q.order_by(BountyListing.id.desc())
                    .offset(offset)
                    .limit(min(limit, 50))
                    .all())

            return [
                {
                    "id": b.id, "title": b.title, "category": b.category,
                    "reward_cent": b.reward_cent, "complexity": b.complexity,
                    "tags": json.loads(b.tags or "[]"),
                    "status": b.status, "claimed_count": b.claimed_count,
                    "max_claims": b.max_claims,
                    "deadline": b.deadline.isoformat() if b.deadline else None,
                    "created_at": b.created_at.isoformat() if b.created_at else None,
                }
                for b in rows
            ]
        finally:
            db.close()

    def get_bounty_detail(self, bounty_id: int) -> dict:
        """获取悬赏详情（含提交列表）。"""
        db = SessionLocal()
        try:
            bounty = db.get(BountyListing, bounty_id)
            if bounty is None:
                return {"error": "bounty not found"}

            subs = (db.query(BountySubmission)
                    .filter(BountySubmission.bounty_id == bounty_id)
                    .order_by(BountySubmission.id.asc())
                    .all())

            return {
                "id": bounty.id,
                "title": bounty.title,
                "description": bounty.description,
                "category": bounty.category,
                "reward_cent": bounty.reward_cent,
                "complexity": bounty.complexity,
                "tags": json.loads(bounty.tags or "[]"),
                "issuer_type": bounty.issuer_type,
                "issuer_id": bounty.issuer_id,
                "status": bounty.status,
                "claimed_count": bounty.claimed_count,
                "max_claims": bounty.max_claims,
                "deadline": bounty.deadline.isoformat() if bounty.deadline else None,
                "created_at": bounty.created_at.isoformat() if bounty.created_at else None,
                "submissions": [
                    {"id": s.id, "submitter_type": s.submitter_type,
                     "submitter_id": s.submitter_id, "status": s.status,
                     "created_at": s.created_at.isoformat() if s.created_at else None}
                    for s in subs
                ],
            }
        finally:
            db.close()

    def expire_overdue(self) -> dict:
        """将所有已过期但未处理的悬赏标记为 expired。"""
        db = SessionLocal()
        try:
            now = _now()
            count = (db.query(BountyListing)
                     .filter(BountyListing.status.in_(["open", "claimed"]),
                             BountyListing.deadline < now)
                     .update({"status": "expired"}, synchronize_session=False))
            db.commit()
            logger.info("Bounty expire_overdue: %d expired", count)
            return {"expired": count}
        finally:
            db.close()


instance = BountyBoard()
