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
"""CORS 策略管理服务（P0 安全）。

功能：
- 多租户 CORS 策略增删查；
- 生成可直接传给 FastAPI CORSMiddleware 的配置字典。

依赖模型：CORSPolicy。
"""
import logging
from datetime import datetime, timedelta

from .config import settings
from .database import SessionLocal
from .models import CORSPolicy

logger = logging.getLogger(__name__)


def _now():
    return datetime.utcnow()


class CORSService:
    """多租户 CORS 策略管理。"""

    def add_policy(self, db, origin_pattern: str, methods: str = "*",
                   headers: str = "*", credentials: bool = False,
                   max_age: int = 3600, tenant_code: str = "default"):
        """添加 CORS 策略。

        Args:
            db: SQLAlchemy session。
            origin_pattern: 允许的 origin 模式。
            methods: 允许的 HTTP 方法（逗号分隔或 "*"）。
            headers: 允许的 headers（逗号分隔或 "*"）。
            credentials: 是否允许携带凭证。
            max_age: 预检缓存秒数。
            tenant_code: 租户编码。

        Returns:
            创建的 CORSPolicy 对象。
        """
        policy = CORSPolicy(
            origin_pattern=origin_pattern,
            methods=methods,
            headers=headers,
            credentials=1 if credentials else 0,
            max_age=max_age,
            tenant_code=tenant_code,
        )
        db.add(policy)
        db.commit()
        return policy

    def remove_policy(self, db, policy_id: int):
        """删除 CORS 策略。"""
        deleted = db.query(CORSPolicy).filter(CORSPolicy.id == policy_id).delete()
        db.commit()
        return deleted > 0

    def get_policies(self, db, tenant_code: str = "default") -> list:
        """获取当前租户的 CORS 策略列表。

        Returns:
            [{"id": int, "origin_pattern": str, "methods": str, ...}, ...]
        """
        policies = db.query(CORSPolicy).filter(
            CORSPolicy.tenant_code == tenant_code
        ).all()
        return [
            {
                "id": p.id,
                "origin_pattern": p.origin_pattern,
                "methods": p.methods,
                "headers": p.headers,
                "credentials": bool(p.credentials),
                "max_age": p.max_age,
            }
            for p in policies
        ]

    def build_middleware_config(self, db, tenant_code: str = "default") -> dict:
        """返回可直接传给 FastAPI CORSMiddleware 的参数 dict。

        Returns:
            {"allow_origins": list, "allow_methods": list,
             "allow_headers": list, "allow_credentials": bool, "max_age": int}
        """
        policies = db.query(CORSPolicy).filter(
            CORSPolicy.tenant_code == tenant_code
        ).all()
        if not policies:
            # fallback: 使用 settings.CORS_ORIGINS
            origins = [o.strip() for o in settings.CORS_ORIGINS.split(",")]
            return {
                "allow_origins": origins,
                "allow_methods": ["*"],
                "allow_headers": ["*"],
                "allow_credentials": False,
                "max_age": 3600,
            }

        allow_origins = list(set(p.origin_pattern for p in policies))
        methods_set = set()
        headers_set = set()
        allow_credentials = False
        max_age = 3600

        for p in policies:
            if p.methods == "*":
                methods_set = {"*"}
            else:
                methods_set.update(m.strip() for m in p.methods.split(","))
            if p.headers == "*":
                headers_set = {"*"}
            else:
                headers_set.update(h.strip() for h in p.headers.split(","))
            if p.credentials:
                allow_credentials = True
            max_age = max(max_age, p.max_age)

        return {
            "allow_origins": allow_origins,
            "allow_methods": list(methods_set),
            "allow_headers": list(headers_set),
            "allow_credentials": allow_credentials,
            "max_age": max_age,
        }


cors_config = CORSService()
