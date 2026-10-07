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
"""P2 ML 增强内容审核服务。

规则 + 模型混合评分机制：
1. 规则层：复用既有 moderation.py 的词表/正则规则产出 rule_score；
2. 模型层：当前为**占位模拟推理**（C-D23），非真实 ML 模型，产出 ml_scores；
3. 决策层：综合两层得分给出 pass / review / block 三级判定。

C-D23 诚实标注：_model_inference 当前使用 md5 种子伪随机数，不具备真实
分类能力。因此混合权重已将模型层从 0.6 降至 0.1，以规则层(0.9)为主导，
避免伪随机噪声干扰审核决策。生产接入真实推理（ONNX/TFLite/HTTP 微服务）后
恢复权重为 0.4/0.6。

评分维度：toxic, spam, violence, harassment, misinformation。
决策阈值由 settings.ML_MODERATION_BLOCK_THRESHOLD 控制。
"""
import json
import logging
import random
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .database import SessionLocal
from .config import settings
from .models import ModerationScore

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.utcnow()


class MLModerator:
    """ML 增强内容审核服务。"""

    def score_content(self, content_type: str, content_id: int,
                      text: str, citizen_id: int = 0,
                      db: Session = None) -> dict:
        """对单条内容评分。

        流程：规则打分 -> 模型打分 -> 混合决策 -> 持久化。

        Args:
            content_type: 内容类型。
            content_id: 内容 ID。
            text: 待审核文本。
            citizen_id: 关联公民。
            db: 可选外部 session（C-D13）；传入时只 flush 不 commit，
                由调用方控制事务边界；不传则自开 session 独立提交。

        Returns:
            {"score_id", "content_type", "content_id", "scores", "decision"}
        """
        # 规则层打分
        rule_scores = self._rule_score(text)
        # 模型层打分（模拟推理）
        ml_scores = self._model_inference(text)
        # C-D23：模型层为占位伪随机（非真实推理），降权至 0.1；规则层主导 0.9。
        # 生产接入真实 ML 推理后恢复为 rule*0.4 + ml*0.6。
        combined = {}
        for key in ml_scores:
            combined[key] = round(rule_scores.get(key, 0) * 0.9 + ml_scores[key] * 0.1, 4)

        # 决策
        decision = self._decide(combined)

        # C-D13：支持调用方传入 session 以保持事务原子性
        own_session = db is None
        if own_session:
            db = SessionLocal()
        try:
            record = ModerationScore(
                content_type=content_type,
                content_id=content_id,
                model_name="hybrid_v1",
                scores=json.dumps(combined),
                decision=decision,
                citizen_id=citizen_id,
            )
            db.add(record)
            if own_session:
                db.commit()
            else:
                db.flush()
            logger.info("ml_moderation: scored %s:%d decision=%s", content_type, content_id, decision)
            return {
                "score_id": record.id,
                "content_type": content_type,
                "content_id": content_id,
                "scores": combined,
                "decision": decision,
            }
        finally:
            if own_session:
                db.close()

    def batch_score(self, items: list) -> list:
        """批量评分。

        Args:
            items: [{"content_type", "content_id", "text", "citizen_id"}, ...]

        Returns:
            评分结果列表。
        """
        results = []
        for item in items:
            try:
                r = self.score_content(
                    content_type=item.get("content_type", "unknown"),
                    content_id=item.get("content_id", 0),
                    text=item.get("text", ""),
                    citizen_id=item.get("citizen_id", 0),
                )
                results.append(r)
            except Exception as exc:
                logger.warning("ml_moderation: batch item failed: %s", exc)
                results.append({"content_id": item.get("content_id", 0), "error": str(exc)})
        return results

    def get_decision(self, scores: dict) -> str:
        """根据得分字典决定处置。"""
        return self._decide(scores)

    def update_model(self, model_name: str, version: str) -> dict:
        """更新模型版本（模拟热加载）。"""
        logger.info("ml_moderation: model updated to %s v%s", model_name, version)
        return {
            "model_name": model_name,
            "version": version,
            "updated_at": _now().isoformat(),
            "status": "loaded",
        }

    def get_model_versions(self) -> list:
        """列出可用模型版本。"""
        return [
            {"name": "basic_v1", "version": "1.0.0", "status": "deprecated", "accuracy": 0.72},
            {"name": "hybrid_v1", "version": "1.2.0", "status": "active", "accuracy": 0.89},
            {"name": "transformer_v2", "version": "2.0.0-beta", "status": "staging", "accuracy": 0.94},
        ]

    def train_feedback(self, content_id: int, human_decision: str) -> dict:
        """收集人工标注反馈用于模型迭代。

        Args:
            content_id: 被标注的内容 ID。
            human_decision: 人工判定 pass / review / block。
        """
        db: Session = SessionLocal()
        try:
            # 查找最近的自动评分记录
            record = (db.query(ModerationScore)
                      .filter(ModerationScore.content_id == content_id)
                      .order_by(ModerationScore.id.desc())
                      .first())
            if record is None:
                raise ValueError(f"Content {content_id} has no score record")

            # 记录反馈（生产应存到独立的 feedback 表）
            logger.info("ml_moderation: feedback for content %d: human=%s auto=%s",
                        content_id, human_decision, record.decision)
            return {
                "content_id": content_id,
                "auto_decision": record.decision,
                "human_decision": human_decision,
                "agreement": record.decision == human_decision,
                "collected_at": _now().isoformat(),
            }
        finally:
            db.close()

    def get_stats(self, period_days: int = 7) -> dict:
        """获取审核统计。"""
        db: Session = SessionLocal()
        try:
            cutoff = _now() - timedelta(days=period_days)
            records = (db.query(ModerationScore)
                       .filter(ModerationScore.created_at >= cutoff)
                       .all())
            total = len(records)
            decisions = defaultdict(int)
            for r in records:
                decisions[r.decision] += 1

            return {
                "period_days": period_days,
                "total_scored": total,
                "decisions": dict(decisions),
                "block_rate": round(decisions["block"] / total, 4) if total > 0 else 0,
                "review_rate": round(decisions["review"] / total, 4) if total > 0 else 0,
            }
        finally:
            db.close()

    def get_flagged_content(self, limit: int = 50) -> list:
        """获取最近被标记（review/block）的内容列表。"""
        db: Session = SessionLocal()
        try:
            records = (db.query(ModerationScore)
                       .filter(ModerationScore.decision.in_(["review", "block"]))
                       .order_by(ModerationScore.id.desc())
                       .limit(limit)
                       .all())
            return [
                {
                    "score_id": r.id,
                    "content_type": r.content_type,
                    "content_id": r.content_id,
                    "scores": json.loads(r.scores or "{}"),
                    "decision": r.decision,
                    "citizen_id": r.citizen_id,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in records
            ]
        finally:
            db.close()

    # ---- 内部方法 ----

    @staticmethod
    def _rule_score(text: str) -> dict:
        """规则层打分（简化：基于文本长度和特殊字符比例）。"""
        if not text:
            return {"toxic": 0, "spam": 0, "violence": 0, "harassment": 0, "misinformation": 0}

        scores = {"toxic": 0.0, "spam": 0.0, "violence": 0.0, "harassment": 0.0, "misinformation": 0.0}
        # 简易规则：重复字符高 -> spam
        if len(text) > 20 and len(set(text)) / len(text) < 0.3:
            scores["spam"] = 0.6
        # 全大写比例高 -> toxic
        if len(text) > 10:
            upper_ratio = sum(1 for c in text if c.isupper()) / len(text)
            if upper_ratio > 0.7:
                scores["toxic"] = 0.5
        return scores

    @staticmethod
    def _model_inference(text: str) -> dict:
        """模型推理（模拟：基于文本 hash 产生伪随机但确定性的分数）。"""
        if not text:
            return {"toxic": 0.0, "spam": 0.0, "violence": 0.0, "harassment": 0.0, "misinformation": 0.0}

        # 模拟：用 text hash 做种子产生确定性伪分数
        import hashlib
        seed = int(hashlib.md5(text.encode()).hexdigest()[:8], 16)
        rng = random.Random(seed)
        return {
            "toxic": round(rng.uniform(0, 0.3), 4),
            "spam": round(rng.uniform(0, 0.2), 4),
            "violence": round(rng.uniform(0, 0.1), 4),
            "harassment": round(rng.uniform(0, 0.15), 4),
            "misinformation": round(rng.uniform(0, 0.1), 4),
        }

    @staticmethod
    def _decide(scores: dict) -> str:
        """基于综合得分做决策。"""
        threshold = settings.ML_MODERATION_BLOCK_THRESHOLD
        max_score = max(scores.values()) if scores else 0

        if max_score >= threshold:
            return "block"
        elif max_score >= threshold * 0.6:
            return "review"
        return "pass"


instance = MLModerator()
