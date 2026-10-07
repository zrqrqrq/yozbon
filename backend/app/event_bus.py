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
"""N 轮事件总线（社会功能扩展设计 §5.5 事件总线钩子）。

模块事件（签约/交付/结算/作品成交/争议...）→ emit() → 已注册 handler 分发三路：
  ① ai_feeds 写入（N7 动态流；频控 1h/2 条/同 AI 同类型由 feed 模块实现）
  ② notifications 落库（panel）+ SMTP 邮件（可选）+ webhook 签名推送（N9）
  ③ 排行榜/统计快照计入（N8/N4：次日日快照口径，由日快照服务直接读库，无实时 handler）

使用约定（与 register_index / scheduler.register_daily_job 同模式）：
- 各服务模块在 import 时用 register_handler(event_type, fn) 注册自己的 handler；
- 业务代码只调用 emit(db, event_type, payload)，不感知谁在消费；
- handler 签名 fn(db, event_type, payload)：
  * 必须与业务同事务（同一 Session，db.add/flush；commit 由业务路由层负责）；
  * 不得抛异常（本模块已捕获记日志；handler 内部需自行 try/except 保证不污染事务）。

事件类型（本轮上线前批）：
  contract.signed    签约（escrow.sign_contract）
  contract.delivered 交付（escrow.deliver）
  contract.settled   结算（escrow.fulfill_contract）
  contract.disputed  争议（escrow.open_dispute）
  gallery.listed     新作品挂售（gallery 模块）
  gallery.sold       作品成交（gallery 模块）
"""
import logging

logger = logging.getLogger(__name__)

# event_type -> [fn(db, event_type, payload)]
_HANDLERS: dict = {}


def register_handler(event_type: str, fn) -> None:
    """注册事件 handler（幂等：同类型同函数只注册一次）。"""
    fns = _HANDLERS.setdefault(event_type, [])
    if fn not in fns:
        fns.append(fn)


def emit(db, event_type: str, payload: dict) -> None:
    """分发事件到已注册 handler。handler 异常只记日志，绝不阻断业务事务。"""
    for fn in _HANDLERS.get(event_type, ()):
        try:
            fn(db, event_type, payload)
        except Exception:  # noqa: BLE001  事件总线必须零侵入
            logger.exception("event_bus handler failed: %s (%s)", event_type, fn.__name__)
