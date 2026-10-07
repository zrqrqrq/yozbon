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
"""项目实体 + WBS 依赖图调度引擎（蓝图 §二 表 9 / §四 L9 / §六 规则 9 侧翼）。

职责边界（C2 线）：
- 发布大项目：draft → review（预算≥REVIEW_MANDATORY_BUDGET 强制评审组 3-5 异质）
  → approved（存 review_report_id）→ running（宿主确认预算与拆解后）。
- WBS：分解 AI 提交节点 + 依赖边；依赖图无环检测（Kahn 拓扑排序，成环→400）+ 拓扑序号 seq。
- 调度：前驱 done 节点才可转 matching；里程碑托管 = 每节点独立合约（走 B 线 escrow，
  本模块只驱动节点状态机 pending→matching→signed→executing→accepted→done，不实现合约细节）。
- PM 总管：管理费 PROJECT_FEE_PM_RATE，按节点结算时从节点预算口径计提一次
  （规则 9 侧翼：里程碑托管不重复扣费——每节点仅计提一次，幂等 ref 兜底）。

注意：金额一律 integer 分；服务内只 flush，commit 由路由层负责。
"""
from collections import defaultdict, deque

from sqlalchemy.orm import Session

from .config import settings
from .database import register_index
from .models import (AICitizen, NodeDep, Project, ProjectNode, ReviewPanel,
                     ReviewReport)
from . import wallet


class ProjectError(Exception):
    """项目/WBS 业务异常（路由层映射 HTTP 400）。"""


# ---- 组合索引（约定 §0 铁律 3：不改 models.py，import 时注册，init_db 幂等执行）----
# project_nodes(project_id, status)：按项目拉各状态节点（调度/看板查询路径）
register_index("CREATE INDEX IF NOT EXISTS idx_nodes_proj_status "
               "ON project_nodes(project_id, status)")
# node_deps(node_id)：查某节点的前驱依赖（调度判定路径）；uq_dep_ai 已兜底边唯一
register_index("CREATE INDEX IF NOT EXISTS idx_nodedeps_node "
               "ON node_deps(node_id, dep_node_id)")


# ---------------- 评审组异质性校验 ----------------
def _validate_panel_members(db: Session, reviewer_ids: list) -> list:
    """校验评审组 3-5 名「异质」AI：不重复、不同宿主、不同职业标签（防串通刷票）。"""
    ids = list(dict.fromkeys(int(i) for i in reviewer_ids))
    lo, hi = settings.REVIEW_PANEL_MIN, settings.REVIEW_PANEL_MAX
    if not (lo <= len(ids) <= hi):
        raise ProjectError(f"Review panel requires {lo}-{hi} heterogeneous AIs (received {len(ids)}) ")
    members = [db.get(AICitizen, i) for i in ids]
    if any(m is None for m in members):
        raise ProjectError("A review AI does not exist")
    host_ids = [m.host_id for m in members]
    if len(set(host_ids)) != len(members):
        raise ProjectError("Review panel must be heterogeneous: not from the same host (anti-collusion)")
    occs = [(m.occupation or "General") for m in members]
    if len(set(occs)) != len(members):
        raise ProjectError("Review panel must be heterogeneous: occupation/skill tags must not be identical")
    return ids


# ---------------- 发布大项目 ----------------
def create_project(db: Session, host_id: int, title: str, budget_cent: int,
                   deadline=None, pm_citizen_id: int = 0,
                   reviewer_ids: list | None = None) -> Project:
    """宿主发布大项目。

    - 预算 ≥ REVIEW_MANDATORY_BUDGET(200AC)：强制评审组（3-5 异质），状态 → review。
    - 预算 < 线：抽样 REVIEW_SAMPLE_RATE（MVP 以「显式传入 reviewer_ids」代表被抽中/强制评审），
      未传入则免评审直接 approved，宿主可直接 confirm 进入 running。
    评审聚合由 governance.finalize_review_panel 在评审齐票后回写 approved + review_report_id。
    """
    if budget_cent <= 0:
        raise ProjectError("Budget must be positive (cents)")
    p = Project(host_id=host_id, title=title, budget_cent=budget_cent,
                deadline=deadline, status="draft", pm_citizen_id=pm_citizen_id or 0)
    db.add(p)
    db.flush()

    need_review = budget_cent >= settings.REVIEW_MANDATORY_BUDGET
    reviewers = reviewer_ids or []
    if need_review:
        ids = _validate_panel_members(db, reviewers)
        panel = ReviewPanel(project_id=p.id, member_ids=_json(ids), status="voting")
        db.add(panel)
        p.status = "review"
    elif reviewers:
        # 低于强制线但被抽样/宿主主动要求评审：同样组评审组
        ids = _validate_panel_members(db, reviewers)
        panel = ReviewPanel(project_id=p.id, member_ids=_json(ids), status="voting")
        db.add(panel)
        p.status = "review"
    else:
        # 未触发评审：直接进入 approved，等待宿主确认运行
        p.status = "approved"
    db.flush()
    return p


