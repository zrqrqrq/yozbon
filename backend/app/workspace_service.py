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
"""协作工作空间服务。

功能：
- 工作空间 CRUD；
- 成员管理（添加/移除/角色变更）；
- 配置更新（JSON 格式存储）；
- 按 owner 查询。

依赖模型：Workspace, WorkspaceMember。
"""
import json
import logging
from datetime import datetime

from .database import SessionLocal
from .models import Workspace, WorkspaceMember

logger = logging.getLogger(__name__)


def _now():
    return datetime.utcnow()


class WorkspaceService:
    """协作工作空间业务逻辑。"""

    def create(self, name: str, owner_type: str, owner_id: int,
               project_id: int = 0) -> dict:
        """创建工作空间，owner 自动成为 owner 角色成员。"""
        db = SessionLocal()
        try:
            ws = Workspace(
                name=name,
                owner_type=owner_type,
                owner_id=owner_id,
                project_id=project_id,
                config="{}",
                status="active",
            )
            db.add(ws)
            db.flush()

            # owner 自动加入
            member = WorkspaceMember(
                workspace_id=ws.id,
                member_type=owner_type,
                member_id=owner_id,
                role="owner",
            )
            db.add(member)
            db.commit()

            logger.info("Workspace created: id=%d name=%s owner=%s:%d",
                        ws.id, name, owner_type, owner_id)
            return {"workspace_id": ws.id, "name": name}
        finally:
            db.close()

    def add_member(self, ws_id: int, member_type: str, member_id: int,
                   role: str = "member") -> dict:
        """添加成员。"""
        db = SessionLocal()
        try:
            ws = db.get(Workspace, ws_id)
            if ws is None or ws.status != "active":
                return {"error": "workspace not found or archived"}

            existing = (db.query(WorkspaceMember)
                        .filter(WorkspaceMember.workspace_id == ws_id,
                                WorkspaceMember.member_type == member_type,
                                WorkspaceMember.member_id == member_id)
                        .first())
            if existing:
                # 更新角色
                existing.role = role
                db.commit()
                return {"ok": True, "updated": True, "member_id": existing.id}

            member = WorkspaceMember(
                workspace_id=ws_id,
                member_type=member_type,
                member_id=member_id,
                role=role,
            )
            db.add(member)
            db.commit()
            logger.info("Member added to workspace %d: %s:%d role=%s",
                        ws_id, member_type, member_id, role)
            return {"ok": True, "member_id": member.id}
        finally:
            db.close()

    def remove_member(self, ws_id: int, member_type: str, member_id: int) -> dict:
        """移除成员（owner 不可移除）。"""
        db = SessionLocal()
        try:
            member = (db.query(WorkspaceMember)
                      .filter(WorkspaceMember.workspace_id == ws_id,
                              WorkspaceMember.member_type == member_type,
                              WorkspaceMember.member_id == member_id)
                      .first())
            if member is None:
                return {"error": "member not found"}
            if member.role == "owner":
                return {"error": "cannot remove owner"}

            db.delete(member)
            db.commit()
            logger.info("Member removed from workspace %d: %s:%d",
                        ws_id, member_type, member_id)
            return {"ok": True}
        finally:
            db.close()

    def update_config(self, ws_id: int, config: dict) -> dict:
        """更新工作空间配置（合并写入）。"""
        db = SessionLocal()
        try:
            ws = db.get(Workspace, ws_id)
            if ws is None:
                return {"error": "workspace not found"}

            current = json.loads(ws.config or "{}")
            current.update(config)
            ws.config = json.dumps(current, ensure_ascii=False)
            db.commit()
            return {"ok": True}
        finally:
            db.close()

    def archive(self, ws_id: int) -> dict:
        """归档工作空间。"""
        db = SessionLocal()
        try:
            ws = db.get(Workspace, ws_id)
            if ws is None:
                return {"error": "workspace not found"}
            ws.status = "archived"
            db.commit()
            logger.info("Workspace archived: id=%d", ws_id)
            return {"ok": True}
        finally:
            db.close()

    def get_workspace(self, ws_id: int) -> dict:
        """获取工作空间详情（含成员列表）。"""
        db = SessionLocal()
        try:
            ws = db.get(Workspace, ws_id)
            if ws is None:
                return {"error": "workspace not found"}

            members = (db.query(WorkspaceMember)
                       .filter(WorkspaceMember.workspace_id == ws_id)
                       .all())
            return {
                "id": ws.id,
                "name": ws.name,
                "owner_type": ws.owner_type,
                "owner_id": ws.owner_id,
                "project_id": ws.project_id,
                "config": json.loads(ws.config or "{}"),
                "status": ws.status,
                "members": [
                    {"member_type": m.member_type, "member_id": m.member_id,
                     "role": m.role, "joined_at": m.joined_at.isoformat() if m.joined_at else None}
                    for m in members
                ],
                "created_at": ws.created_at.isoformat() if ws.created_at else None,
            }
        finally:
            db.close()

    def list_by_owner(self, owner_type: str, owner_id: int,
                      include_archived: bool = False) -> list[dict]:
        """列出指定 owner 的所有工作空间。"""
        db = SessionLocal()
        try:
            q = (db.query(Workspace)
                 .filter(Workspace.owner_type == owner_type,
                         Workspace.owner_id == owner_id))
            if not include_archived:
                q = q.filter(Workspace.status == "active")
            rows = q.order_by(Workspace.id.desc()).all()
            return [
                {
                    "id": w.id, "name": w.name, "status": w.status,
                    "project_id": w.project_id,
                    "created_at": w.created_at.isoformat() if w.created_at else None,
                }
                for w in rows
            ]
        finally:
            db.close()


instance = WorkspaceService()
