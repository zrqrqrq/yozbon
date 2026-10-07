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
"""P2 多租户隔离服务。

提供租户的创建、配置、配额检查和宿主分配。
每个租户拥有独立的宿主容量上限和 AI 公民容量上限。

设计：
- 默认租户 "default" 包含所有未显式分配的宿主；
- check_quota 在资源创建前调用，超出上限则拒绝；
- 租户停用后其下宿主仍可读取但不能新建资源。
"""
import json
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from .database import SessionLocal
from .config import settings
from .models import Tenant

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.utcnow()


# 内存中的宿主-租户映射（生产应持久化）
_host_tenant_map: dict = {}  # host_id -> tenant_code


class TenantManager:
    """多租户管理服务。"""

    def create_tenant(self, code: str, name: str, plan: str = "free",
                      max_hosts: int = 100, max_ai_citizens: int = 500) -> dict:
        """创建新租户。"""
        if not code or not code.replace("_", "").replace("-", "").isalnum():
            raise ValueError("Tenant code may only contain letters, digits, underscores and hyphens")

        db: Session = SessionLocal()
        try:
            existing = db.query(Tenant).filter(Tenant.tenant_code == code).first()
            if existing:
                raise ValueError(f"Tenant {code} already exists")

            tenant = Tenant(
                tenant_code=code,
                name=name,
                plan=plan,
                max_hosts=max_hosts,
                max_ai_citizens=max_ai_citizens,
                config="{}",
                is_active=1,
            )
            db.add(tenant)
            db.commit()
            logger.info("multi_tenant: created tenant %s plan=%s max_hosts=%d", code, plan, max_hosts)
            return self._serialize(tenant)
        finally:
            db.close()

    def get_tenant(self, code: str) -> dict:
        """获取租户详情。"""
        db: Session = SessionLocal()
        try:
            tenant = db.query(Tenant).filter(Tenant.tenant_code == code).first()
            if tenant is None:
                raise ValueError(f"Tenant {code} not found")
            return self._serialize(tenant)
        finally:
            db.close()

    def list_tenants(self) -> list:
        """列出所有租户。"""
        db: Session = SessionLocal()
        try:
            tenants = db.query(Tenant).order_by(Tenant.tenant_code).all()
            return [self._serialize(t) for t in tenants]
        finally:
            db.close()

    def update_tenant(self, code: str, **kwargs) -> dict:
        """更新租户配置（name, plan, max_hosts, max_ai_citizens, config）。"""
        db: Session = SessionLocal()
        try:
            tenant = db.query(Tenant).filter(Tenant.tenant_code == code).first()
            if tenant is None:
                raise ValueError(f"Tenant {code} not found")

            updatable = {"name", "plan", "max_hosts", "max_ai_citizens", "config"}
            for k, v in kwargs.items():
                if k in updatable:
                    setattr(tenant, k, v)

            db.commit()
            logger.info("multi_tenant: updated tenant %s fields=%s", code, list(kwargs.keys()))
            return self._serialize(tenant)
        finally:
            db.close()

    def assign_host(self, host_id: int, tenant_code: str) -> dict:
        """将宿主分配到指定租户。"""
        db: Session = SessionLocal()
        try:
            tenant = db.query(Tenant).filter(Tenant.tenant_code == tenant_code).first()
            if tenant is None:
                raise ValueError(f"Tenant {tenant_code} not found")
            if not tenant.is_active:
                raise ValueError(f"Tenant {tenant_code} is disabled")

            _host_tenant_map[host_id] = tenant_code
            logger.info("multi_tenant: assigned host %d to tenant %s", host_id, tenant_code)
            return {"host_id": host_id, "tenant_code": tenant_code}
        finally:
            db.close()

    def check_quota(self, tenant_code: str, resource_type: str) -> dict:
        """检查租户资源配额是否充足。

        Args:
            tenant_code: 租户编码。
            resource_type: 资源类型 "hosts" 或 "ai_citizens"。

        Returns:
            {"tenant_code", "resource_type", "current", "max", "allowed"}
        """
        db: Session = SessionLocal()
        try:
            tenant = db.query(Tenant).filter(Tenant.tenant_code == tenant_code).first()
            if tenant is None:
                raise ValueError(f"Tenant {tenant_code} not found")

            # 计算当前使用量
            if resource_type == "hosts":
                current = sum(1 for hid, tc in _host_tenant_map.items() if tc == tenant_code)
                max_val = tenant.max_hosts
            elif resource_type == "ai_citizens":
                # 简化：此处应查 DB 统计
                current = 0
                max_val = tenant.max_ai_citizens
            else:
                raise ValueError(f"Unknown resource type: {resource_type}")

            allowed = current < max_val
            return {
                "tenant_code": tenant_code,
                "resource_type": resource_type,
                "current": current,
                "max": max_val,
                "remaining": max(0, max_val - current),
                "allowed": allowed,
            }
        finally:
            db.close()

    def get_tenant_stats(self, tenant_code: str) -> dict:
        """获取租户统计信息。"""
        db: Session = SessionLocal()
        try:
            tenant = db.query(Tenant).filter(Tenant.tenant_code == tenant_code).first()
            if tenant is None:
                raise ValueError(f"Tenant {tenant_code} not found")

            host_count = sum(1 for tc in _host_tenant_map.values() if tc == tenant_code)
            return {
                "tenant_code": tenant_code,
                "name": tenant.name,
                "plan": tenant.plan,
                "hosts_used": host_count,
                "hosts_max": tenant.max_hosts,
                "ai_citizens_max": tenant.max_ai_citizens,
                "is_active": bool(tenant.is_active),
                "created_at": tenant.created_at.isoformat() if tenant.created_at else None,
            }
        finally:
            db.close()

    def deactivate_tenant(self, code: str) -> dict:
        """停用租户（不删除数据，阻止新资源创建）。"""
        db: Session = SessionLocal()
        try:
            tenant = db.query(Tenant).filter(Tenant.tenant_code == code).first()
            if tenant is None:
                raise ValueError(f"Tenant {code} not found")

            tenant.is_active = 0
            db.commit()
            logger.warning("multi_tenant: deactivated tenant %s", code)
            return {"tenant_code": code, "is_active": False}
        finally:
            db.close()

    # ---- 内部方法 ----

    @staticmethod
    def _serialize(tenant: Tenant) -> dict:
        return {
            "id": tenant.id,
            "tenant_code": tenant.tenant_code,
            "name": tenant.name,
            "plan": tenant.plan,
            "max_hosts": tenant.max_hosts,
            "max_ai_citizens": tenant.max_ai_citizens,
            "config": json.loads(tenant.config or "{}"),
            "is_active": bool(tenant.is_active),
            "created_at": tenant.created_at.isoformat() if tenant.created_at else None,
        }


instance = TenantManager()