def get_project(db: Session, project_id: int) -> Project:
    p = db.get(Project, project_id)
    if p is None:
        raise ProjectError(f"Project {project_id} not found")
    return p


def list_projects(db: Session, host_id: int) -> list:
    rows = (db.query(Project).filter(Project.host_id == host_id)
            .order_by(Project.id.desc()).all())
    return [{"id": r.id, "title": r.title, "budget_cent": r.budget_cent,
             "status": r.status, "pm_citizen_id": r.pm_citizen_id,
             "review_report_id": r.review_report_id,
             "created_at": r.created_at.isoformat() if r.created_at else ""}
            for r in rows]


# ---------------- WBS 节点分解（无环检测 + 拓扑序） ----------------
def _json(x) -> str:
    import json
    return json.dumps(x, ensure_ascii=False)


def _topo_order(key_to_id: dict, edges: list) -> list:
    """Kahn 拓扑排序。

    edges: [(node_id, dep_node_id)] —— node_id 依赖 dep_node_id（前驱先做）。
    返回拓扑序的 node_id 列表；成环时抛出 ProjectError。
    """
    nodes = list(key_to_id.values())
    indeg = {n: 0 for n in nodes}
    adj = defaultdict(list)          # 前驱 -> 后继
    for node_id, dep_id in edges:
        adj[dep_id].append(node_id)
        indeg[node_id] += 1
    q = deque([n for n in nodes if indeg[n] == 0])
    order = []
    while q:
        cur = q.popleft()
        order.append(cur)
        for nxt in adj[cur]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                q.append(nxt)
    if len(order) != len(nodes):
        raise ProjectError("WBS dependency graph has a cycle (circular dependency); decomposition rejected")
    return order


def submit_nodes(db: Session, project_id: int, nodes: list, deps: list) -> dict:
    """分解 AI 提交 WBS（多个 project_nodes + node_deps 依赖边）。

    nodes: [{"key","skill","spec","deliverable_std","budget_cent","duration_h"}]
    deps:  [{"from":<前驱key>,"to":<后继key>}]  —— to 依赖 from，from 必须先 done。
    依赖图先在内存做无环检测，通过后才落库；拓扑序号 seq 按 Kahn 序赋值。
    """
    p = get_project(db, project_id)
    if p.status not in ("approved", "running"):
        raise ProjectError(f"Project status {p.status} does not accept WBS decomposition (requires approved/running)")
    if not nodes:
        raise ProjectError("Node list is empty")
    # 同一项目只允许一次性拆解（重复提交会造成依赖边歧义）
    if db.query(ProjectNode).filter(ProjectNode.project_id == project_id).count() > 0:
        raise ProjectError("This project has already submitted a WBS; duplicate decomposition not allowed")

    key_to_node = {}
    objs = []
    for n in nodes:
        key = n.get("key") or n.get("id")
        if key is None:
            raise ProjectError("Each node must contain a unique key")
        node = ProjectNode(project_id=project_id, skill=n.get("skill", ""),
                           spec=n.get("spec", ""),
                           deliverable_std=n.get("deliverable_std", ""),
                           budget_cent=int(n.get("budget_cent", 0)),
                           duration_h=int(n.get("duration_h", 24)),
                           status="pending", seq=0)
        db.add(node)
        objs.append(node)
        key_to_node[str(key)] = node
    db.flush()  # 拿到 node.id

    # 依赖边（内存）：to 依赖 from → node_id=to.id, dep_node_id=from.id
    edges = []
    edge_pairs = []
    for d in deps:
        a = key_to_node.get(str(d.get("from")))   # 前驱
        b = key_to_node.get(str(d.get("to")))     # 后继
        if a is None or b is None:
            raise ProjectError("A dependency edge references a non-existent node key")
        if a.id == b.id:
            raise ProjectError("A node cannot depend on itself")
        edges.append((b.id, a.id))
        edge_pairs.append((b.id, a.id))

    # 无环检测 + 拓扑序（落库前）
    order = _topo_order({k: v.id for k, v in key_to_node.items()}, edges)
    seq_of = {nid: i + 1 for i, nid in enumerate(order)}

    for n in objs:
        n.seq = seq_of[n.id]
    for node_id, dep_id in edge_pairs:
        db.add(NodeDep(node_id=node_id, dep_node_id=dep_id))
    db.flush()

    # 拆解完成后若项目已在 running，立即推进一轮调度（无依赖节点转 matching）
    if p.status == "running":
        advance_scheduling(db, project_id)
    return {"project_id": project_id,
            "nodes": [{"id": n.id, "key": _find_key(key_to_node, n.id),
                       "seq": n.seq, "status": n.status} for n in objs]}


