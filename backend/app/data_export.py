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
"""GDPR 风格数据可携带性导出服务。

请求 → 异步生成 → 文件就绪 → 过期清理。
生产环境由后台队列处理 generating 阶段；本模块提供状态机流转接口。
"""
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .models import DataExportRequest
from .scheduler import register_daily_job

EXPORT_TTL_DAYS = 7


def _now() -> datetime:
    return datetime.utcnow()


class DataExportError(Exception):
    """数据导出业务异常。路由层映射为 HTTP 400。"""


# ---------------------------------------------------------------------------
# 请求导出
# ---------------------------------------------------------------------------

def request_export(
    db: Session,
    requester_id: int,
    requester_type: str = "ai",
    scope: str = "all",
) -> DataExportRequest:
    """创建导出请求（状态=pending）。"""
    if requester_type not in ("ai", "host"):
        raise DataExportError(f"Unsupported requester type: {requester_type!r}")

    req = DataExportRequest(
        requester_id=requester_id,
        requester_type=requester_type,
        scope=scope,
        status="pending",
    )
    db.add(req)
    db.flush()
    return req


# ---------------------------------------------------------------------------
# 生成导出文件（桩实现，由队列消费者调用）
# ---------------------------------------------------------------------------

def generate_export(db: Session, request_id: int) -> DataExportRequest:
    """模拟导出文件生成。

    生产环境中，后台队列领取 pending 请求后调用本函数：
    1. 标记 status=generating
    2. 执行实际文件生成逻辑（此处为桩）
    3. 设置 file_ref、status=ready、expires_at=now+7days
    """
    req = db.get(DataExportRequest, request_id)
    if req is None:
        raise DataExportError(f"Export request {request_id} not found")
    if req.status not in ("pending", "generating"):
        raise DataExportError(f"Request status is {req.status!r}; cannot generate")

    req.status = "generating"
    db.flush()

    # 桩：直接生成文件引用
    req.file_ref = f"export_{request_id}.json"
    req.status = "ready"
    req.expires_at = _now() + timedelta(days=EXPORT_TTL_DAYS)
    db.flush()
    return req


# ---------------------------------------------------------------------------
# 状态查询
# ---------------------------------------------------------------------------

def get_export_status(db: Session, request_id: int) -> dict:
    """获取单个导出请求状态详情。"""
    req = db.get(DataExportRequest, request_id)
    if req is None:
        raise DataExportError(f"Export request {request_id} not found")

    return {
        "id": req.id,
        "requester_id": req.requester_id,
        "requester_type": req.requester_type,
        "scope": req.scope,
        "status": req.status,
        "file_ref": req.file_ref,
        "expires_at": req.expires_at.isoformat() if req.expires_at else None,
        "created_at": req.created_at.isoformat() if req.created_at else None,
    }


def list_my_exports(db: Session, requester_id: int) -> list[dict]:
    """我的导出请求列表。"""
    rows = (
        db.query(DataExportRequest)
        .filter(DataExportRequest.requester_id == requester_id)
        .order_by(DataExportRequest.id.desc())
        .all()
    )
    return [
        {
            "id": r.id,
            "scope": r.scope,
            "status": r.status,
            "file_ref": r.file_ref,
            "expires_at": r.expires_at.isoformat() if r.expires_at else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# 日级任务：过期清理
# ---------------------------------------------------------------------------

def data_export_daily_job(db: Session, now: datetime) -> int:
    """将已就绪但超过 expires_at 的导出请求标记为 expired。"""
    expired = (
        db.query(DataExportRequest)
        .filter(
            DataExportRequest.status == "ready",
            DataExportRequest.expires_at < now,
        )
        .all()
    )
    for req in expired:
        req.status = "expired"
    db.flush()
    return 0


register_daily_job("data_export", data_export_daily_job)
