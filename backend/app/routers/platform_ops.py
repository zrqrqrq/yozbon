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
"""P2 平台工程路由：开发者门户/Feature Flags/Webhook/GDPR/多租户/Push/链路/备份/合规/审核/迁移。"""
from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..dev_portal import instance as dev_svc
from ..feature_flags import instance as ff_svc
from ..webhook_enhanced import instance as wh_svc
from ..gdpr_service import instance as gdpr_svc
from ..multi_tenant import instance as tenant_svc
from ..push_notify import instance as push_svc
from ..tracing import instance as trace_svc
from ..backup_service import instance as backup_svc
from ..compliance_service import instance as compliance_svc
from ..ml_moderation import instance as moderation_svc
from ..migration_service import instance as migration_svc

router = APIRouter(prefix="/api/platform", tags=["platform-ops"])


# ======================== 请求体 ========================

class DevAppBody(BaseModel):
    name: str = Field(..., min_length=1)
    description: str = ""
    host_id: int = 0


class FeatureFlagBody(BaseModel):
    flag_key: str = Field(..., min_length=1)
    description: str = ""
    enabled: bool = True
    rollout_pct: int = 100
    target_tiers: str = "*"


class WebhookBody(BaseModel):
    host_id: int
    url: str = Field(..., min_length=1)
    events: list[str] = Field(..., min_length=1)
    secret: str | None = None


class WebhookReplayBody(BaseModel):
    webhook_id: str = Field(..., min_length=1)
    from_event_id: int = 0
    to_event_id: int = 0


class GDPRBody(BaseModel):
    host_id: int
    scope: str = "all"
    data_categories: list[str] | None = None
    reason: str = ""


class TenantBody(BaseModel):
    code: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    plan: str = "free"
    max_hosts: int = 100
    max_ai_citizens: int = 500


class PushSubscribeBody(BaseModel):
    subscriber_type: str = "host"
    subscriber_id: int
    channel: str = "web-push"
    endpoint: str = Field(..., min_length=1)
    event_types: list[str] = Field(..., min_length=1)


class PushSendBody(BaseModel):
    subscriber_type: str = "host"
    subscriber_id: int
    event_type: str = Field(..., min_length=1)
    payload: dict = {}


class ComplianceBody(BaseModel):
    report_type: str = Field(..., min_length=1)


class ModerationBody(BaseModel):
    content_type: str = "text"
    content_id: int = 0
    text: str = Field(..., min_length=1)
    citizen_id: int = 0


# ======================== 开发者门户 ========================

@router.post("/dev-portal/apps")
def register_app(body: DevAppBody):
    """注册第三方开发者应用。"""
    return dev_svc.register_app(name=body.name, description=body.description, host_id=body.host_id)


@router.get("/dev-portal/apps/{id}/keys")
def get_app_keys(id: str):
    """获取应用 API Key 列表。"""
    return dev_svc.get_keys(app_id=id)


# ======================== Feature Flags ========================

@router.post("/feature-flags")
def create_flag(body: FeatureFlagBody):
    """创建特性开关。"""
    return ff_svc.create_flag(
        flag_key=body.flag_key, description=body.description,
        enabled=body.enabled, rollout_pct=body.rollout_pct, target_tiers=body.target_tiers,
    )


@router.get("/feature-flags/{key}/evaluate")
def evaluate_flag(key: str, user_id: int = 0, tier: str = ""):
    """评估特性开关是否对某用户生效。"""
    enabled = ff_svc.is_enabled(flag_key=key, user_id=user_id or None, tier=tier or None)
    return {"flag_key": key, "enabled": enabled}


# ======================== Webhook ========================

@router.post("/webhooks")
def register_webhook(body: WebhookBody):
    """注册 Webhook 订阅。"""
    return wh_svc.register_webhook(
        host_id=body.host_id, url=body.url, events=body.events, secret=body.secret,
    )


@router.post("/webhooks/replay")
def replay_webhook(body: WebhookReplayBody):
    """事件回放（按 ID 区间重新投递）。"""
    return wh_svc.replay_events(
        webhook_id=body.webhook_id, from_event_id=body.from_event_id, to_event_id=body.to_event_id,
    )


# ======================== GDPR ========================

@router.post("/gdpr/erasure")
def request_erasure(body: GDPRBody):
    """提交 GDPR 数据删除请求。"""
    return gdpr_svc.request_erasure(
        host_id=body.host_id, scope=body.scope,
        data_categories=body.data_categories, reason=body.reason,
    )


@router.get("/gdpr/requests/{id}")
def gdpr_status(id: int):
    """查询 GDPR 请求处理状态。"""
    return gdpr_svc.get_request_status(request_id=id)


# ======================== 多租户 ========================

@router.post("/tenants")
def create_tenant(body: TenantBody):
    """创建租户。"""
    return tenant_svc.create_tenant(
        code=body.code, name=body.name, plan=body.plan,
        max_hosts=body.max_hosts, max_ai_citizens=body.max_ai_citizens,
    )


# ======================== Push ========================

@router.post("/push/subscribe")
def push_subscribe(body: PushSubscribeBody):
    """创建推送订阅。"""
    return push_svc.subscribe(
        subscriber_type=body.subscriber_type, subscriber_id=body.subscriber_id,
        channel=body.channel, endpoint=body.endpoint, event_types=body.event_types,
    )


@router.post("/push/send")
def push_send(body: PushSendBody):
    """发送推送通知。"""
    return push_svc.send(
        subscriber_type=body.subscriber_type, subscriber_id=body.subscriber_id,
        event_type=body.event_type, payload=body.payload,
    )


# ======================== 链路追踪 ========================

@router.get("/traces/{trace_id}")
def get_trace(trace_id: str):
    """查询分布式链路追踪信息。"""
    return trace_svc.get_trace(trace_id=trace_id)


# ======================== 备份 ========================

@router.post("/backups")
def create_backup(backup_type: str = "manual"):
    """触发数据库备份。"""
    return backup_svc.create_backup(backup_type=backup_type, triggered_by="api")


# ======================== 合规审计 ========================

@router.post("/compliance/reports")
def generate_compliance_report(body: ComplianceBody):
    """生成合规审计报告。"""
    return compliance_svc.generate_report(report_type=body.report_type)


# ======================== 内容审核 ========================

@router.post("/moderation/score")
def moderation_score(body: ModerationBody):
    """ML 增强内容评分。"""
    return moderation_svc.score_content(
        content_type=body.content_type, content_id=body.content_id,
        text=body.text, citizen_id=body.citizen_id,
    )


# ======================== 迁移 ========================

@router.get("/migrations/status")
def migration_status():
    """获取数据库迁移状态。"""
    return migration_svc.get_current_version()
