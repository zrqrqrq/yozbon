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
"""N11 worker_bridge 真算力服务（设计 §3 N11：宿主自备算力接入当劳动者）。

核心概念：宿主自备 GPU/RH 通道节点回连平台拉履约任务；签约事件分派到本 AI
宿主的可用节点；节点 3 分钟无心跳判离线 → 在途任务超时 → 连动信用违约；
节点全离线时任务不分派（留 executing 态），走平台 RH 通道 fallback。

幂等：
- 同一 contract 只分派一次 WorkerTask（contract.signed handler 查重）；
- 同一 WorkerTask delivered 回传重复到达不重复调 escrow.deliver（不重复结算）；
- 违约信用事件按 contract:id 只记一次（credit.has_event 查重）。

token 存证：节点长期 token = wn_<node_id>_<32hex>，库中只存 sha256 哈希
（与 AI key 同款，见 deps.issue_ai_key）；明文仅注册时返回一次。

本服务只 flush；commit 由路由层/调度器负责。
"""
import hashlib
import json
import logging
import secrets
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from . import credit, escrow, scheduler
from .event_bus import emit, register_handler
from .models import (AICitizen, Contract, CreditEvent, WorkerNode, WorkerTask)

logger = logging.getLogger(__name__)

VALID_NODE_TYPES = ("local_gpu", "cloud_gpu", "rh_api")
# 心跳超时（秒）：超过即判离线
HEARTBEAT_TIMEOUT_SECONDS = 180
# RH fallback 默认策略：节点不可用时不强制分派，合约保持 executing 由平台通道承接
RH_FALLBACK_DEFAULT = "platform_rh"


class WorkerError(Exception):
    """worker 业务异常；路由层映射 400/401/403/404/409。"""


# ---------------- token 存证（与 AI key 同款哈希） ----------------
def issue_node_token(node_id: int) -> tuple:
    """返回 (明文 token, sha256 哈希)。明文仅注册时返回一次。"""
    secret = secrets.token_hex(16)
    token = f"wn_{node_id}_{secret}"
    return token, hashlib.sha256(token.encode()).hexdigest()


def auth_node(authorization: str | None, db: Session) -> WorkerNode:
    """节点长期 token 认证：Bearer wn_<id>_<hex>，sha256 比对。失败 401。"""
    from fastapi import HTTPException
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing node token")
    token = authorization[7:]
    parts = token.split("_")
    if len(parts) != 3 or parts[0] != "wn":
        raise HTTPException(status_code=401, detail="bad node token format")
    try:
        node_id = int(parts[1])
    except ValueError:
        raise HTTPException(status_code=401, detail="bad node token format")
    node = db.get(WorkerNode, node_id)
    if node is None or not node.api_key_hash:
        raise HTTPException(status_code=401, detail="node not found")
    digest = hashlib.sha256(token.encode()).hexdigest()
    if not secrets.compare_digest(digest, node.api_key_hash):
        raise HTTPException(status_code=401, detail="invalid node token")
    return node


# ---------------- 注册 / 心跳 ----------------
def register_node(db: Session, host_id: int, name: str, node_type: str,
                   base_url: str, capabilities: list, max_concurrency: int
                   ) -> tuple:
    if node_type not in VALID_NODE_TYPES:
        raise WorkerError(f"node_type must be one of {VALID_NODE_TYPES}")
    max_concurrency = max(1, min(int(max_concurrency or 1), 64))
    dup = (db.query(WorkerNode)
           .filter(WorkerNode.host_id == host_id, WorkerNode.name == name).first())
    if dup is not None:
        raise WorkerError(f"Node name {name!r} already exists")
    node = WorkerNode(host_id=host_id, name=name[:120], node_type=node_type,
                      base_url=base_url[:500], max_concurrency=max_concurrency,
                      current_load=0, status="offline")
    db.add(node)
    db.flush()
    token, token_hash = issue_node_token(node.id)
    node.api_key_hash = token_hash
    node.capabilities = json.dumps(list(capabilities or []), ensure_ascii=False)
    db.flush()
    return node, token


def heartbeat(db: Session, node: WorkerNode, load: int) -> dict:
    node.heartbeat_at = datetime.utcnow()
    node.offline_since = None
    node.current_load = max(0, min(int(load or 0), node.max_concurrency))
    node.status = "busy" if node.current_load >= node.max_concurrency else "idle"
    db.flush()
    return {"node_id": node.id, "status": node.status,
            "current_load": node.current_load}


# ---------------- 拉任务（幂等） ----------------
def pull_task(db: Session, node: WorkerNode) -> dict | None:
    running = (db.query(WorkerTask)
               .filter(WorkerTask.node_id == node.id,
                       WorkerTask.status == "running").count())
    if running >= node.max_concurrency:
        return None
    # 优先 pending；无 pending 时把本机 running 任务再发一次（崩溃重拉不重复结算）
    task = (db.query(WorkerTask)
            .filter(WorkerTask.node_id == node.id,
                    WorkerTask.status == "pending")
            .order_by(WorkerTask.id.asc()).first())
    if task is None:
        task = (db.query(WorkerTask)
                .filter(WorkerTask.node_id == node.id,
                        WorkerTask.status == "running")
                .order_by(WorkerTask.id.asc()).first())
        if task is None:
            return None
        return _task_spec(task, recovered=True)
    task.status = "running"
    if task.started_at is None:
        task.started_at = datetime.utcnow()
    node.current_load = min(node.current_load + 1, node.max_concurrency)
    node.status = "busy" if node.current_load >= node.max_concurrency else "idle"
    db.flush()
    return _task_spec(task, recovered=False)


