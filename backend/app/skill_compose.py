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
"""技能组合 DAG 编排服务。

功能：
- 定义组合（create_composition）：指定 DAG 节点和边；
- 发布/执行/查询状态；
- DAG 执行采用拓扑排序，按层依次执行节点；
- 血缘查询。

依赖模型：SkillComposition, SkillCompositionRun。
DAG JSON 格式: {"nodes": [{"id","skill","params"}], "edges": [{"from","to"}]}
"""
import json
import logging
import time
from datetime import datetime

from .database import SessionLocal
from .models import SkillComposition, SkillCompositionRun

logger = logging.getLogger(__name__)


def _now():
    return datetime.utcnow()


def _topological_sort(nodes: list[dict], edges: list[dict]) -> list[list[str]]:
    """拓扑排序（分层）：返回 [[layer1_node_ids], [layer2_node_ids], ...]。"""
    # 构建邻接表和入度
    in_degree: dict[str, int] = {n["id"]: 0 for n in nodes}
    adjacency: dict[str, list[str]] = {n["id"]: [] for n in nodes}

    for edge in edges:
        src, dst = edge["from"], edge["to"]
        if src in adjacency and dst in in_degree:
            adjacency[src].append(dst)
            in_degree[dst] += 1

    # BFS 分层
    layers: list[list[str]] = []
    queue = [nid for nid, deg in in_degree.items() if deg == 0]

    while queue:
        layers.append(queue)
        next_queue = []
        for nid in queue:
            for neighbor in adjacency.get(nid, []):
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    next_queue.append(neighbor)
        queue = next_queue

    return layers


class SkillComposer:
    """技能组合 DAG 编排业务逻辑。"""

    def create_composition(self, name: str, description: str = "",
                           dag: dict | None = None,
                           author_id: int = 0) -> dict:
        """创建技能组合定义。

        Args:
            dag: {"nodes": [...], "edges": [...]}

        Returns:
            {"composition_id": int}
        """
        if dag is None:
            dag = {"nodes": [], "edges": []}

        # 验证 DAG 无环（拓扑排序必须能完成）
        nodes = dag.get("nodes", [])
        edges = dag.get("edges", [])
        layers = _topological_sort(nodes, edges)
        covered = set()
        for layer in layers:
            covered.update(layer)
        all_ids = {n["id"] for n in nodes}
        if covered != all_ids:
            return {"error": "DAG contains cycle or orphan nodes"}

        db = SessionLocal()
        try:
            comp = SkillComposition(
                name=name,
                description=description,
                dag=json.dumps(dag, ensure_ascii=False),
                author_id=author_id,
                status="draft",
            )
            db.add(comp)
            db.commit()
            logger.info("Composition created: id=%d name=%s nodes=%d",
                        comp.id, name, len(nodes))
            return {"composition_id": comp.id, "name": name}
        finally:
            db.close()

    def publish(self, composition_id: int) -> dict:
        """发布组合（draft -> published）。"""
        db = SessionLocal()
        try:
            comp = db.get(SkillComposition, composition_id)
            if comp is None:
                return {"error": "composition not found"}
            if comp.status != "draft":
                return {"error": f"cannot publish from status '{comp.status}'"}

            comp.status = "published"
            db.commit()
            logger.info("Composition published: id=%d", composition_id)
            return {"ok": True, "status": "published"}
        finally:
            db.close()

    def execute(self, composition_id: int, task_id: int = 0,
                initiator_id: int = 0, inputs: dict | None = None) -> dict:
        """执行组合（拓扑排序逐层执行节点）。

        每个节点的"执行"是模拟的（生成 node_results），
        实际生产应分发到 worker 执行。
        """
        start = time.monotonic()
        db = SessionLocal()
        try:
            comp = db.get(SkillComposition, composition_id)
            if comp is None:
                return {"error": "composition not found"}
            if comp.status != "published":
                return {"error": "composition not published"}

            dag = json.loads(comp.dag or '{"nodes":[],"edges":[]}')
            nodes = dag.get("nodes", [])
            edges = dag.get("edges", [])
            layers = _topological_sort(nodes, edges)

            # 模拟按层执行
            node_results: dict[str, dict] = {}
            total_cost = 0
            failed = False

            for layer_idx, layer in enumerate(layers):
                for node_id in layer:
                    node_def = next((n for n in nodes if n["id"] == node_id), None)
                    if node_def is None:
                        continue

                    # 模拟执行（实际生产中调用 capability.compute）
                    result = {
                        "node_id": node_id,
                        "skill": node_def.get("skill", ""),
                        "status": "success",
                        "layer": layer_idx,
                        "output": {"simulated": True, "inputs": inputs or {}},
                    }
                    node_results[node_id] = result
                    total_cost += 10  # 每节点模拟成本

            duration_ms = int((time.monotonic() - start) * 1000)
            run_status = "failed" if failed else "success"

            run = SkillCompositionRun(
                composition_id=composition_id,
                task_id=task_id,
                initiator_id=initiator_id,
                node_results=json.dumps(node_results, ensure_ascii=False),
                status=run_status,
                total_cost_cent=total_cost,
                duration_ms=duration_ms,
                finished_at=_now(),
            )
            db.add(run)
            comp.usage_count += 1
            db.commit()

            logger.info("Composition executed: run=%d comp=%d nodes=%d status=%s",
                        run.id, composition_id, len(nodes), run_status)
            return {
                "run_id": run.id,
                "status": run_status,
                "nodes_executed": len(node_results),
                "total_cost_cent": total_cost,
                "duration_ms": duration_ms,
            }
        finally:
            db.close()

    def get_execution_status(self, run_id: int) -> dict:
        """查询执行状态。"""
        db = SessionLocal()
        try:
            run = db.get(SkillCompositionRun, run_id)
            if run is None:
                return {"error": "run not found"}
            return {
                "run_id": run.id,
                "composition_id": run.composition_id,
                "status": run.status,
                "task_id": run.task_id,
                "node_results": json.loads(run.node_results or "{}"),
                "total_cost_cent": run.total_cost_cent,
                "duration_ms": run.duration_ms,
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            }
        finally:
            db.close()

    def list_compositions(self, status: str = "") -> list[dict]:
        """列出组合。"""
        db = SessionLocal()
        try:
            q = db.query(SkillComposition)
            if status:
                q = q.filter(SkillComposition.status == status)
            rows = q.order_by(SkillComposition.id.desc()).limit(50).all()
            return [
                {
                    "id": c.id, "name": c.name, "status": c.status,
                    "author_id": c.author_id, "version": c.version,
                    "usage_count": c.usage_count,
                    "node_count": len(json.loads(c.dag or '{"nodes":[]}').get("nodes", [])),
                    "created_at": c.created_at.isoformat() if c.created_at else None,
                }
                for c in rows
            ]
        finally:
            db.close()

    def get_lineage(self, composition_id: int) -> dict:
        """获取组合的血缘（DAG 结构 + 依赖的技能列表）。"""
        db = SessionLocal()
        try:
            comp = db.get(SkillComposition, composition_id)
            if comp is None:
                return {"error": "composition not found"}

            dag = json.loads(comp.dag or '{"nodes":[],"edges":[]}')
            nodes = dag.get("nodes", [])
            skills = list({n.get("skill", "") for n in nodes if n.get("skill")})
            layers = _topological_sort(nodes, dag.get("edges", []))

            return {
                "composition_id": comp.id,
                "name": comp.name,
                "skills_used": skills,
                "layers": layers,
                "total_nodes": len(nodes),
                "total_edges": len(dag.get("edges", [])),
            }
        finally:
            db.close()


instance = SkillComposer()
