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
"""任务检查点管理服务。

保存和恢复长任务的阶段快照，支持断点续传，定期清理过期检查点。
"""
import json
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .database import SessionLocal
from .config import settings
from .models import TaskCheckpoint

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.utcnow()


class TaskCheckpointManager:
    """任务检查点管理器。"""

    def save(self, db: Session, task_id: int, ai_id: int,
             stage_index: int, state_snapshot: dict,
             tokens_used: int = 0) -> int:
        """保存断点，返回 checkpoint_id。"""
        expires_at = _now() + timedelta(hours=settings.CHECKPOINT_TTL_HOURS)
        cp = TaskCheckpoint(
            task_id=task_id,
            ai_id=ai_id,
            stage_index=stage_index,
            state_snapshot=json.dumps(state_snapshot),
            tokens_used=tokens_used,
            expires_at=expires_at,
        )
        db.add(cp)
        db.commit()
        return cp.id

    def restore(self, db: Session, task_id: int) -> dict:
        """恢复最近有效的 checkpoint。"""
        now = _now()
        cp = db.query(TaskCheckpoint).filter(
            TaskCheckpoint.task_id == task_id,
            (TaskCheckpoint.expires_at.is_(None)) |
            (TaskCheckpoint.expires_at > now),
        ).order_by(TaskCheckpoint.created_at.desc(), TaskCheckpoint.id.desc()).first()

        if cp is None:
            return {}

        return {
            "id": cp.id,
            "task_id": cp.task_id,
            "ai_id": cp.ai_id,
            "stage_index": cp.stage_index,
            "state_snapshot": json.loads(cp.state_snapshot) if cp.state_snapshot else {},
            "tokens_used": cp.tokens_used,
            "created_at": cp.created_at.isoformat() if cp.created_at else None,
        }

    def cleanup_expired(self, db: Session):
        """清理过期 checkpoint（注册为 daily job）。"""
        now = _now()
        deleted = db.query(TaskCheckpoint).filter(
            TaskCheckpoint.expires_at.isnot(None),
            TaskCheckpoint.expires_at <= now,
        ).delete()
        db.commit()
        logger.info("task_checkpoint cleanup: %d expired removed", deleted)
        return deleted

    def list_checkpoints(self, db: Session, task_id: int) -> list:
        """列出某任务的所有检查点。"""
        items = db.query(TaskCheckpoint).filter(
            TaskCheckpoint.task_id == task_id
        ).order_by(TaskCheckpoint.stage_index.asc()).all()
        return [
            {
                "id": cp.id,
                "stage_index": cp.stage_index,
                "tokens_used": cp.tokens_used,
                "created_at": cp.created_at.isoformat() if cp.created_at else None,
                "expires_at": cp.expires_at.isoformat() if cp.expires_at else None,
            }
            for cp in items
        ]


task_checkpoint = TaskCheckpointManager()
