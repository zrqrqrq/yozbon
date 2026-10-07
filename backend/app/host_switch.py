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
"""宿主「紧急暂停城主自治循环」开关（S13）。

复用 SystemState（key=String PK / value_cent=Integer）作为开关存储位，
key 固定为 "governor_paused"：value_cent==1 视为已暂停，0（或缺行）视为未暂停。

读写范式参考 monetary_policy._get_state_value/_set_state_value 对 SystemState 的操作。
is_governor_paused 是只读且**绝不抛异常**：任何异常一律降级为 False（不暂停），
保证城主循环不会因为开关读取失败而被误停。
"""
import logging

from sqlalchemy.orm import Session

from .models import SystemState

logger = logging.getLogger(__name__)

# SystemState 中开关 key
_GOVERNOR_PAUSED_KEY = "governor_paused"


def is_governor_paused(db: Session) -> bool:
    """读取 SystemState key="governor_paused"：value_cent==1 视为暂停。

    - 缺行（从未写过）返回 False；
    - 任何异常返回 False（不得抛，避免误停城主循环）。
    """
    try:
        row = db.get(SystemState, _GOVERNOR_PAUSED_KEY)
        return bool(row is not None and row.value_cent == 1)
    except Exception:   # 降级为未暂停，绝不抛出
        logger.exception("读取 governor_paused 失败，降级为未暂停")
        return False


def set_governor_paused(db: Session, paused: bool) -> None:
    """写 SystemState key="governor_paused"（1=暂停 / 0=恢复）并 commit。"""
    new_value = 1 if paused else 0
    row = db.get(SystemState, _GOVERNOR_PAUSED_KEY)
    if row is None:
        row = SystemState(key=_GOVERNOR_PAUSED_KEY, value_cent=new_value)
        db.add(row)
    else:
        row.value_cent = new_value
    from datetime import datetime
    row.updated_at = datetime.utcnow()
    db.commit()
