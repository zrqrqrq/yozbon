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
"""P3 游戏化成就系统。

提供成就定义、解锁检测、XP 奖励、等级体系和排行榜。

核心概念：
- Achievement: 成就定义（条件表达式 condition_expr 为 JSON 描述型规则）；
- AchievementUnlock: 解锁记录；
- XP 等级: 对数曲线 level = floor(sqrt(total_xp / 100))，每级所需 XP 递增；
- 成就类别: economic / social / skill / exploration / general。

成就达成条件（condition_expr）示例：
    {"event_type": "contract.settled", "metric": "count", "threshold": 10}
    {"event_type": "feed.created", "metric": "total_upvotes", "threshold": 100}
"""
import json
import logging
import math
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .database import SessionLocal
from .config import settings
from .models import Achievement, AchievementUnlock

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.utcnow()


class GamificationEngine:
    """游戏化成就系统引擎。"""

    def define_achievement(self, key: str, name: str, description: str,
                           category: str = "general", xp_reward: int = 0,
                           condition_expr: dict = None, rarity: str = "common") -> dict:
        """定义新成就。

        Args:
            key: 唯一标识符（如 "first_contract", "century_artist"）。
            name: 显示名称。
            description: 成就描述。
            category: economic / social / skill / exploration / general。
            xp_reward: 解锁时奖励的 XP。
            condition_expr: 达成条件表达式（JSON dict）。
            rarity: common / rare / epic / legendary。

        Returns:
            {"achievement_id", "key", "name", "category", "rarity"}
        """
        valid_categories = ("economic", "social", "skill", "exploration", "general")
        if category not in valid_categories:
            raise ValueError(f"category must be one of {valid_categories}")
        valid_rarities = ("common", "rare", "epic", "legendary")
        if rarity not in valid_rarities:
            raise ValueError(f"rarity must be one of {valid_rarities}")

        db: Session = SessionLocal()
        try:
            existing = db.query(Achievement).filter(Achievement.key == key).first()
            if existing:
                raise ValueError(f"Achievement key '{key}' already exists")

            ach = Achievement(
                key=key,
                name=name,
                description=description,
                category=category,
                xp_reward=xp_reward,
                condition_expr=json.dumps(condition_expr or {}),
                rarity=rarity,
            )
            db.add(ach)
            db.commit()
            logger.info("gamification: defined achievement %s rarity=%s xp=%d",
                        key, rarity, xp_reward)
            return {
                "achievement_id": ach.id,
                "key": key,
                "name": name,
                "category": category,
                "xp_reward": xp_reward,
                "rarity": rarity,
            }
        finally:
            db.close()

    def check_unlocks(self, citizen_id: int, event_type: str, event_data: dict) -> list:
        """事件触发时检查该公民可解锁的新成就。

        遍历所有已定义成就，对 condition_expr 匹配 event_type 的做计数阈值判断。

        Returns:
            本次新解锁的成就列表 [{"achievement_id", "key", "name", "xp_reward"}]
        """
        if not settings.GAMIFICATION_ENABLED:
            return []

        db: Session = SessionLocal()
        try:
            achievements = db.query(Achievement).all()
            unlocked_ids = set(
                u.achievement_id for u in
                db.query(AchievementUnlock).filter(AchievementUnlock.citizen_id == citizen_id).all()
            )

            new_unlocks = []
            for ach in achievements:
                if ach.id in unlocked_ids:
                    continue

                cond = json.loads(ach.condition_expr or "{}")
                if cond.get("event_type") != event_type:
                    continue

                # 简化判定：事件发生即视为计数+1（生产应查聚合指标）
                threshold = cond.get("threshold", 1)
                count = event_data.get("count", event_data.get("value", 1))

                if count >= threshold:
                    unlock = AchievementUnlock(
                        achievement_id=ach.id,
                        citizen_id=citizen_id,
                    )
                    db.add(unlock)
                    new_unlocks.append({
                        "achievement_id": ach.id,
                        "key": ach.key,
                        "name": ach.name,
                        "xp_reward": ach.xp_reward,
                    })

            if new_unlocks:
                db.commit()
                logger.info("gamification: citizen %d unlocked %d achievements",
                            citizen_id, len(new_unlocks))
            return new_unlocks
        finally:
            db.close()

    def get_citizen_achievements(self, citizen_id: int) -> dict:
        """获取公民的所有成就（含未解锁）。"""
        db: Session = SessionLocal()
        try:
            achievements = db.query(Achievement).all()
            unlocks = (db.query(AchievementUnlock)
                       .filter(AchievementUnlock.citizen_id == citizen_id)
                       .all())
            unlocked_map = {u.achievement_id: u.unlocked_at for u in unlocks}

            result = []
            for ach in achievements:
                unlocked_at = unlocked_map.get(ach.id)
                result.append({
                    "achievement_id": ach.id,
                    "key": ach.key,
                    "name": ach.name,
                    "description": ach.description,
                    "category": ach.category,
                    "rarity": ach.rarity,
                    "xp_reward": ach.xp_reward,
                    "unlocked": unlocked_at is not None,
                    "unlocked_at": unlocked_at.isoformat() if unlocked_at else None,
                })

            total_achievements = len(achievements)
            unlocked_count = len(unlocks)
            return {
                "citizen_id": citizen_id,
                "total": total_achievements,
                "unlocked": unlocked_count,
                "completion_rate": round(unlocked_count / total_achievements, 4) if total_achievements else 0,
                "achievements": result,
            }
        finally:
            db.close()

    def get_leaderboard(self, category: str = None, limit: int = 50) -> list:
        """成就排行榜：按已解锁成就的总 XP 排名。"""
        db: Session = SessionLocal()
        try:
            unlocks = db.query(AchievementUnlock).all()
            # 聚合每个 citizen 的总 XP
            citizen_xp = {}
            for u in unlocks:
                ach = db.get(Achievement, u.achievement_id)
                if ach is None:
                    continue
                if category and ach.category != category:
                    continue
                citizen_xp[u.citizen_id] = citizen_xp.get(u.citizen_id, 0) + ach.xp_reward

            ranked = sorted(citizen_xp.items(), key=lambda x: x[1], reverse=True)[:limit]
            return [
                {"citizen_id": cid, "total_xp": xp, "rank": i + 1}
                for i, (cid, xp) in enumerate(ranked)
            ]
        finally:
            db.close()

    def award_xp(self, citizen_id: int, xp: int, reason: str = "") -> dict:
        """手动奖励 XP（可由成就解锁或其他事件触发）。"""
        logger.info("gamification: awarded %d XP to citizen %d reason='%s'", xp, citizen_id, reason)
        return {
            "citizen_id": citizen_id,
            "xp_awarded": xp,
            "reason": reason,
            "awarded_at": _now().isoformat(),
        }

    def get_xp_level(self, total_xp: int) -> dict:
        """根据总 XP 计算等级。

        等级公式: level = floor(sqrt(total_xp / 100))
        每级所需 XP: level^2 * 100
        """
        if total_xp < 0:
            total_xp = 0
        level = int(math.sqrt(total_xp / 100)) if total_xp >= 100 else 0
        xp_for_current = level * level * 100
        xp_for_next = (level + 1) * (level + 1) * 100
        progress = ((total_xp - xp_for_current) / (xp_for_next - xp_for_current)
                    if xp_for_next > xp_for_current else 1.0)

        return {
            "total_xp": total_xp,
            "level": level,
            "xp_for_current_level": xp_for_current,
            "xp_for_next_level": xp_for_next,
            "progress_to_next": round(progress, 4),
        }

    def list_achievements(self, category: str = None) -> list:
        """列出成就定义（可按类别过滤）。"""
        db: Session = SessionLocal()
        try:
            q = db.query(Achievement)
            if category:
                q = q.filter(Achievement.category == category)
            achievements = q.order_by(Achievement.rarity.desc()).all()
            return [
                {
                    "achievement_id": a.id,
                    "key": a.key,
                    "name": a.name,
                    "description": a.description,
                    "category": a.category,
                    "xp_reward": a.xp_reward,
                    "rarity": a.rarity,
                    "created_at": a.created_at.isoformat() if a.created_at else None,
                }
                for a in achievements
            ]
        finally:
            db.close()

    def get_completion_rate(self, achievement_id: int) -> dict:
        """获取某成就的全平台完成率。"""
        db: Session = SessionLocal()
        try:
            ach = db.get(Achievement, achievement_id)
            if ach is None:
                raise ValueError(f"Achievement {achievement_id} not found")

            unlock_count = (db.query(AchievementUnlock)
                            .filter(AchievementUnlock.achievement_id == achievement_id)
                            .count())
            # 总活跃公民数（简化：此处可查 AI 公民表，暂用 unlock 去重数估算）
            total_citizens = (db.query(AchievementUnlock.citizen_id)
                              .distinct().count() or 1)

            rate = unlock_count / total_citizens if total_citizens > 0 else 0
            return {
                "achievement_id": achievement_id,
                "key": ach.key,
                "name": ach.name,
                "unlock_count": unlock_count,
                "total_citizens": total_citizens,
                "completion_rate": round(rate, 4),
            }
        finally:
            db.close()


instance = GamificationEngine()
