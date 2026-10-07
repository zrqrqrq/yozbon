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
"""P3 社交图谱分析服务。

基于 SocialGraphEdge 表构建图结构，提供：
- 边操作（增/改/查）；
- BFS 最短路径；
- 简单社区发现（连通分量）；
- 影响力排名（加权度中心性）；
- 互友查询；
- 图统计。

关系类型: collaborator / mentor / friend / rival
权重: 关系强度（0~inf，由业务累积）。
"""
import logging
from collections import defaultdict, deque
from datetime import datetime

from sqlalchemy.orm import Session

from .database import SessionLocal
from .models import SocialGraphEdge

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.utcnow()


class SocialGraphAnalyzer:
    """社交图谱分析服务。"""

    def add_edge(self, from_id: int, to_id: int, relation_type: str, weight: float = 1.0) -> dict:
        """添加图谱边（双向存储有向边）。"""
        if from_id == to_id:
            raise ValueError("Cannot add an edge to itself")

        db: Session = SessionLocal()
        try:
            existing = (db.query(SocialGraphEdge)
                        .filter(SocialGraphEdge.from_id == from_id,
                                SocialGraphEdge.to_id == to_id,
                                SocialGraphEdge.relation_type == relation_type)
                        .first())
            if existing:
                existing.weight += weight
                existing.interactions += 1
                existing.last_interaction = _now()
                db.commit()
                return {"action": "updated", "from_id": from_id, "to_id": to_id, "weight": existing.weight}

            edge = SocialGraphEdge(
                from_id=from_id, to_id=to_id,
                relation_type=relation_type,
                weight=weight, interactions=1,
            )
            db.add(edge)
            db.commit()
            logger.info("social_graph: added edge %d -> %d type=%s weight=%.2f",
                        from_id, to_id, relation_type, weight)
            return {"action": "created", "from_id": from_id, "to_id": to_id, "weight": weight}
        finally:
            db.close()

    def update_edge(self, from_id: int, to_id: int, relation_type: str, delta_weight: float) -> dict:
        """更新边权重（累加 delta）。"""
        db: Session = SessionLocal()
        try:
            edge = (db.query(SocialGraphEdge)
                    .filter(SocialGraphEdge.from_id == from_id,
                            SocialGraphEdge.to_id == to_id,
                            SocialGraphEdge.relation_type == relation_type)
                    .first())
            if edge is None:
                raise ValueError(f"Edge {from_id}->{to_id} ({relation_type}) not found")

            edge.weight = max(0, edge.weight + delta_weight)
            edge.interactions += 1
            edge.last_interaction = _now()
            db.commit()
            return {"from_id": from_id, "to_id": to_id, "new_weight": edge.weight}
        finally:
            db.close()

    def get_connections(self, citizen_id: int, relation_type: str = None, limit: int = 50) -> list:
        """获取指定节点的所有连接。"""
        db: Session = SessionLocal()
        try:
            q = (db.query(SocialGraphEdge)
                 .filter(SocialGraphEdge.from_id == citizen_id))
            if relation_type:
                q = q.filter(SocialGraphEdge.relation_type == relation_type)
            edges = q.order_by(SocialGraphEdge.weight.desc()).limit(limit).all()
            return [
                {
                    "to_id": e.to_id,
                    "relation_type": e.relation_type,
                    "weight": e.weight,
                    "interactions": e.interactions,
                    "last_interaction": e.last_interaction.isoformat() if e.last_interaction else None,
                }
                for e in edges
            ]
        finally:
            db.close()

    def find_communities(self, min_size: int = 3) -> list:
        """社区发现：基于连通分量的简单实现。

        返回大小 >= min_size 的连通分量列表。
        """
        db: Session = SessionLocal()
        try:
            edges = db.query(SocialGraphEdge).all()
            # 构建无向邻接表
            adj = defaultdict(set)
            nodes = set()
            for e in edges:
                adj[e.from_id].add(e.to_id)
                adj[e.to_id].add(e.from_id)
                nodes.add(e.from_id)
                nodes.add(e.to_id)

            # BFS 连通分量
            visited = set()
            communities = []
            for node in nodes:
                if node in visited:
                    continue
                component = set()
                queue = deque([node])
                while queue:
                    n = queue.popleft()
                    if n in visited:
                        continue
                    visited.add(n)
                    component.add(n)
                    for neighbor in adj[n]:
                        if neighbor not in visited:
                            queue.append(neighbor)
                if len(component) >= min_size:
                    communities.append(sorted(component))

            communities.sort(key=len, reverse=True)
            return [
                {"community_id": i + 1, "members": c, "size": len(c)}
                for i, c in enumerate(communities)
            ]
        finally:
            db.close()

    def get_influencers(self, metric: str = "weighted_degree", limit: int = 20) -> list:
        """影响力排名。

        metric: weighted_degree（加权入度+出度之和）或 interactions（总交互次数）。
        """
        db: Session = SessionLocal()
        try:
            edges = db.query(SocialGraphEdge).all()
            scores = defaultdict(float)
            interaction_count = defaultdict(int)

            for e in edges:
                scores[e.from_id] += e.weight
                scores[e.to_id] += e.weight
                interaction_count[e.from_id] += e.interactions
                interaction_count[e.to_id] += e.interactions

            if metric == "interactions":
                ranked = sorted(interaction_count.items(), key=lambda x: x[1], reverse=True)
            else:
                ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)

            return [
                {"citizen_id": cid, "score": round(score, 2), "rank": i + 1}
                for i, (cid, score) in enumerate(ranked[:limit])
            ]
        finally:
            db.close()

    def get_mutual_connections(self, id_a: int, id_b: int) -> list:
        """查询两个节点的互友列表。"""
        db: Session = SessionLocal()
        try:
            a_connections = set(
                e.to_id for e in db.query(SocialGraphEdge).filter(SocialGraphEdge.from_id == id_a).all()
            )
            b_connections = set(
                e.to_id for e in db.query(SocialGraphEdge).filter(SocialGraphEdge.from_id == id_b).all()
            )
            mutual = a_connections & b_connections
            return sorted(mutual)
        finally:
            db.close()

    def shortest_path(self, id_a: int, id_b: int) -> dict:
        """BFS 最短路径。

        Returns:
            {"path": [node_ids], "distance": int} 或 {"path": [], "distance": -1} 不可达。
        """
        db: Session = SessionLocal()
        try:
            edges = db.query(SocialGraphEdge).all()
            adj = defaultdict(set)
            for e in edges:
                adj[e.from_id].add(e.to_id)
                adj[e.to_id].add(e.from_id)

            if id_a == id_b:
                return {"path": [id_a], "distance": 0}

            # BFS
            queue = deque([(id_a, [id_a])])
            visited = {id_a}
            while queue:
                current, path = queue.popleft()
                for neighbor in adj[current]:
                    if neighbor == id_b:
                        return {"path": path + [neighbor], "distance": len(path)}
                    if neighbor not in visited:
                        visited.add(neighbor)
                        queue.append((neighbor, path + [neighbor]))

            return {"path": [], "distance": -1, "reason": "Unreachable"}
        finally:
            db.close()

    def get_graph_stats(self) -> dict:
        """获取图统计信息。"""
        db: Session = SessionLocal()
        try:
            total_edges = db.query(SocialGraphEdge).count()
            if total_edges == 0:
                return {"total_edges": 0, "total_nodes": 0, "total_relations": {}}

            edges = db.query(SocialGraphEdge).all()
            nodes = set()
            relations = defaultdict(int)
            weights = []
            for e in edges:
                nodes.add(e.from_id)
                nodes.add(e.to_id)
                relations[e.relation_type] += 1
                weights.append(e.weight)

            avg_weight = sum(weights) / len(weights) if weights else 0
            return {
                "total_edges": total_edges,
                "total_nodes": len(nodes),
                "avg_weight": round(avg_weight, 2),
                "total_relations": dict(relations),
                "density": round(total_edges / (len(nodes) * (len(nodes) - 1)), 4) if len(nodes) > 1 else 0,
            }
        finally:
            db.close()


instance = SocialGraphAnalyzer()
