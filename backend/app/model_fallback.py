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
"""模型降级引擎（P0 安全）。

功能：
- 注册模型降级规则（主模型 + fallback 链）；
- 根据任务类型解析当前应使用的模型；
- 报告失败时自动切换到下一个 fallback；
- 报告成功时重置失败计数；
- 返回当前可用模型链（排除已知失败的）。

依赖模型：ModelFallbackRule。
"""
import json
import logging
from datetime import datetime, timedelta

from .config import settings
from .database import SessionLocal
from .models import ModelFallbackRule

logger = logging.getLogger(__name__)


def _now():
    return datetime.utcnow()


# C-D12 说明：_failure_counts 为进程级熔断计数器（非持久化）。
# 设计理由：(1) 失败计数是瞬态信号，成功一次即重置，无需跨重启保留；
#          (2) 多 worker 场景下各 worker 独立计熔断是可接受的保守策略
#              （每个 worker 独立探测到故障后各自降级，比集中式更快响应）；
#          (3) 生产若需共享熔断窗口，接入 Redis INCR + TTL 替代本 dict。
_failure_counts: dict = {}  # {(rule_id, model_name): int}


class ModelFallbackEngine:
    """模型降级链引擎。"""

    def register_rule(self, db, name: str, primary_model: str,
                      fallback_chain: list, trigger_conditions: dict,
                      timeout_ms: int = 30000):
        """创建模型降级规则。

        Args:
            db: SQLAlchemy session。
            name: 规则名称（如 "text_generation"）。
            primary_model: 主模型标识。
            fallback_chain: 降级模型链 ["model_b", "model_c"]。
            trigger_conditions: 触发降级的条件 {"timeout": true, "error_5xx": true}。
            timeout_ms: 超时毫秒。

        Returns:
            创建的规则对象。
        """
        rule = ModelFallbackRule(
            name=name,
            primary_model=primary_model,
            fallback_chain=json.dumps(fallback_chain),
            trigger_conditions=json.dumps(trigger_conditions),
            timeout_ms=timeout_ms,
            enabled=1,
        )
        db.add(rule)
        db.commit()
        return rule

    def resolve(self, db, task_type: str) -> dict:
        """根据任务类型返回当前应使用的模型和备选链。

        Args:
            db: SQLAlchemy session。
            task_type: 任务类型（匹配 rule.name）。

        Returns:
            {"primary": str, "chain": list, "rule_id": int}
        """
        rule = db.query(ModelFallbackRule).filter(
            ModelFallbackRule.name == task_type,
            ModelFallbackRule.enabled == 1,
        ).first()
        if not rule:
            return {"primary": "default", "chain": [], "rule_id": 0}
        chain = json.loads(rule.fallback_chain) if rule.fallback_chain else []
        return {"primary": rule.primary_model, "chain": chain, "rule_id": rule.id}

    def report_failure(self, db, rule_id: int, model_name: str) -> str:
        """标记某模型失败，返回下一个 fallback。

        Args:
            db: SQLAlchemy session。
            rule_id: 规则 ID。
            model_name: 失败的模型名。

        Returns:
            下一个可用模型名（若全部失败返回 "none"）。
        """
        key = (rule_id, model_name)
        _failure_counts[key] = _failure_counts.get(key, 0) + 1

        rule = db.query(ModelFallbackRule).filter(ModelFallbackRule.id == rule_id).first()
        if not rule:
            return "none"
        chain = json.loads(rule.fallback_chain) if rule.fallback_chain else []
        full_chain = [rule.primary_model] + chain
        for m in full_chain:
            if _failure_counts.get((rule_id, m), 0) < settings.CB_FAILURE_THRESHOLD:
                return m
        return "none"

    def report_success(self, db, rule_id: int, model_name: str):
        """标记成功，重置该模型的失败计数。"""
        key = (rule_id, model_name)
        _failure_counts.pop(key, None)

    def get_active_chain(self, db, rule_id: int) -> list:
        """返回当前可用模型链（排除已知失败的）。

        Args:
            db: SQLAlchemy session。
            rule_id: 规则 ID。

        Returns:
            当前可用模型名列表。
        """
        rule = db.query(ModelFallbackRule).filter(ModelFallbackRule.id == rule_id).first()
        if not rule:
            return []
        chain = json.loads(rule.fallback_chain) if rule.fallback_chain else []
        full_chain = [rule.primary_model] + chain
        threshold = settings.CB_FAILURE_THRESHOLD
        return [
            m for m in full_chain
            if _failure_counts.get((rule_id, m), 0) < threshold
        ]


model_fallback = ModelFallbackEngine()
