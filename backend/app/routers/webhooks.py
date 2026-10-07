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
"""N9 AI 侧通知触达路由。

宿主侧（host JWT）：
  POST   /api/host/webhooks          订阅（secret 仅本次返回一次）
  GET    /api/host/webhooks          列表（secret 一律 masked，永不回显）
  DELETE /api/host/webhooks/{id}     退订

AI 侧（workflow key）：
  GET    /api/ai/notifications       读自己的通知（?unread=1 只看未读）
  POST   /api/ai/notifications/{id}/read   标已读

注：readonly(web 注册) AI 访问 /api/ai/* 已由 deps.get_current_ai 路径前缀集中拦截 403。
本文件顶部 import notify_service 以触发其模块级 register_handler（事件总线钩子注册）。
"""
import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_ai, get_current_host
from ..models import AICitizen, Host, Notification, WebhookSubscription
from ..notify_service import (ALL_EVENTS, gen_secret, validate_webhook_url)  # noqa: F401

router = APIRouter()


# ---------------- 宿主 webhook 订阅 ----------------
class WebhookIn(BaseModel):
    url: str
    secret: str | None = None       # 不传则自动生成；均仅创建时返回一次
    events: list[str] = []
    ai_id: int = 0                   # 0 = 该宿主全部 AI


def _mask(secret: str) -> str:
    if not secret:
        return ""
    return "********" + secret[-4:] if len(secret) > 4 else "********"


@router.post("/api/host/webhooks")
def create_webhook(body: WebhookIn,
                   host: Host = Depends(get_current_host),
                   db: Session = Depends(get_db)):
    bad = [e for e in body.events if e not in ALL_EVENTS]
    if bad:
        raise HTTPException(status_code=400, detail=f"Unknown event type: {bad}")
    if not body.events:
        raise HTTPException(status_code=400, detail="events cannot be empty")
    # SSRF：仅 http(s) 公网 URL，拒绝内网/私有 IP
    try:
        validate_webhook_url(body.url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # ai_id 归属校验（指定具体 AI 时必须属于本宿主）
    if body.ai_id:
        ai = db.get(AICitizen, body.ai_id)
        if ai is None or ai.host_id != host.id:
            raise HTTPException(status_code=403, detail="ai_id does not belong to this host")
    # 同 host+url 重复订阅 → 409（uq_webhook_url 唯一索引双保险）
    dup = (db.query(WebhookSubscription)
           .filter(WebhookSubscription.host_id == host.id,
                   WebhookSubscription.url == body.url).first())
    if dup:
        raise HTTPException(status_code=409, detail="Subscription for the same host+url already exists")
    secret = body.secret or gen_secret()
    sub = WebhookSubscription(host_id=host.id, ai_id=body.ai_id, url=body.url,
                              secret=secret,
                              events_json=json.dumps(body.events, ensure_ascii=False),
                              active=1)
    db.add(sub)
    db.commit()
    db.refresh(sub)
    # secret 仅此处明文返回一次；此后任何端点都不回显
    return {"id": sub.id, "url": sub.url, "events": body.events,
            "ai_id": sub.ai_id, "active": sub.active, "secret": secret}


@router.get("/api/host/webhooks")
def list_webhooks(host: Host = Depends(get_current_host),
                  db: Session = Depends(get_db)):
    rows = (db.query(WebhookSubscription)
            .filter(WebhookSubscription.host_id == host.id)
            .order_by(WebhookSubscription.id).all())
    return [{"id": r.id, "url": r.url,
             "events": json.loads(r.events_json or "[]"),
             "ai_id": r.ai_id, "active": r.active,
             "secret": _mask(r.secret)} for r in rows]


@router.delete("/api/host/webhooks/{sub_id}")
def delete_webhook(sub_id: int,
                   host: Host = Depends(get_current_host),
                   db: Session = Depends(get_db)):
    sub = db.get(WebhookSubscription, sub_id)
    if sub is None or sub.host_id != host.id:
        raise HTTPException(status_code=404, detail="Subscription not found")
    db.delete(sub)
    db.commit()
    return {"ok": True, "id": sub_id}


# ---------------- AI 侧通知读取（workflow key；readonly 被 /api/ai/* 前缀拦截 403） ----------------
def _ser_notif(r: Notification) -> dict:
    return {
        "id": r.id, "type": r.type, "title": r.title,
        "payload": json.loads(r.payload or "{}"),
        "channel": r.channel, "read": r.read,
        "created_at": r.created_at.isoformat() if isinstance(r.created_at, datetime) else None,
    }


@router.get("/api/ai/notifications")
def list_my_notifications(ai: AICitizen = Depends(get_current_ai),
                          unread: int = 0, limit: int = 50, offset: int = 0,
                          db: Session = Depends(get_db)):
    q = db.query(Notification).filter(Notification.ai_id == ai.id)
    if unread:
        q = q.filter(Notification.read == 0)
    total = q.count()
    rows = (q.order_by(Notification.id.desc())
            .offset(offset).limit(min(max(limit, 1), 200)).all())
    unread_cnt = (db.query(Notification)
                  .filter(Notification.ai_id == ai.id, Notification.read == 0).count())
    return {"total": total, "unread": unread_cnt,
            "items": [_ser_notif(r) for r in rows]}


@router.post("/api/ai/notifications/{nid}/read")
def read_notification(nid: int,
                      ai: AICitizen = Depends(get_current_ai),
                      db: Session = Depends(get_db)):
    r = db.get(Notification, nid)
    if r is None or r.ai_id != ai.id:       # 只能标自己的通知（他 AI/不存在 → 404）
        raise HTTPException(status_code=404, detail="Notification not found")
    r.read = 1
    db.commit()
    return {"ok": True, "id": r.id, "read": 1}
