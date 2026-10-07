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
"""优雅降级服务。

管理系统的降级模式：激活/停用降级、自动检测健康指标决定是否需要降级、
为中间件提供请求级别的降级判断。
"""
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from .database import SessionLocal
from .config import settings
from .models import DegradationModeState

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.utcnow()


class GracefulDegradationService:
    """优雅降级服务。"""

    def activate(self, db: Session, mode: str, trigger_reason: str,
                 triggered_by: str = "auto"):
        """激活降级模式。"""
        state = DegradationModeState(
            mode=mode,
            trigger_reason=trigger_reason,
            triggered_by=triggered_by,
            active=1,
        )
        db.add(state)
        db.commit()
        logger.warning("degradation activated: mode=%s reason=%s", mode, trigger_reason)
        return state.id

    def deactivate(self, db: Session, mode_state_id: int):
        """停用降级模式。"""
        state = db.get(DegradationModeState, mode_state_id)
        if state is None:
            raise ValueError(f"Degradation state {mode_state_id} not found")
        state.active = 0
        state.deactivated_at = _now()
        db.commit()

    def current_mode(self, db: Session) -> str:
        """返回当前活动降级模式（无降级返回 'normal'）。"""
        state = db.query(DegradationModeState).filter(
            DegradationModeState.active == 1
        ).order_by(DegradationModeState.activated_at.desc()).first()
        if state is None:
            return "normal"
        return state.mode

    def auto_detect(self, db: Session):
        """检查健康指标，自动决定是否需要降级（注册为 periodic）。

        简化策略：若当前无降级但检测到问题则激活。
        """
        current = self.current_mode(db)
        # 示例健康检查：数据库连接可用性等
        # 实际环境中根据健康指标决定
        try:
            from sqlalchemy import text
            db.execute(text("SELECT 1"))
        except Exception:
            if current == "normal":
                self.activate(db, "critical", "database unreachable", "auto")
            return

        # 健康恢复时可考虑自动降级回退
        # 这里仅做激活检测，恢复由手动或外部触发 deactivate

    def should_degrade(self, service_name: str) -> dict:
        """检查中间件用：该请求是否应被降级处理。

        返回 {"degraded": bool, "mode": str, "guidance": str}
        """
        db: Session = SessionLocal()
        try:
            mode = self.current_mode(db)
        finally:
            db.close()

        if mode == "normal":
            return {"degraded": False, "mode": "normal", "guidance": ""}

        # 根据 mode 决定哪些服务被降级
        degradation_map = {
            "read_only": {
                "guidance": "System is in read-only mode; write operations will be rejected",
                "degrade_write": True,
            },
            "cache_only": {
                "guidance": "System is serving cached responses; real-time data is unavailable",
                "degrade_write": True,
            },
            "critical": {
                "guidance": "System is in emergency mode; only core features are available",
                "degrade_write": True,
            },
        }

        info = degradation_map.get(mode, {"guidance": "System is degrading", "degrade_write": False})
        is_write = service_name in ("create", "update", "delete", "post", "put", "patch")
        degraded = info.get("degrade_write", False) and is_write

        return {
            "degraded": degraded,
            "mode": mode,
            "guidance": info["guidance"],
        }


graceful_degradation = GracefulDegradationService()