def _task_spec(task: WorkerTask, recovered: bool) -> dict:
    try:
        payload = json.loads(task.payload or "{}")
    except (ValueError, TypeError):
        payload = {}
    return {"worker_task_id": task.id, "contract_id": task.task_id,
            "status": task.status, "recovered": recovered, "payload": payload}


# ---------------- 回传结果（幂等 + 驱动履约结算） ----------------
def submit_result(db: Session, node: WorkerNode, worker_task_id: int,
                  result_ref: str, status: str) -> dict:
    task = (db.query(WorkerTask)
            .filter(WorkerTask.id == worker_task_id).first())
    if task is None:
        raise WorkerError("Task not found")
    if task.node_id != node.id:
        raise WorkerError("No permission to operate on another node's task")
    if task.status == "delivered":
        # 幂等：节点崩溃重传回传，不得重复结算
        return {"ok": True, "duplicate": True, "worker_task_id": task.id}
    if status not in ("delivered", "failed"):
        raise WorkerError("status must be delivered/failed")

    task.result_ref = result_ref[:500]
    task.finished_at = datetime.utcnow()
    node.current_load = max(0, node.current_load - 1)

    contract = db.get(Contract, task.task_id)
    if status == "delivered":
        task.status = "delivered"
        # 驱动履约结算：把交付物推进该合约交付路径（escrow.deliver 调用方，非修改方）
        if contract is not None and contract.status == "executing":
            worker = db.get(AICitizen, contract.worker_id)
            if worker is not None:
                fp = hashlib.sha256(
                    f"{contract.id}:{result_ref}".encode()).hexdigest()
                escrow.deliver(db, worker, contract.id,
                               file_ref=result_ref, fingerprint=fp)
    else:
        task.status = "failed"
        # 连动信用：履约失败记违约（按合约只罚一次）
        if contract is not None:
            ev_ref = f"contract:{contract.id}"
            if not credit.has_event(db, contract.worker_id, "breach", ref=ev_ref):
                credit.record_event(db, contract.worker_id, "breach",
                                    reason=f"Node callback failed task={task.id}",
                                    ref=ev_ref)
    db.flush()
    return {"ok": True, "duplicate": False, "worker_task_id": task.id,
            "status": task.status}


# ---------------- 分派钩子：contract.signed → 建 WorkerTask(pending) ----------------
def dispatch_on_signed(db: Session, event_type: str, payload: dict) -> None:
    """签约事件：优先分派到该 AI 宿主自备可用节点；无可用节点=平台 RH fallback。

    只读 payload + 查重；handler 异常由 event_bus 兜底，此处再 try 一层。
    """
    try:
        ai_id = payload.get("ai_id")
        contract_id = payload.get("contract_id")
        if not ai_id or not contract_id:
            return
        # 幂等：同合约已分派过
        if db.query(WorkerTask).filter(
                WorkerTask.task_id == int(contract_id)).first() is not None:
            return
        worker = db.get(AICitizen, int(ai_id))
        if worker is None:
            return
        nodes = (db.query(WorkerNode)
                 .filter(WorkerNode.host_id == worker.host_id).all())
        # 选可用（非离线且有余力）中负载最低的节点
        candidates = [n for n in nodes
                      if n.status != "offline"
                      and (n.current_load or 0) < n.max_concurrency]
        if not candidates:
            # RH fallback：无可用自备节点 → 不分派，合约保持 executing 由平台通道承接
            logger.info("worker fallback to platform RH: contract=%s host=%s",
                        contract_id, worker.host_id)
            return
        node = min(candidates, key=lambda n: (n.current_load or 0, n.id))
        db.add(WorkerTask(node_id=node.id, task_id=int(contract_id),
                          payload=json.dumps(payload, ensure_ascii=False),
                          status="pending"))
        db.flush()
    except Exception:  # noqa: BLE001 事件总线零侵入
        logger.exception("dispatch_on_signed failed: %s", payload)


register_handler("contract.signed", dispatch_on_signed)


# ---------------- 离线巡检（日级注册；心跳超时→offline→判违约） ----------------
def sweep_offline(db: Session, now: datetime | None = None) -> dict:
    """日级巡检：心跳超时节点置 offline；其在途任务置 timeout 并连动信用违约。"""
    now = now or datetime.utcnow()
    cutoff = now - timedelta(seconds=HEARTBEAT_TIMEOUT_SECONDS)
    nodes = db.query(WorkerNode).all()
    offline_cnt = 0
    timeout_tasks = 0
    for n in nodes:
        if n.status == "offline":
            continue
        if n.heartbeat_at is None or n.heartbeat_at < cutoff:
            n.status = "offline"
            n.offline_since = n.heartbeat_at or now
            n.current_load = 0
            offline_cnt += 1
            for t in (db.query(WorkerTask)
                      .filter(WorkerTask.node_id == n.id,
                              WorkerTask.status.in_(("pending", "running")))
                      .all()):
                t.status = "timeout"
                t.finished_at = now
                timeout_tasks += 1
                contract = db.get(Contract, t.task_id)
                if contract is not None:
                    ev_ref = f"contract:{contract.id}"
                    if not credit.has_event(db, contract.worker_id, "breach",
                                           ref=ev_ref):
                        credit.record_event(
                            db, contract.worker_id, "breach",
                            reason=f"Node {n.id} offline timeout; task {t.id} not delivered",
                            ref=ev_ref)
    db.flush()
    # 日任务契约（scheduler.register_daily_job）：返回 int（task_id 或 0）；
    # 这里无 governance_task，返回处理的任务计数（非 0 表示本轮有动作）。
    return offline_cnt + timeout_tasks


scheduler.register_daily_job("worker_offline_sweep", sweep_offline)
