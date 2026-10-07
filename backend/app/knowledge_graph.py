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
"""知识图谱服务（P1 增强）。

功能：
- 添加图边；
- BFS 获取关联节点；
- 路径查找；
- 简单模式查询；
- 从现有数据重建图边（注册为 daily job）。

依赖模型：KnowledgeGraphEdge。
"""
import logging
from collections import deque
from datetime import datetime, timedelta

from .config import settings
from .database import SessionLocal
from .models import KnowledgeGraphEdge

logger = logging.getLogger(__name__)


def _now():
    return datetime.utcnow()


class KnowledgeGraphService:
    """知识图谱关系管理。"""

    def add_edge(self, db, source_type: str, source_id: int,
                 target_type: str, target_id: int, relation: str,
                 weight: float = 1.0):
        """添加知识图谱边。"""
        edge = KnowledgeGraphEdge(
            source_type=source_type,
            source_id=source_id,
            target_type=target_type,
            target_id=target_id,
            relation=relation,
            weight=weight,
        )
        db.add(edge)
        db.commit()
        return edge

    def get_neighbors(self, db, node_type: str, node_id: int,
                      relation: str = None, depth: int = 1) -> list:
        """BFS 获取关联节点。

        Args:
            db: SQLAlchemy session。
            node_type: 起始节点类型。
            node_id: 起始节点 ID。
            relation: 限定关系类型（None 则所有）。
            depth: 搜索深度。

        Returns:
            关联节点列表 [{"type": str, "id": int, "relation": str, "depth": int}]。
        """
        visited = set()
        results = []
        queue = deque()
        queue.append((node_type, node_id, 0))
        visited.add((node_type, node_id))

        while queue:
            cur_type, cur_id, cur_depth = queue.popleft()
            if cur_depth >= depth:
                continue
            query = db.query(KnowledgeGraphEdge).filter(
                KnowledgeGraphEdge.source_type == cur_type,
                KnowledgeGraphEdge.source_id == cur_id,
            )
            if relation:
                query = query.filter(KnowledgeGraphEdge.relation == relation)
            edges = query.all()
            for e in edges:
                neighbor_key = (e.target_type, e.target_id)
                if neighbor_key not in visited:
                    visited.add(neighbor_key)
                    results.append({
                        "type": e.target_type,
                        "id": e.target_id,
                        "relation": e.relation,
                        "weight": e.weight,
                        "depth": cur_depth + 1,
                    })
                    queue.append((e.target_type, e.target_id, cur_depth + 1))
        return results

    def find_path(self, db, source_type: str, source_id: int,
                  target_type: str, target_id: int, max_depth: int = 5) -> list:
        """BFS 查找两节点间最短路径。

        Returns:
            路径列表（空表示无路径）: [{"type": str, "id": int, "relation": str}]
        """
        visited = set()
        queue = deque()
        start = (source_type, source_id)
        target = (target_type, target_id)
        queue.append((start, [], 0))
        visited.add(start)

        while queue:
            (cur_type, cur_id), path, depth = queue.popleft()
            if depth >= max_depth:
                continue
            edges = db.query(KnowledgeGraphEdge).filter(
                KnowledgeGraphEdge.source_type == cur_type,
                KnowledgeGraphEdge.source_id == cur_id,
            ).all()
            for e in edges:
                neighbor = (e.target_type, e.target_id)
                if neighbor in visited:
                    continue
                visited.add(neighbor)
                new_path = path + [{"type": cur_type, "id": cur_id,
                                    "relation": e.relation}]
                if neighbor == target:
                    new_path.append({"type": e.target_type, "id": e.target_id,
                                     "relation": ""})
                    return new_path
                queue.append((neighbor, new_path, depth + 1))
        return []

    def query_pattern(self, db, pattern: dict) -> list:
        """简单模式查询。

        pattern 示例: {"source_type": "ai", "relation": "completed", "target_type": "project"}

        Returns:
            匹配的边列表。
        """
        query = db.query(KnowledgeGraphEdge)
        if "source_type" in pattern:
            query = query.filter(KnowledgeGraphEdge.source_type == pattern["source_type"])
        if "source_id" in pattern:
            query = query.filter(KnowledgeGraphEdge.source_id == pattern["source_id"])
        if "relation" in pattern:
            query = query.filter(KnowledgeGraphEdge.relation == pattern["relation"])
        if "target_type" in pattern:
            query = query.filter(KnowledgeGraphEdge.target_type == pattern["target_type"])
        if "target_id" in pattern:
            query = query.filter(KnowledgeGraphEdge.target_id == pattern["target_id"])
        edges = query.limit(100).all()
        return [
            {
                "source_type": e.source_type,
                "source_id": e.source_id,
                "relation": e.relation,
                "target_type": e.target_type,
                "target_id": e.target_id,
                "weight": e.weight,
            }
            for e in edges
        ]

    def rebuild_from_events(self, db):
        """从现有数据重建图边（注册为 daily job）。

        当前为占位实现：可在此扫描 contracts, projects 等表重建关系。
        """
        # 占位：实际场景从 contracts 重建 ai->completed->contract 边
        logger.info("rebuild_from_events: placeholder, no rebuild performed")
        return 0


knowledge_graph = KnowledgeGraphService()
