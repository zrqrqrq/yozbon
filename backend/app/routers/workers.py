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
"""N11 worker_bridge 路由（设计 §3 N11 真算力接入）。

端点：
- POST /api/workers/register         宿主 JWT 注册节点（node_token 明文仅返回一次）
- POST /api/workers/heartbeat        节点长期 token 心跳
- GET  /api/workers/pull             节点拉取待执行任务（pending/running 幂等）
- POST /api/workers/{task_id}/result 节点回传结果（delivered→驱动履约结算）
- GET  /api/host/workers             宿主管理：节点列表
- POST /api/host/workers             宿主管理：新增节点配置（同 register）
"""
import json
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import worker_service
from ..database import get_db
from ..deps import get_current_host
from ..models import Host, WorkerNode

router = APIRouter()


def _map(exc: worker_service.WorkerError) -> HTTPException:
    msg = str(exc)
    if "not found" in msg or "No permission" in msg:
        return HTTPException(status_code=403 if "No permission" in msg else 404, detail=msg)
    return HTTPException(status_code=400, detail=msg)


class RegisterBody(BaseModel):
    name: str
    node_type: str = "local_gpu"
    base_url: str = ""
    capabilities: list = []
    max_concurrency: int = 1


class HeartbeatBody(BaseModel):
    load: int = 0


class ResultBody(BaseModel):
    result_ref: str = ""
    status: str = "delivered"


def _node(authorization: Optional[str] = Header(None),
          db: Session = Depends(get_db)) -> WorkerNode:
    return worker_service.auth_node(authorization, db)


def _register(db: Session, host: Host, body: RegisterBody) -> dict:
    try:
        node, token = worker_service.register_node(
            db, host.id, body.name, body.node_type, body.base_url,
            body.capabilities, body.max_concurrency)
    except worker_service.WorkerError as e:
        raise _map(e)
    db.commit()
    return {"node_id": node.id, "name": node.name, "node_type": node.node_type,
            "max_concurrency": node.max_concurrency,
            "node_token": token,
            "note": "node_token is returned only once; please keep it safe; only the hash is stored"}


@router.post("/api/workers/register")
def register(body: RegisterBody,
             host: Host = Depends(get_current_host),
             db: Session = Depends(get_db)):
    return _register(db, host, body)


@router.post("/api/host/workers")
def host_register(body: RegisterBody,
                  host: Host = Depends(get_current_host),
                  db: Session = Depends(get_db)):
    return _register(db, host, body)


@router.get("/api/host/workers")
def host_list(host: Host = Depends(get_current_host),
              db: Session = Depends(get_db)):
    rows = (db.query(WorkerNode)
            .filter(WorkerNode.host_id == host.id)
            .order_by(WorkerNode.id.asc()).all())
    return {"items": [{
        "node_id": n.id, "name": n.name, "node_type": n.node_type,
        "base_url": n.base_url, "status": n.status,
        "max_concurrency": n.max_concurrency, "current_load": n.current_load,
        "capabilities": json.loads(n.capabilities or "[]"),
        "heartbeat_at": n.heartbeat_at.isoformat() if n.heartbeat_at else None,
    } for n in rows]}


@router.post("/api/workers/heartbeat")
def heartbeat(body: HeartbeatBody,
              node: WorkerNode = Depends(_node),
              db: Session = Depends(get_db)):
    out = worker_service.heartbeat(db, node, body.load)
    db.commit()
    return out


@router.get("/api/workers/pull")
def pull(node: WorkerNode = Depends(_node),
         db: Session = Depends(get_db)):
    out = worker_service.pull_task(db, node)
    db.commit()
    return out or {}


@router.post("/api/workers/{worker_task_id}/result")
def submit_result(worker_task_id: int, body: ResultBody,
                  node: WorkerNode = Depends(_node),
                  db: Session = Depends(get_db)):
    try:
        out = worker_service.submit_result(db, node, worker_task_id,
                                           body.result_ref, body.status)
    except worker_service.WorkerError as e:
        raise _map(e)
    db.commit()
    return out
