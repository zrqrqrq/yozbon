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
"""供需撮合服务（蓝图 §二 表 9 project_nodes；蓝图 §三 GET /api/ai/jobs）。

jobs 检索：活跃项目节点 = project_nodes.status='matching' 且（可选）skill 匹配，
按「技能匹配 × 信用分 × 报价」综合排序，分页。

投标/议价状态机（沿用 project_nodes.status，不另建投标表）：
- 投标 = 建一条 Contract(status='proposed') 要约行（worker=投标 AI，buyer=项目总管 AI）；
- 报价存 terms_json.offer_cent；
- 投标防重复：同一 worker 对同一 node 只能有一条 active 要约
  （代码校验 + 部分唯一索引 uq_bid_proposal 双保险）；
- 中标后由买方 accept 签约 → node.status='signed'（见 escrow.sign_contract）。
"""
import json

from sqlalchemy.orm import Session

from .database import register_index
from .lang import detect_lang
from .models import (AICitizen, Contract, Host, Project, ProjectNode)


# ---------------- 组合索引注册（import 时执行，init_db 统一幂等建） ----------------
# 市场检索主路径：按 status+matching 拉节点、按 skill 过滤。
register_index("CREATE INDEX IF NOT EXISTS idx_pn_status_skill "
               "ON project_nodes(status, skill)")
# 投标防重复双保险之二：同一 (node,worker) 在 proposed 态唯一（部分唯一索引）。
register_index("CREATE UNIQUE INDEX IF NOT EXISTS uq_bid_proposal "
               "ON contracts(node_id, worker_id) WHERE status='proposed'")


class MarketError(Exception):
    """撮合/投标业务异常。路由层映射为 HTTP 400/404/409。"""


def list_jobs(db: Session, skill: str = "", limit: int = 20, offset: int = 0) -> dict:
    """可接任务检索（蓝图 §三 GET /api/ai/jobs）。

    排序因子（综合）：
      1) 技能匹配：请求带 skill 时，skill 精确相等的节点排前（其余按相关近似排后）；
      2) 信用分：买方所属宿主的 host_credit 高者优先（找靠谱东家）；
      3) 报价：节点预算 budget_cent 高者优先（同条件下报酬高者优先）。
    分页 limit≤50。
    """
    limit = min(max(int(limit), 1), 50)
    offset = max(int(offset), 0)
    q = (db.query(ProjectNode, Project, Host)
           .join(Project, ProjectNode.project_id == Project.id)
           .outerjoin(Host, Project.host_id == Host.id)
           .filter(ProjectNode.status == "matching"))
    if skill:
        q = q.filter(ProjectNode.skill == skill)
    total = q.count()
    # 排序：先 skill 精确（带 skill 时恒等，不影响），再宿主信用 desc，再预算 desc
    rows = (q.order_by(Host.host_credit.desc().nullslast(),
                       ProjectNode.budget_cent.desc(),
                       ProjectNode.id.asc())
              .limit(limit).offset(offset).all())
    items = []
    for node, proj, host in rows:
        items.append({
            "node_id": node.id,
            "project_id": node.project_id,
            "skill": node.skill,
            "spec": node.spec,
            "deliverable_std": node.deliverable_std,
            "budget_cent": node.budget_cent,
            "duration_h": node.duration_h,
            "status": node.status,
            "host_credit": host.host_credit if host else 100,
            "host_id": proj.host_id,
        })
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def _get_matching_node(db: Session, node_id: int) -> ProjectNode:
    node = db.get(ProjectNode, node_id)
    if node is None:
        raise MarketError(f"Node {node_id} not found")
    if node.status != "matching":
        raise MarketError(f"Node status={node.status}; not in biddable matching state")
    return node


def bid(db: Session, worker: AICitizen, node_id: int, offer_cent: int,
        message: str = "") -> Contract:
    """worker 对匹配节点投标：建一条 proposed 要约行。

    - buyer 解析为项目总管 AI（projects.pm_citizen_id）；未指定总管 AI 不能投标；
    - 防重复：该 worker 对该 node 已有 active(proposed) 要约 → 409。
    """
    if offer_cent <= 0:
        raise MarketError("Quote must be a positive integer in cents")
    node = _get_matching_node(db, node_id)
    proj = db.get(Project, node.project_id)
    if proj is None or not proj.pm_citizen_id:
        raise MarketError("Project has no manager AI (buyer) assigned; bidding unavailable")
    # 代码层防重复（部分唯一索引 uq_bid_proposal 兜底）
    dup = (db.query(Contract)
           .filter(Contract.node_id == node_id,
                   Contract.worker_id == worker.id,
                   Contract.status == "proposed")
           .first())
    if dup is not None:
        raise MarketError(f"Already bid on this node (offer #{dup.id}); do not bid again")
    terms = {"offer_cent": int(offer_cent), "message": message[:200],
             "spec": node.spec, "deliverable_std": node.deliverable_std}
    c = Contract(node_id=node.id, project_id=node.project_id,
                 worker_id=worker.id, buyer_id=proj.pm_citizen_id,
                 terms_json=json.dumps(terms, ensure_ascii=False),
                 task_lang=detect_lang(node.spec or ""),
                 status="proposed")
    db.add(c)
    db.flush()
    return c


def negotiate(db: Session, actor: AICitizen, node_id: int, offer_cent: int,
              message: str = "") -> Contract:
    """议价：对某节点的 active 要约调整报价。

    - worker 作为投标方：更新 terms_json.offer_cent（降价/加价）；
    - buyer（项目总管）：记录 terms_json.buyer_offer_cent（还价），供双方收敛。
    仅 proposed 态可议价；已签约/履约后不可改价。
    """
    if offer_cent <= 0:
        raise MarketError("Quote must be a positive integer in cents")
    node = _get_matching_node(db, node_id)
    q = (db.query(Contract)
         .filter(Contract.node_id == node_id, Contract.status == "proposed"))
    c = None
    if actor.id in [r.worker_id for r in q.all()]:
        c = q.filter(Contract.worker_id == actor.id).first()
    if c is None and actor.id:
        # actor 可能是买方总管：找该 node 任一 proposed 要约进行还价
        c = q.first()
    if c is None:
        raise MarketError("This node has no active offer; cannot negotiate (bid first)")
    if c.status != "proposed":
        raise MarketError("Offer already signed; cannot negotiate")
    terms = json.loads(c.terms_json or "{}")
    if actor.id == c.worker_id:
        terms["offer_cent"] = int(offer_cent)
        terms["worker_message"] = message[:200]
    elif actor.id == c.buyer_id:
        terms["buyer_offer_cent"] = int(offer_cent)
        terms["buyer_message"] = message[:200]
    else:
        raise MarketError("Not a party to this offer; no permission to negotiate")
    c.terms_json = json.dumps(terms, ensure_ascii=False)
    db.flush()
    return c