def _find_key(key_to_node: dict, node_id: int) -> str:
    for k, v in key_to_node.items():
        if v.id == node_id:
            return k
    return ""


# ---------------- 调度引擎 ----------------
def _deps_of(db: Session, node_id: int) -> list:
    """返回某节点的全部前驱 node 对象。"""
    rows = db.query(NodeDep).filter(NodeDep.node_id == node_id).all()
    dep_ids = [r.dep_node_id for r in rows]
    if not dep_ids:
        return []
    return db.query(ProjectNode).filter(ProjectNode.id.in_(dep_ids)).all()


def advance_scheduling(db: Session, project_id: int) -> list:
    """调度：前驱全部 done 的 pending 节点 → matching（可被 B 线市场撮合）。

    规则：只有当某节点的所有依赖节点都已 done，该节点才允许进入 matching；
    否则保持 pending（拓扑阻塞）。返回新转为 matching 的节点 id 列表。
    """
    nodes = db.query(ProjectNode).filter(ProjectNode.project_id == project_id).all()
    newly = []
    for n in nodes:
        if n.status != "pending":
            continue
        deps = _deps_of(db, n.id)
        if deps and any(d.status != "done" for d in deps):
            continue  # 前驱未全部完成，保持 pending
        n.status = "matching"
        newly.append(n.id)
    db.flush()
    return newly


# ---------------- 节点状态机（驱动 B 线合约回调；不实现合约细节） ----------------
def _require_node(node: ProjectNode, want: tuple) -> None:
    if node.status not in want:
        raise ProjectError(f"Node {node.id} current status {node.status}; this operation is not allowed")


def mark_node_signed(db: Session, node_id: int) -> ProjectNode:
    node = db.get(ProjectNode, node_id)
    if node is None:
        raise ProjectError(f"Node {node_id} not found")
    _require_node(node, ("matching",))
    node.status = "signed"
    db.flush()
    return node


def mark_node_executing(db: Session, node_id: int) -> ProjectNode:
    node = db.get(ProjectNode, node_id)
    if node is None:
        raise ProjectError(f"Node {node_id} not found")
    _require_node(node, ("signed",))
    node.status = "executing"
    db.flush()
    return node


def mark_node_accepted(db: Session, node_id: int) -> ProjectNode:
    node = db.get(ProjectNode, node_id)
    if node is None:
        raise ProjectError(f"Node {node_id} not found")
    _require_node(node, ("executing",))
    node.status = "accepted"
    db.flush()
    return node


def complete_node(db: Session, node_id: int) -> dict:
    """节点验收完成 → done：计提 PM 管理费（每节点一次，幂等）+ 推进后续调度。

    规则 9 侧翼：里程碑托管不重复——PM 管理费按节点预算口径仅在该节点 done 时计提一次，
    幂等 ref=pmfee:node:<id> 兜底，重复调用不会二次入账。
    （B 线 escrow 的工人结算费在此之外，本模块不重复扣交易手续费。）
    """
    node = db.get(ProjectNode, node_id)
    if node is None:
        raise ProjectError(f"Node {node_id} not found")
    if node.status == "done":
        return {"node_id": node.id, "status": "done", "already": True}
    _require_node(node, ("matching", "signed", "executing", "accepted"))
    node.status = "done"

    # PM 管理费（按节点预算计提一次）
    p = db.get(Project, node.project_id)
    pm_fee = 0
    if p and p.pm_citizen_id:
        pm_fee = round(node.budget_cent * settings.PROJECT_FEE_PM_RATE)
        if pm_fee > 0:
            try:
                wallet.credit(db, p.pm_citizen_id, pm_fee, "管理费",
                              ref=f"pmfee:node:{node.id}",
                              note=f"project {node.project_id} node {node.id} management fee")
            except wallet.WalletError:
                # 幂等：该节点管理费已计提过（重复 complete），跳过
                pm_fee = 0
    db.flush()

    # 前驱完成 → 唤醒后继
    advance_scheduling(db, node.project_id)
    return {"node_id": node.id, "status": "done", "pm_fee_cent": pm_fee}


# ---------------- 宿主确认运行 ----------------
def approve_running(db: Session, host_id: int, project_id: int) -> Project:
    """宿主确认预算与拆解 → approved → running，启动调度。"""
    p = get_project(db, project_id)
    if p.host_id != host_id:
        raise ProjectError("No permission to operate on others' projects")
    if p.status != "approved":
        raise ProjectError(f"Project status {p.status}; must be approved to confirm execution")
    p.status = "running"
    db.flush()
    advance_scheduling(db, project_id)
    return p


def get_review_report(db: Session, project_id: int) -> ReviewReport | None:
    p = get_project(db, project_id)
    if not p.review_report_id:
        return None
    return db.get(ReviewReport, p.review_report_id)
