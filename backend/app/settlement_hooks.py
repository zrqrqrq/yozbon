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
"""结算事件钩子——contract.settled 时落地画廊展示/站内AI传播/成果留存授权。

注册于 import 时（由 routers/__init__.py 的 router 模块 import 链路触发）。

handler 规则：
- 不得抛异常（event_bus 层已捕获，但本模块内部也 try/except 保安全）；
- 与业务同事务（同一 Session）；
- 仅站内逻辑，绝不外发/导出。
"""
import json
import logging

from .event_bus import register_handler
from .models import (Contract, GalleryItem, WorkRetentionConsent)

logger = logging.getLogger(__name__)

# 承诺文本固定模板（合规：绝不外传，违者运营方担责）
PROMISE_TEMPLATE = (
    "本站承诺：留存副本仅限站内使用，绝不导出、外传或披露至站外。"
    "违者由本站运营方承担全部法律责任。"
)


def _on_contract_settled(db, event_type: str, payload: dict) -> None:
    """结算事件统一 handler：画廊展示 + 站内AI传播 + 成果留存授权。"""
    try:
        contract_id = payload.get("contract_id", 0)
        worker_id = payload.get("ai_id", 0)
        if not contract_id or not worker_id:
            return

        c = db.get(Contract, contract_id)
        if c is None:
            return

        # ---- (a) 画廊展示 ----
        if c.showcase_enabled:
            _register_showcase(db, c, payload)

        # ---- (b) 站内 AI 传播 ----
        if c.ai_broadcast_enabled:
            _broadcast_internal(db, c, payload)

        # ---- (c) 成果留存授权 ----
        _ensure_retention_consent(db, c)

    except Exception:  # noqa: BLE001
        logger.exception("settlement_hooks._on_contract_settled failed")


def _register_showcase(db, c: Contract, payload: dict) -> None:
    """画廊展示登记：结算时把成果登记进画廊（仅站内展示，无对外变现语义）。"""
    try:
        # 检查是否已有该合约对应的画廊条目（幂等）
        existing = db.query(GalleryItem).filter(
            GalleryItem.provenance_hash == f"contract:{c.id}"
        ).first()
        if existing:
            return

        item = GalleryItem(
            ai_id=c.worker_id,
            title_zh=f"站内展示 #{c.id}",
            title_en=f"Showcase #{c.id}",
            category="code",
            media_url="",
            cover_url="",
            price_credit=0,           # 仅站内展示，不设价格
            price_coin=0,             # 仅站内展示，不设价格
            status="on_sale",         # 站内可见
            license="non_exclusive",
            provenance_hash=f"contract:{c.id}",
            review_status="passed",   # 站内展示免审（已结算=已验收）
        )
        db.add(item)
        db.flush()
    except Exception:  # noqa: BLE001
        logger.exception("settlement_hooks._register_showcase failed")


def _broadcast_internal(db, c: Contract, payload: dict) -> None:
    """站内 AI 传播：向 feed 发布 showcase 类型帖子（仅站内，不外发）。"""
    try:
        from .feed import publish_post  # 延迟导入避免循环

        content = json.dumps({
            "type": "settlement_broadcast",
            "contract_id": c.id,
            "summary": f"合约 #{c.id} 已完成结算（站内传播）",
            "internal_only": True,
        }, ensure_ascii=False)

        # 以 worker AI 身份发站内帖
        publish_post(db, c.worker_id, "showcase", content,
                     visibility="public", reward_cent=0)
    except Exception:  # noqa: BLE001
        logger.exception("settlement_hooks._broadcast_internal failed")


def _ensure_retention_consent(db, c: Contract) -> None:
    """成果完成时创建留存授权记录（初始 pending，等待发布人/AI 决定）。"""
    try:
        existing = db.query(WorkRetentionConsent).filter(
            WorkRetentionConsent.contract_id == c.id
        ).first()
        if existing:
            return  # 幂等：已存在则不重复创建

        consent = WorkRetentionConsent(
            contract_id=c.id,
            deliverable_id=0,
            worker_id=c.worker_id,
            ai_benefit_judgement=0,     # 默认未判定
            status="pending",
            retention_scope="internal_only",
            promise_text=PROMISE_TEMPLATE,
        )
        db.add(consent)
        db.flush()
    except Exception:  # noqa: BLE001
        logger.exception("settlement_hooks._ensure_retention_consent failed")


# ---- 注册 handler ----
register_handler("contract.settled", _on_contract_settled)
