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
"""AI 互信网络服务。

功能：
- 信任增/减（boost/reduce_trust）：互动后调整信任分值；
- 信任查询（get_trust_score）：特定上下文的信任度；
- 综合声誉（get_reputation）：所有边加权聚合；
- 时间衰减（decay_all）：按天衰减信任值；
- 可信伙伴查询与推荐。

依赖模型：TrustEdge。
信任值范围：[0.0, 1.0]，默认初始 0.5。
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy import func

from .config import settings
from .database import SessionLocal
from .models import TrustEdge

logger = logging.getLogger(__name__)


def _now():
    return datetime.utcnow()


class TrustNetwork:
    """AI 互信网络业务逻辑。"""

    def boost_trust(self, truster_id: int, trustee_id: int,
                    context: str = "general", delta: float = 0.05) -> dict:
        """增加信任值。"""
        if delta <= 0:
            delta = 0.05

        db = SessionLocal()
        try:
            edge = self._get_or_create_edge(db, truster_id, trustee_id, context)
            old_score = edge.trust_score
            edge.trust_score = min(1.0, edge.trust_score + delta)
            edge.interactions += 1
            edge.last_updated = _now()
            db.commit()

            logger.debug("Trust boosted: %d->%d [%s] %.3f -> %.3f",
                         truster_id, trustee_id, context, old_score, edge.trust_score)
            return {"trust_score": round(edge.trust_score, 4), "delta": delta}
        finally:
            db.close()

    def reduce_trust(self, truster_id: int, trustee_id: int,
                     context: str = "general", delta: float = 0.1) -> dict:
        """减少信任值。"""
        if delta <= 0:
            delta = 0.1

        db = SessionLocal()
        try:
            edge = self._get_or_create_edge(db, truster_id, trustee_id, context)
            old_score = edge.trust_score
            edge.trust_score = max(settings.TRUST_MIN_SCORE, edge.trust_score - delta)
            edge.interactions += 1
            edge.last_updated = _now()
            db.commit()

            logger.debug("Trust reduced: %d->%d [%s] %.3f -> %.3f",
                         truster_id, trustee_id, context, old_score, edge.trust_score)
            return {"trust_score": round(edge.trust_score, 4), "delta": -delta}
        finally:
            db.close()

    def get_trust_score(self, truster_id: int, trustee_id: int,
                        context: str = "general") -> dict:
        """查询特定上下文的信任分值。"""
        db = SessionLocal()
        try:
            edge = (db.query(TrustEdge)
                    .filter(TrustEdge.truster_id == truster_id,
                            TrustEdge.trustee_id == trustee_id,
                            TrustEdge.context == context)
                    .first())
            if edge is None:
                return {"trust_score": 0.5, "interactions": 0, "note": "no history (default)"}

            return {
                "trust_score": round(edge.trust_score, 4),
                "interactions": edge.interactions,
                "context": edge.context,
                "last_updated": edge.last_updated.isoformat() if edge.last_updated else None,
            }
        finally:
            db.close()

    def get_reputation(self, citizen_id: int) -> dict:
        """综合声誉：所有入边的加权平均信任分。"""
        db = SessionLocal()
        try:
            edges = (db.query(TrustEdge)
                     .filter(TrustEdge.trustee_id == citizen_id)
                     .all())
            if not edges:
                return {"citizen_id": citizen_id, "reputation": 0.5, "trustors": 0}

            total_weight = 0.0
            weighted_sum = 0.0
            for e in edges:
                weight = 1.0 + e.interactions * 0.1  # 互动越多权重越高
                weighted_sum += e.trust_score * weight
                total_weight += weight

            reputation = weighted_sum / total_weight if total_weight > 0 else 0.5

            return {
                "citizen_id": citizen_id,
                "reputation": round(reputation, 4),
                "trustors": len(edges),
                "avg_interactions": round(sum(e.interactions for e in edges) / len(edges), 1),
            }
        finally:
            db.close()

    def decay_all(self) -> dict:
        """全局时间衰减：每天衰减 TRUST_DECAY_PER_DAY（向 0.5 中性值收敛）。"""
        db = SessionLocal()
        try:
            now = _now()
            edges = db.query(TrustEdge).all()
            decay = settings.TRUST_DECAY_PER_DAY
            updated = 0

            for e in edges:
                if e.last_updated and (now - e.last_updated).total_seconds() < 86400:
                    continue  # 不足一天不衰减
                # 向 0.5 衰减
                diff = e.trust_score - 0.5
                e.trust_score = 0.5 + diff * (1.0 - decay)
                e.trust_score = max(settings.TRUST_MIN_SCORE, min(1.0, e.trust_score))
                updated += 1

            db.commit()
            logger.info("Trust decay: %d edges updated", updated)
            return {"decayed": updated}
        finally:
            db.close()

    def get_trusted_partners(self, citizen_id: int,
                             min_score: float = 0.7) -> list[dict]:
        """查询指定 AI 的可信伙伴列表。"""
        db = SessionLocal()
        try:
            edges = (db.query(TrustEdge)
                     .filter(TrustEdge.truster_id == citizen_id,
                             TrustEdge.trust_score >= min_score)
                     .order_by(TrustEdge.trust_score.desc())
                     .limit(50)
                     .all())
            return [
                {
                    "partner_id": e.trustee_id,
                    "trust_score": round(e.trust_score, 4),
                    "context": e.context,
                    "interactions": e.interactions,
                }
                for e in edges
            ]
        finally:
            db.close()

    def recommend_partners(self, requester_id: int,
                           context: str = "general") -> list[dict]:
        """推荐合作伙伴：基于信任传递（朋友的朋友）。"""
        db = SessionLocal()
        try:
            # 直接信任的伙伴
            my_trusted = (db.query(TrustEdge.trustee_id)
                          .filter(TrustEdge.truster_id == requester_id,
                                  TrustEdge.trust_score >= 0.7)
                          .subquery())

            # 我信任的人信任的人（二跳）
            candidates = (db.query(
                TrustEdge.trustee_id.label("candidate_id"),
                func.avg(TrustEdge.trust_score).label("avg_trust"),
                func.count(TrustEdge.id).label("ref_count"),
            )
                          .filter(TrustEdge.truster_id.in_(
                              db.query(TrustEdge.trustee_id)
                              .filter(TrustEdge.truster_id == requester_id,
                                      TrustEdge.trust_score >= 0.7)
                          ))
                          .filter(TrustEdge.trustee_id != requester_id)
                          .filter(TrustEdge.context.in_([context, "general"]))
                          .group_by(TrustEdge.trustee_id)
                          .order_by(func.avg(TrustEdge.trust_score).desc())
                          .limit(10)
                          .all())

            return [
                {
                    "citizen_id": c.candidate_id,
                    "avg_trust": round(c.avg_trust, 4),
                    "reference_count": c.ref_count,
                    "context": context,
                }
                for c in candidates
            ]
        finally:
            db.close()

    # ---------- 内部 ----------

    def _get_or_create_edge(self, db, truster_id: int, trustee_id: int,
                            context: str) -> TrustEdge:
        edge = (db.query(TrustEdge)
                .filter(TrustEdge.truster_id == truster_id,
                        TrustEdge.trustee_id == trustee_id,
                        TrustEdge.context == context)
                .first())
        if edge is None:
            edge = TrustEdge(
                truster_id=truster_id,
                trustee_id=trustee_id,
                trust_score=0.5,
                context=context,
                interactions=0,
            )
            db.add(edge)
            db.flush()
        return edge


instance = TrustNetwork()
