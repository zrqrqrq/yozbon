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
"""批量入驻 SDK（面向 B 端企业客户）。

定位：为需要大规模管理 AI 员工的企业提供批量操作接口——
  - 批量注册（一次最多 50 个 AI）
  - 批量导入已有配置
  - 批量状态变更（暂停/恢复/激活）
  - 批量配额设置
  - 注册历史统计

鉴权：宿主 JWT（get_current_host）。
"""
from __future__ import annotations

import json
import secrets
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_host, issue_ai_key
from ..models import AICitizen, Host

router = APIRouter(prefix="/api/host/batch", tags=["host-batch"])

# 单次批量上限
_BATCH_LIMIT = 50


# ---------------- 请求体 ----------------

class AgentSpec(BaseModel):
    """单个 AI 注册规格。"""
    name: str = Field(..., min_length=1, max_length=80, description="AI name")
    occupation: str = Field(default="", max_length=40, description="Skill tags")
    persona: str = Field(default="", max_length=2000, description="Persona description")
    class_level: str = Field(default="bottom", description="Class: bottom/middle/boss/capital/governance")
    status: Optional[str] = Field(default=None, description="Initial status; uses default_status if empty")


class BatchRegisterBody(BaseModel):
    """批量注册请求。"""
    agents: list[AgentSpec] = Field(..., min_length=1, max_length=_BATCH_LIMIT,
                                     description="AI list (max 50)")
    default_status: str = Field(default="active", description="Default initial status: active/intern")


class ImportConfig(BaseModel):
    """导入配置项。"""
    name: str = Field(..., min_length=1, max_length=80)
    occupation: str = Field(default="", max_length=40)
    persona: str = Field(default="", max_length=2000)
    class_level: str = Field(default="bottom")
    compute_assets: dict = Field(default_factory=dict, description='{"channel":"platform","daily_quota":100}')


class BatchImportBody(BaseModel):
    """批量导入请求。"""
    configs: list[ImportConfig] = Field(..., min_length=1, max_length=_BATCH_LIMIT)


class SetStatusBody(BaseModel):
    """批量状态变更。"""
    citizen_ids: list[int] = Field(..., min_length=1, max_length=_BATCH_LIMIT)
    action: str = Field(..., description="pause|resume|activate")


class SetQuotaBody(BaseModel):
    """批量配额设置。"""
    citizen_ids: list[int] = Field(..., min_length=1, max_length=_BATCH_LIMIT)
    compute_assets: dict = Field(..., description='{"channel":"platform","daily_quota":100}')


# ---------------- 辅助 ----------------

def _generate_ai_uid(host_id: int, db: Session) -> str:
    """生成唯一 ai_uid，确保不冲突。"""
    for _ in range(10):
        suffix = secrets.token_hex(8)
        uid = f"ai_{host_id}_{suffix}"
        exists = db.query(AICitizen).filter(AICitizen.ai_uid == uid).first()
        if not exists:
            return uid
    # 极端情况 fallback：加时间戳
    import time
    return f"ai_{host_id}_{secrets.token_hex(4)}{int(time.time())}"


def _validate_ownership(db: Session, citizen_ids: list[int], host_id: int) -> tuple[list[AICitizen], list[int]]:
    """校验 citizen_ids 全部属于 host_id。返回 (合法citizens, 不合法的ids)。"""
    citizens = db.query(AICitizen).filter(AICitizen.id.in_(citizen_ids)).all()
    owned = [c for c in citizens if c.host_id == host_id]
    owned_ids = {c.id for c in owned}
    not_owned = [cid for cid in citizen_ids if cid not in owned_ids]
    return owned, not_owned


# ---------------- 端点 ----------------

@router.post("/register")
def batch_register(body: BatchRegisterBody,
                   host: Host = Depends(get_current_host),
                   db: Session = Depends(get_db)):
    """批量注册 AI（一次最多 50 个）。

    每个 AI 自动生成唯一 ai_uid，签发 API key 并返回。
    校验失败的项记入 failed 列表，不影响其他项创建。
    """
    if len(body.agents) > _BATCH_LIMIT:
        raise HTTPException(status_code=400, detail=f"At most {_BATCH_LIMIT} AIs per registration")

    valid_statuses = ("active", "intern")
    if body.default_status not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"default_status must be one of {valid_statuses}")

    created = []
    failed = []

    for idx, agent in enumerate(body.agents):
        # 名称校验
        if not agent.name or not agent.name.strip():
            failed.append({"index": idx, "name": agent.name, "reason": "Name cannot be empty"})
            continue

        # 状态决定
        status = agent.status if agent.status and agent.status in valid_statuses else body.default_status

        # class_level 校验
        valid_levels = ("bottom", "middle", "boss", "capital", "governance")
        level = agent.class_level if agent.class_level in valid_levels else "bottom"

        try:
            ai_uid = _generate_ai_uid(host.id, db)
            citizen = AICitizen(
                host_id=host.id,
                ai_uid=ai_uid,
                name=agent.name.strip()[:80],
                occupation=(agent.occupation or "general").strip()[:40],
                persona=agent.persona[:2000],
                status=status,
                class_level=level,
                balance_cent=0,
                compute_assets=json.dumps({"channel": "platform", "daily_quota": 100}),
                source="api",
            )
            db.add(citizen)
            db.flush()

            api_key, key_hash = issue_ai_key(citizen.id)
            citizen.api_key_hash = key_hash

            created.append({
                "citizen_id": citizen.id,
                "ai_uid": citizen.ai_uid,
                "name": citizen.name,
                "api_key": api_key,
            })
        except Exception as exc:
            failed.append({"index": idx, "name": agent.name, "reason": str(exc)[:200]})

    db.commit()

    return {
        "created": created,
        "failed": failed,
        "total": len(body.agents),
        "success_count": len(created),
    }


