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
"""二次投票（Quadratic Voting）机制。

功能：
- 投票创建（poll）：设定选项和每位投票者的信用额度；
- 投票：消耗 credits 数 = vote_weight^2；
- 计票：有效票 = sqrt(credits)；
- 关闭投票与结果查询。

依赖模型：QuadraticVote。
核心公式：有效票数 = sqrt(credits_spent)，即 n^2 成本获得 n 票影响力。
"""
import json
import logging
import math
from datetime import datetime

from sqlalchemy import func

from .config import settings
from .database import SessionLocal
from .models import QuadraticPoll, QuadraticVote

logger = logging.getLogger(__name__)

# A-H3 修复：poll 元数据持久化到 QuadraticPoll 表（库为唯一真源），
# self._polls 仅作只读缓存（兼容旧调用方 reset 钩子）。投票记录写 QuadraticVote。


def _now():
    return datetime.utcnow()


class QuadraticVoting:
    """二次投票机制。"""

    def __init__(self):
        # 兼容旧调用方的 reset 钩子（_polls.clear()/_next_poll_id）；
        # A-H3 后 poll 真源在 QuadraticPoll 表，本 dict 仅作只读缓存。
        self._polls: dict[int, dict] = {}
        self._next_poll_id = 1

    def _load_poll(self, db, poll_id: int):
        """从 QuadraticPoll 表读取 poll 元数据（缓存回退兼容外部注入）。"""
        row = db.get(QuadraticPoll, poll_id)
        if row is None:
            return self._polls.get(poll_id)
        try:
            options = json.loads(row.options_json or "[]")
        except Exception:  # noqa: BLE001
            options = []
        return {
            "id": row.id, "title": row.title, "options": options,
            "credits_per_voter": row.credits_per_voter, "status": row.status,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }

    def create_poll(self, title: str, options: list[str],
                    credits_per_voter: int | None = None) -> dict:
        """创建投票（持久化到 QuadraticPoll 表）。

        Args:
            title: 投票标题。
            options: 选项列表。
            credits_per_voter: 每人可用信用数（默认取配置）。

        Returns:
            {"poll_id": int, "credits_per_voter": int}
        """
        if credits_per_voter is None:
            credits_per_voter = settings.QV_CREDITS_PER_VOTER

        db = SessionLocal()
        try:
            poll = QuadraticPoll(
                title=title,
                options_json=json.dumps(list(options), ensure_ascii=False),
                credits_per_voter=credits_per_voter,
                status="open",
            )
            db.add(poll)
            db.commit()
            db.refresh(poll)
            poll_id = poll.id
            self._next_poll_id = max(self._next_poll_id, poll_id + 1)
            self._polls[poll_id] = {
                "id": poll_id, "title": title, "options": list(options),
                "credits_per_voter": credits_per_voter, "status": "open",
                "created_at": _now().isoformat(),
            }
            logger.info("QV poll created: id=%d title=%s options=%d",
                        poll_id, title, len(options))
            return {"poll_id": poll_id, "credits_per_voter": credits_per_voter}
        finally:
            db.close()

    def cast_vote(self, poll_id: int, voter_type: str, voter_id: int,
                  option_id: int, credits: int) -> dict:
        """投票：消耗 credits，有效票 = sqrt(credits)。

        A-H4：额度校验前对 QuadraticPoll 行加 FOR UPDATE 行锁，串行化同一
        poll 的并发额度检查与写入，防止超投（SQLite 忽略 FOR UPDATE 但不报错）。

        Returns:
            {"vote_weight": int, "credits_spent": int}
        """
        if credits <= 0:
            return {"error": "credits must be positive"}

        db = SessionLocal()
        try:
            # A-H4：锁定 poll 行（PG 行锁；SQLite 自动忽略），串行化额度校验
            poll_row = (db.query(QuadraticPoll)
                        .filter(QuadraticPoll.id == poll_id)
                        .with_for_update().first())
            if poll_row is not None:
                poll_status = poll_row.status
                try:
                    opts = json.loads(poll_row.options_json or "[]")
                except Exception:  # noqa: BLE001
                    opts = []
                credits_per_voter = poll_row.credits_per_voter
            else:
                cached = self._polls.get(poll_id)
                if cached is None:
                    return {"error": "poll not found"}
                poll_status = cached["status"]
                opts = cached["options"]
                credits_per_voter = cached["credits_per_voter"]

            if poll_status != "open":
                return {"error": "poll is closed"}

            # 验证选项有效
            if option_id < 0 or option_id >= len(opts):
                return {"error": "invalid option"}

            # 检查已用信用
            used = (db.query(func.coalesce(func.sum(QuadraticVote.credits_spent), 0))
                    .filter(QuadraticVote.poll_id == poll_id,
                            QuadraticVote.voter_id == voter_id,
                            QuadraticVote.voter_type == voter_type)
                    .scalar())

            remaining = credits_per_voter - used
            if credits > remaining:
                return {"error": f"insufficient credits: have {remaining}, need {credits}"}

            # 有效票 = sqrt(credits)（向下取整）
            vote_weight = int(math.isqrt(credits))
            actual_credits = vote_weight * vote_weight  # 只消耗完全平方数

            vote = QuadraticVote(
                poll_id=poll_id,
                voter_id=voter_id,
                voter_type=voter_type,
                option_id=option_id,
                credits_spent=actual_credits,
                vote_weight=vote_weight,
            )
            db.add(vote)
            db.commit()

            logger.info("QV vote cast: poll=%d voter=%s:%d option=%d weight=%d",
                        poll_id, voter_type, voter_id, option_id, vote_weight)
            return {"vote_weight": vote_weight, "credits_spent": actual_credits}
        finally:
            db.close()

    def get_results(self, poll_id: int) -> dict:
        """获取投票结果（按有效票汇总）。"""
        db = SessionLocal()
        try:
            poll = self._load_poll(db, poll_id)
            if poll is None:
                return {"error": "poll not found"}

            results = (db.query(
                QuadraticVote.option_id,
                func.sum(QuadraticVote.vote_weight).label("total_weight"),
                func.sum(QuadraticVote.credits_spent).label("total_credits"),
                func.count(QuadraticVote.id).label("vote_count"),
            )
                       .filter(QuadraticVote.poll_id == poll_id)
                       .group_by(QuadraticVote.option_id)
                       .all())

            options_result = []
            for i, opt_name in enumerate(poll["options"]):
                found = next((r for r in results if r.option_id == i), None)
                options_result.append({
                    "option_id": i,
                    "name": opt_name,
                    "total_weight": int(found.total_weight) if found else 0,
                    "total_credits": int(found.total_credits) if found else 0,
                    "voter_count": int(found.vote_count) if found else 0,
                })

            # 按权重降序排列
            options_result.sort(key=lambda x: x["total_weight"], reverse=True)
            return {
                "poll_id": poll_id,
                "title": poll["title"],
                "status": poll["status"],
                "results": options_result,
            }
        finally:
            db.close()

    def close_poll(self, poll_id: int) -> dict:
        """关闭投票。"""
        db = SessionLocal()
        try:
            row = db.get(QuadraticPoll, poll_id)
            if row is not None:
                row.status = "closed"
                row.closed_at = _now()
                db.commit()
                poll = {"status": "closed"}
            else:
                poll = self._polls.get(poll_id)
                if poll is None:
                    return {"error": "poll not found"}
                poll["status"] = "closed"
                poll["closed_at"] = _now().isoformat()
            logger.info("QV poll closed: id=%d", poll_id)
            return {"ok": True, "poll_id": poll_id, "status": "closed"}
        finally:
            db.close()

    def get_voter_credits(self, poll_id: int, voter_type: str,
                          voter_id: int) -> dict:
        """查询投票者剩余信用。"""
        db = SessionLocal()
        try:
            poll = self._load_poll(db, poll_id)
            if poll is None:
                return {"error": "poll not found"}

            used = (db.query(func.coalesce(func.sum(QuadraticVote.credits_spent), 0))
                    .filter(QuadraticVote.poll_id == poll_id,
                            QuadraticVote.voter_id == voter_id,
                            QuadraticVote.voter_type == voter_type)
                    .scalar())

            total = poll["credits_per_voter"]
            return {
                "poll_id": poll_id,
                "total_credits": total,
                "used": int(used),
                "remaining": total - int(used),
            }
        finally:
            db.close()


instance = QuadraticVoting()
