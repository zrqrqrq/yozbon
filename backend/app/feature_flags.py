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
"""P2 Feature Flags 特性开关服务。

提供特性开关的 CRUD、灰度发布（按 user_id hash 一致性分桶）和上下文求值。

核心机制：
- rollout_pct 为灰度百分比（0-100）；
- 通过 hashlib.md5(user_id) % 100 实现一致性灰度，同一 user 始终命中同一分桶；
- target_tiers 限制适用席位等级（逗号分隔，"*" 代表全部）。
"""
import hashlib
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from .database import SessionLocal
from .config import settings
from .models import FeatureFlag

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.utcnow()


class FeatureFlagService:
    """Feature Flags 特性开关服务。"""

    def is_enabled(self, flag_key: str, user_id: int = None, tier: str = None) -> bool:
        """判断某用户/席位是否命中开关。

        逻辑：
        1. 开关未启用 → False；
        2. target_tiers 不含当前 tier 且不为 "*" → False；
        3. rollout_pct == 100 → True；
        4. 按 user_id hash 取模判断是否命中灰度。
        """
        db: Session = SessionLocal()
        try:
            flag = db.query(FeatureFlag).filter(FeatureFlag.flag_key == flag_key).first()
            if flag is None or not flag.enabled:
                return False

            # 席位限制
            if flag.target_tiers != "*" and tier:
                allowed = [t.strip() for t in flag.target_tiers.split(",")]
                if tier not in allowed:
                    return False

            # 灰度计算
            if flag.rollout_pct >= 100:
                return True
            if flag.rollout_pct <= 0:
                return False
            if user_id is None:
                return False

            bucket = self._hash_bucket(flag_key, user_id)
            return bucket < flag.rollout_pct
        finally:
            db.close()

    def create_flag(self, flag_key: str, description: str, enabled: bool,
                    rollout_pct: int = 0, target_tiers: str = "*") -> dict:
        """创建新的特性开关。"""
        if rollout_pct < 0 or rollout_pct > 100:
            raise ValueError("rollout_pct must be between 0 and 100")

        db: Session = SessionLocal()
        try:
            existing = db.query(FeatureFlag).filter(FeatureFlag.flag_key == flag_key).first()
            if existing:
                raise ValueError(f"Feature flag {flag_key} already exists")

            flag = FeatureFlag(
                flag_key=flag_key,
                description=description,
                enabled=1 if enabled else 0,
                rollout_pct=rollout_pct,
                target_tiers=target_tiers,
            )
            db.add(flag)
            db.commit()
            logger.info("feature_flags: created flag %s enabled=%s rollout=%d%%",
                        flag_key, enabled, rollout_pct)
            return self._serialize(flag)
        finally:
            db.close()

    def update_flag(self, flag_key: str, **kwargs) -> dict:
        """更新开关属性（enabled, rollout_pct, target_tiers, description）。"""
        db: Session = SessionLocal()
        try:
            flag = db.query(FeatureFlag).filter(FeatureFlag.flag_key == flag_key).first()
            if flag is None:
                raise ValueError(f"Feature flag {flag_key} not found")

            updatable = {"enabled", "rollout_pct", "target_tiers", "description"}
            for k, v in kwargs.items():
                if k not in updatable:
                    continue
                if k == "enabled":
                    v = 1 if v else 0
                setattr(flag, k, v)

            flag.updated_at = _now()
            db.commit()
            logger.info("feature_flags: updated flag %s fields=%s", flag_key, list(kwargs.keys()))
            return self._serialize(flag)
        finally:
            db.close()

    def delete_flag(self, flag_key: str) -> bool:
        """删除开关。"""
        db: Session = SessionLocal()
        try:
            flag = db.query(FeatureFlag).filter(FeatureFlag.flag_key == flag_key).first()
            if flag is None:
                return False
            db.delete(flag)
            db.commit()
            logger.info("feature_flags: deleted flag %s", flag_key)
            return True
        finally:
            db.close()

    def list_flags(self) -> list:
        """列出所有开关。"""
        db: Session = SessionLocal()
        try:
            flags = db.query(FeatureFlag).order_by(FeatureFlag.flag_key).all()
            return [self._serialize(f) for f in flags]
        finally:
            db.close()

    def evaluate(self, flag_key: str, context: dict) -> bool:
        """基于上下文求值开关。

        context 可包含: user_id, tier, 或其他自定义字段。
        """
        return self.is_enabled(
            flag_key,
            user_id=context.get("user_id"),
            tier=context.get("tier"),
        )

    def get_rollout_pct(self, flag_key: str, user_id: int) -> dict:
        """获取指定用户在开关中的灰度信息。"""
        db: Session = SessionLocal()
        try:
            flag = db.query(FeatureFlag).filter(FeatureFlag.flag_key == flag_key).first()
            if flag is None:
                raise ValueError(f"Feature flag {flag_key} not found")

            bucket = self._hash_bucket(flag_key, user_id)
            return {
                "flag_key": flag_key,
                "user_id": user_id,
                "bucket": bucket,
                "rollout_pct": flag.rollout_pct,
                "hit": bucket < flag.rollout_pct,
            }
        finally:
            db.close()

    # ---- 内部方法 ----

    @staticmethod
    def _hash_bucket(flag_key: str, user_id: int) -> int:
        """一致性分桶：同一 flag_key + user_id 始终得到同一桶号（0-99）。"""
        raw = f"{flag_key}:{user_id}".encode()
        h = int(hashlib.md5(raw).hexdigest(), 16)
        return h % 100

    @staticmethod
    def _serialize(flag: FeatureFlag) -> dict:
        return {
            "id": flag.id,
            "flag_key": flag.flag_key,
            "description": flag.description,
            "enabled": bool(flag.enabled),
            "rollout_pct": flag.rollout_pct,
            "target_tiers": flag.target_tiers,
            "created_at": flag.created_at.isoformat() if flag.created_at else None,
            "updated_at": flag.updated_at.isoformat() if flag.updated_at else None,
        }


instance = FeatureFlagService()
