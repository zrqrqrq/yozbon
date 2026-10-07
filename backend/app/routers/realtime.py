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
"""P1 实时推送与工作空间路由。"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..deps import host_or_any_ai
from ..realtime_push import instance as realtime_svc
from ..workspace_service import instance as ws_svc

router = APIRouter(prefix="/api/realtime", tags=["realtime"],
                   dependencies=[Depends(host_or_any_ai)])


# ======================== 请求体 ========================

class PublishBody(BaseModel):
    channel: str = Field(..., min_length=1)
    event_type: str = Field(..., min_length=1)
    payload: dict = {}
    priority: int = 5


class SubscribeBody(BaseModel):
    citizen_id: int = 0
    host_id: int = 0
    channel: str = Field(..., min_length=1)
    event_types: list[str] = ["*"]


class WorkspaceCreateBody(BaseModel):
    name: str = Field(..., min_length=1)
    owner_type: str = "host"
    owner_id: int
    project_id: int = 0


class AddMemberBody(BaseModel):
    member_type: str = "host"
    member_id: int
    role: str = "member"


# ======================== 事件推送 ========================

@router.post("/events/publish")
def publish_event(body: PublishBody):
    """发布实时事件到指定频道。"""
    return realtime_svc.publish(
        channel=body.channel, event_type=body.event_type,
        payload=body.payload, priority=body.priority,
    )


@router.get("/events/pending")
def pending_events(channel: str, since_id: int = 0,
                   citizen_id: int = 0, host_id: int = 0, limit: int = 50):
    """获取待推送事件列表。"""
    return realtime_svc.get_pending_events(
        channel=channel, since_id=since_id,
        citizen_id=citizen_id, host_id=host_id, limit=limit,
    )


@router.post("/subscribe")
def subscribe(body: SubscribeBody):
    """订阅频道事件。"""
    return realtime_svc.subscribe(
        citizen_id=body.citizen_id, host_id=body.host_id,
        channel=body.channel, event_types=body.event_types,
    )


@router.delete("/subscribe/{id}")
def unsubscribe(id: int):
    """取消订阅。"""
    return realtime_svc.unsubscribe(subscription_id=id)


# ======================== 工作空间 ========================

@router.post("/workspaces")
def create_workspace(body: WorkspaceCreateBody):
    """创建协作工作空间。"""
    return ws_svc.create(
        name=body.name, owner_type=body.owner_type,
        owner_id=body.owner_id, project_id=body.project_id,
    )


@router.get("/workspaces/{id}")
def get_workspace(id: int):
    """获取工作空间详情。"""
    return ws_svc.get_workspace(ws_id=id)


@router.post("/workspaces/{id}/members")
def add_member(id: int, body: AddMemberBody):
    """向工作空间添加成员。"""
    return ws_svc.add_member(
        ws_id=id, member_type=body.member_type,
        member_id=body.member_id, role=body.role,
    )


@router.delete("/workspaces/{id}/members/{mid}")
def remove_member(id: int, mid: int, member_type: str = "host"):
    """从工作空间移除成员。"""
    return ws_svc.remove_member(ws_id=id, member_type=member_type, member_id=mid)
