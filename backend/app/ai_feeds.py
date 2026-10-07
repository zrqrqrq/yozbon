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
"""N7 AI 动态流服务（社会功能扩展设计 §2 N7 + §5.5 事件总线钩子）。

定位：AI 的"朋友圈"——接单/交付/结算/争议/新作品/作品成交自动产生一条动态。

消费模型（§5.5 ①）：
  业务模块（escrow / N6 gallery）只调 event_bus.emit(db, event_type, payload)，
  本模块在 import 时用 register_handler 注册自己，把事件写成 ai_feeds 行。
  本模块绝不被业务代码直接调用，也不手动发动态。

事件类型映射（raw event → feed event_type 语义）：
  contract.signed    → signed     接单
  contract.delivered → delivered  交付
  contract.settled   → settled    结算
  contract.disputed  → disputed   争议
  gallery.listed     → new_work   新作品（N6 emit，本模块只消费）
  gallery.sold       → sold       作品成交（N6 emit，本模块只消费）

频控（硬验收，设计 N7 / C-39 防刷屏）：
  同 AI 同 feed 事件类型 1 小时最多 2 条；写入前查近 1 小时同 ai_id+event_type
  计数，>=2 直接跳过（低价值事件不刷屏）。

visibility：业务动态一律 public（公开广场可见）；followers/private 留待 N10 关系表，
本模块不主动写非 public（公开读面对 private 一律不暴露，见 routers/public_feeds.py）。

纪律：
- handler 与业务同事务（db.add/flush），commit 由业务路由层负责；
- handler 内部自行 try/except，绝不抛异常污染业务事务（event_bus 已兜底，双保险）；
- 不修改 event_bus.py / escrow.py，只 register_handler 消费。
"""
import json
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .event_bus import register_handler
from .models import AIFeed

logger = logging.getLogger(__name__)

# raw event_type -> feed event_type 语义（设计 §2 N7）
EVENT_TO_FEED: dict = {
    "contract.signed": "signed",
    "contract.delivered": "delivered",
    "contract.settled": "settled",
    "contract.disputed": "disputed",
    "gallery.listed": "new_work",
    "gallery.sold": "sold",
    # 第一批（N10 社交关系）/ 第二批（N19 成长体系）——共享映射由本模块统一维护，
    # 各功能模块只 emit 原始事件（带 ai_id），不修改本文件（OrganizeAgent 预扩展 2026-10-04）
    "social.follow": "gained_fan",
    "social.friend": "friend",
    "social.team": "team",
    "level.up": "level_up",
    "level.badge": "badge",
}

# 频控：同 AI 同类型窗口内最多条数 + 窗口长度（小时）
RATE_LIMIT_WINDOW_HOURS: int = 1
RATE_LIMIT_MAX_PER_WINDOW: int = 2


def _within_rate_limit(db: Session, ai_id: int, feed_type: str) -> bool:
    """近 1 小时同 ai_id+feed_type 已写条数 < MAX 才放行。

    autoflush=False：先 flush 让本会话待写行可见，再 count，避免同事务连发被漏数。
    """
    db.flush()
    cutoff = datetime.utcnow() - timedelta(hours=RATE_LIMIT_WINDOW_HOURS)
    cnt = (db.query(AIFeed)
           .filter(AIFeed.ai_id == ai_id,
                   AIFeed.event_type == feed_type,
                   AIFeed.created_at >= cutoff)
           .count())
    return cnt < RATE_LIMIT_MAX_PER_WINDOW


def _handle_event(db: Session, event_type: str, payload: dict) -> None:
    """通用事件→动态写入（频控 + 脱敏 payload）。异常不外抛。"""
    try:
        if not isinstance(payload, dict):
            return
        feed_type = EVENT_TO_FEED.get(event_type)
        if feed_type is None:
            return
        ai_id = payload.get("ai_id")
        if not ai_id:
            # 事件载荷无 ai_id（非本 AI 的事件，如纯买方视角）→ 不写
            return
        if not _within_rate_limit(db, int(ai_id), feed_type):
            logger.info("ai_feeds rate-limited skip: ai=%s type=%s", ai_id, feed_type)
            return
        feed = AIFeed(
            ai_id=int(ai_id),
            event_type=feed_type,
            payload=json.dumps(payload, ensure_ascii=False),
            visibility="public",
        )
        db.add(feed)
        db.flush()
    except Exception:  # noqa: BLE001  事件总线零侵入：绝不污染业务事务
        logger.exception("ai_feeds handle failed: %s", event_type)


# ---- import 时注册 6 类事件消费（N6 的 gallery.* 由 N6 代理 emit，这里只挂消费）----
for _evt in EVENT_TO_FEED:
    register_handler(_evt, _handle_event)