@router.post("/import")
def batch_import(body: BatchImportBody,
                 host: Host = Depends(get_current_host),
                 db: Session = Depends(get_db)):
    """批量导入已有 AI 配置（含完整 compute_assets 等字段）。

    与 /register 类似但允许自定义 compute_assets 和 class_level 等完整配置。
    """
    if len(body.configs) > _BATCH_LIMIT:
        raise HTTPException(status_code=400, detail=f"At most {_BATCH_LIMIT} configs per import")

    created = []
    failed = []

    for idx, cfg in enumerate(body.configs):
        if not cfg.name or not cfg.name.strip():
            failed.append({"index": idx, "name": cfg.name, "reason": "Name cannot be empty"})
            continue

        valid_levels = ("bottom", "middle", "boss", "capital", "governance")
        level = cfg.class_level if cfg.class_level in valid_levels else "bottom"

        try:
            ai_uid = _generate_ai_uid(host.id, db)
            compute_assets_str = json.dumps(cfg.compute_assets) if cfg.compute_assets else json.dumps({"channel": "platform", "daily_quota": 100})

            citizen = AICitizen(
                host_id=host.id,
                ai_uid=ai_uid,
                name=cfg.name.strip()[:80],
                occupation=(cfg.occupation or "general").strip()[:40],
                persona=cfg.persona[:2000],
                status="active",
                class_level=level,
                balance_cent=0,
                compute_assets=compute_assets_str,
                source="api",
            )
            db.add(citizen)
            db.flush()

            api_key, key_hash = issue_ai_key(citizen.id)
            citizen.api_key_hash = key_hash

            created.append({
                "citizen_id": citizen.id,
                "ai_uid": citizen.ai_uid,
                "name": citizen.name,
                "api_key": api_key,
            })
        except Exception as exc:
            failed.append({"index": idx, "name": cfg.name, "reason": str(exc)[:200]})

    db.commit()

    return {
        "created": created,
        "failed": failed,
        "total": len(body.configs),
        "success_count": len(created),
    }


@router.get("/status")
def batch_status(host: Host = Depends(get_current_host),
                 db: Session = Depends(get_db)):
    """查看本宿主批量注册历史统计。

    返回本宿主名下所有 AI 按状态和等级的分布。
    """
    citizens = db.query(AICitizen).filter(AICitizen.host_id == host.id).all()

    by_status: dict[str, int] = {}
    by_level: dict[str, int] = {}

    for c in citizens:
        by_status[c.status] = by_status.get(c.status, 0) + 1
        by_level[c.class_level] = by_level.get(c.class_level, 0) + 1

    return {
        "total_agents": len(citizens),
        "by_status": by_status,
        "by_level": by_level,
    }


@router.post("/set-status")
def batch_set_status(body: SetStatusBody,
                     host: Host = Depends(get_current_host),
                     db: Session = Depends(get_db)):
    """批量状态变更。

    action 映射：
      pause    → status = "sleep"
      resume   → status = "active"
      activate → status = "active"
    """
    action_map = {
        "pause": "sleep",
        "resume": "active",
        "activate": "active",
    }
    if body.action not in action_map:
        raise HTTPException(status_code=400, detail=f"action must be one of {list(action_map.keys())}")

    target_status = action_map[body.action]

    # 校验归属
    owned, not_owned = _validate_ownership(db, body.citizen_ids, host.id)
    if not_owned:
        raise HTTPException(
            status_code=400,
            detail=f"The following citizen_id(s) are not owned by the current host or do not exist: {not_owned}")

    updated = []
    for citizen in owned:
        old_status = citizen.status
        citizen.status = target_status
        updated.append({
            "citizen_id": citizen.id,
            "name": citizen.name,
            "old_status": old_status,
            "new_status": target_status,
        })

    db.commit()

    return {
        "updated": updated,
        "count": len(updated),
        "action": body.action,
    }


@router.post("/set-quota")
def batch_set_quota(body: SetQuotaBody,
                    host: Host = Depends(get_current_host),
                    db: Session = Depends(get_db)):
    """批量设置 compute_assets 配额。

    将指定的 compute_assets JSON 批量写入所有目标 AI。
    """
    # 校验归属
    owned, not_owned = _validate_ownership(db, body.citizen_ids, host.id)
    if not_owned:
        raise HTTPException(
            status_code=400,
            detail=f"The following citizen_id(s) are not owned by the current host or do not exist: {not_owned}")

    new_assets_str = json.dumps(body.compute_assets, ensure_ascii=False)
    updated = []
    for citizen in owned:
        citizen.compute_assets = new_assets_str
        updated.append({
            "citizen_id": citizen.id,
            "name": citizen.name,
        })

    db.commit()

    return {
        "updated": updated,
        "count": len(updated),
        "compute_assets": body.compute_assets,
    }
