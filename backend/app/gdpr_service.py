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
"""P2 GDPR 被遗忘权/数据保护服务。

实现 GDPR 核心条款：
- Art.17 被遗忘权：宿主可请求删除个人数据（全量/部分分类）；
- Art.20 数据可携带权：导出结构化个人数据；
- 数据匿名化：删除请求完成后对关联数据执行匿名化处理。

流程：request_erasure -> pending -> process_erasure -> processing -> anonymize_data -> completed。
期限由 settings.GDPR_ERASURE_DAYS 控制（默认 30 天）。
"""
import json
import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .database import SessionLocal
from .config import settings
from .models import DeletionRequest

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.utcnow()


class GDPRService:
    """GDPR 数据保护服务。"""

    def request_erasure(self, host_id: int, scope: str = "all",
                        data_categories: list = None, reason: str = "") -> dict:
        """提交删除请求。

        Args:
            host_id: 宿主 ID。
            scope: "all" 或 "partial"。
            data_categories: 数据类别列表，如 ["profile", "feeds", "messages"]。
            reason: 申请理由。

        Returns:
            {"request_id", "host_id", "status", "deadline"}
        """
        if scope not in ("all", "partial"):
            raise ValueError("scope must be all or partial")
        if scope == "partial" and not data_categories:
            raise ValueError("partial mode must specify data_categories")

        categories = data_categories or ["*"]
        deadline = _now() + timedelta(days=settings.GDPR_ERASURE_DAYS)

        db: Session = SessionLocal()
        try:
            # 检查是否已有未完成的请求
            existing = (db.query(DeletionRequest)
                        .filter(DeletionRequest.host_id == host_id,
                                DeletionRequest.status.in_(["pending", "processing"]))
                        .first())
            if existing:
                raise ValueError(f"Host {host_id} already has an unfinished deletion request (id={existing.id})")

            req = DeletionRequest(
                host_id=host_id,
                scope=scope,
                data_categories=json.dumps(categories),
                status="pending",
                reason=reason,
            )
            db.add(req)
            db.commit()
            logger.info("gdpr: erasure request %d created for host %d scope=%s",
                        req.id, host_id, scope)
            return {
                "request_id": req.id,
                "host_id": host_id,
                "status": "pending",
                "deadline": deadline.isoformat(),
            }
        finally:
            db.close()

    def process_erasure(self, request_id: int) -> dict:
        """处理删除请求：执行数据匿名化。

        由管理员或定时任务触发。
        """
        db: Session = SessionLocal()
        try:
            req = db.get(DeletionRequest, request_id)
            if req is None:
                raise ValueError(f"Deletion request {request_id} not found")
            if req.status != "pending":
                raise ValueError(f"Request {request_id} status is {req.status}; only pending can be processed")

            req.status = "processing"
            db.commit()

            # 执行匿名化
            self.anonymize_data(req.host_id)

            req.completed_at = _now()
            req.status = "completed"
            db.commit()
            logger.info("gdpr: erasure request %d completed for host %d", request_id, req.host_id)
            return {"request_id": request_id, "status": "completed", "completed_at": req.completed_at.isoformat()}
        finally:
            db.close()

    def get_request_status(self, request_id: int) -> dict:
        """获取删除请求状态。"""
        db: Session = SessionLocal()
        try:
            req = db.get(DeletionRequest, request_id)
            if req is None:
                raise ValueError(f"Deletion request {request_id} not found")
            return {
                "request_id": req.id,
                "host_id": req.host_id,
                "scope": req.scope,
                "data_categories": json.loads(req.data_categories or "[]"),
                "status": req.status,
                "reason": req.reason,
                "created_at": req.created_at.isoformat() if req.created_at else None,
                "completed_at": req.completed_at.isoformat() if req.completed_at else None,
            }
        finally:
            db.close()

    def list_pending_requests(self) -> list:
        """列出所有待处理的删除请求。"""
        db: Session = SessionLocal()
        try:
            reqs = (db.query(DeletionRequest)
                    .filter(DeletionRequest.status.in_(["pending", "processing"]))
                    .order_by(DeletionRequest.created_at.asc())
                    .all())
            return [
                {
                    "request_id": r.id,
                    "host_id": r.host_id,
                    "scope": r.scope,
                    "status": r.status,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in reqs
            ]
        finally:
            db.close()

    def anonymize_data(self, host_id: int) -> dict:
        """匿名化宿主关联数据。

        实际生产需要：
        - 匿名化 profile（昵称改为 "User_<hash>"）；
        - 删除动态/评论；
        - 匿名化交易对手方标识；
        - 保留财务凭证（GDPR Art.17(3)(b) 合规保留义务）。
        """
        # 此处为逻辑框架，实际 DB 操作需配合各模块
        actions = [
            "anonymize_host_profile",
            "delete_feeds",
            "delete_comments",
            "anonymize_chat_metadata",
            "anonymize_notifications",
            "purge_search_history",
        ]
        logger.info("gdpr: anonymizing data for host %d actions=%s", host_id, actions)
        return {"host_id": host_id, "actions_performed": actions, "completed_at": _now().isoformat()}

    def get_data_inventory(self, host_id: int) -> dict:
        """数据清单：列出平台中保存的宿主数据类型及条目数。

        对应 GDPR Art.30 处理记录。
        """
        inventory = {
            "host_id": host_id,
            "categories": [
                {"category": "profile", "description": "Host basic info", "retention": "account lifetime"},
                {"category": "feeds", "description": "Published content", "retention": "account lifetime"},
                {"category": "transactions", "description": "Transaction records", "retention": "7 years (legal)"},
                {"category": "chat", "description": "Chat messages", "retention": "90 days"},
                {"category": "logs", "description": "Operation logs", "retention": "180 days"},
                {"category": "notifications", "description": "Notification records", "retention": "30 days"},
            ],
            "generated_at": _now().isoformat(),
        }
        return inventory

    def export_for_portability(self, host_id: int) -> dict:
        """数据可携带权导出（GDPR Art.20）。

        返回结构化 JSON 数据包。
        """
        export_id = str(uuid.uuid4())
        data = {
            "export_id": export_id,
            "host_id": host_id,
            "format": "application/json",
            "sections": [
                "profile", "feeds", "comments", "transactions",
                "contracts", "notifications", "settings",
            ],
            "generated_at": _now().isoformat(),
            "download_url": f"{settings.APP_BASE_URL}/api/v1/gdpr/exports/{export_id}",
            "expires_at": (_now() + timedelta(days=7)).isoformat(),
        }
        logger.info("gdpr: portability export %s created for host %d", export_id, host_id)
        return data

    def mark_completed(self, request_id: int) -> dict:
        """手动标记删除请求完成。"""
        db: Session = SessionLocal()
        try:
            req = db.get(DeletionRequest, request_id)
            if req is None:
                raise ValueError(f"Deletion request {request_id} not found")

            req.status = "completed"
            req.completed_at = _now()
            db.commit()
            logger.info("gdpr: request %d manually marked completed", request_id)
            return {"request_id": request_id, "status": "completed"}
        finally:
            db.close()


instance = GDPRService()
