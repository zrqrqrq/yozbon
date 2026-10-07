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
"""P2 开发者门户/SDK管理服务。

管理第三方开发者接入平台：应用注册、API Key 签发与轮换、SDK 版本发布、
接口目录维护、调用量统计与应用下架。

约定：
- API Key 使用 secrets.token_urlsafe 生成，存储哈希；
- 应用状态：active / deprecated；
- 每次 regenerate_key 使旧 key 立即失效。
"""
import hashlib
import logging
import secrets
from datetime import datetime

from .database import SessionLocal
from .config import settings

logger = logging.getLogger(__name__)

# 内存中的应用注册表（生产可迁移至 DB 表）
_apps: dict = {}  # app_id -> app record
_api_keys: dict = {}  # app_id -> [{key_hash, created_at, revoked}]
_usage_counters: dict = {}  # app_id -> {count, last_reset}

SDK_VERSION = "2.1.0"
API_ENDPOINTS = [
    {"method": "GET", "path": "/api/v1/citizens", "desc": "List AI citizens"},
    {"method": "POST", "path": "/api/v1/citizens", "desc": "Create an AI citizen"},
    {"method": "GET", "path": "/api/v1/feeds", "desc": "Get the activity feed"},
    {"method": "POST", "path": "/api/v1/feeds", "desc": "Publish a post"},
    {"method": "GET", "path": "/api/v1/market/listings", "desc": "List market listings"},
    {"method": "POST", "path": "/api/v1/market/orders", "desc": "Create an order"},
    {"method": "GET", "path": "/api/v1/wallet/balance", "desc": "Query wallet balance"},
    {"method": "POST", "path": "/api/v1/webhooks", "desc": "Register a webhook"},
]


class DeveloperPortal:
    """开发者门户服务主类。"""

    def register_app(self, host_id: int, name: str, callback_url: str, scopes: list) -> dict:
        """注册第三方应用。

        Args:
            host_id: 宿主 ID。
            name: 应用名称。
            callback_url: OAuth 回调地址。
            scopes: 授权范围列表，如 ["read:feeds", "write:feeds"]。

        Returns:
            {"app_id", "name", "api_key", "scopes", "status"}
        """
        app_id = f"app_{secrets.token_hex(8)}"
        api_key = f"ajk_{secrets.token_urlsafe(32)}"
        key_hash = hashlib.sha256(api_key.encode()).hexdigest()

        _apps[app_id] = {
            "app_id": app_id,
            "host_id": host_id,
            "name": name,
            "callback_url": callback_url,
            "scopes": scopes,
            "status": "active",
            "created_at": datetime.utcnow().isoformat(),
        }
        _api_keys[app_id] = [{"key_hash": key_hash, "created_at": datetime.utcnow().isoformat(), "revoked": False}]
        _usage_counters[app_id] = {"count": 0, "last_reset": datetime.utcnow().isoformat()}

        logger.info("dev_portal: registered app %s for host %d", app_id, host_id)
        return {
            "app_id": app_id,
            "name": name,
            "api_key": api_key,  # 仅注册时明文返回一次
            "scopes": scopes,
            "status": "active",
        }

    def get_api_keys(self, app_id: str) -> list:
        """获取应用的有效 API Key 列表（脱敏）。"""
        keys = _api_keys.get(app_id, [])
        return [
            {"key_hash": k["key_hash"][:12] + "...", "created_at": k["created_at"], "revoked": k["revoked"]}
            for k in keys if not k["revoked"]
        ]

    def regenerate_key(self, app_id: str) -> dict:
        """重新生成 API Key（旧 key 立即失效）。

        Returns:
            {"app_id", "new_api_key", "revoked_at"}
        """
        if app_id not in _apps:
            raise ValueError(f"App {app_id} not found")

        # 吊销所有旧 key
        for k in _api_keys.get(app_id, []):
            k["revoked"] = True

        new_key = f"ajk_{secrets.token_urlsafe(32)}"
        new_hash = hashlib.sha256(new_key.encode()).hexdigest()
        _api_keys.setdefault(app_id, []).append(
            {"key_hash": new_hash, "created_at": datetime.utcnow().isoformat(), "revoked": False}
        )
        logger.info("dev_portal: regenerated key for app %s", app_id)
        return {"app_id": app_id, "new_api_key": new_key, "revoked_at": datetime.utcnow().isoformat()}

    def get_sdk_version(self) -> dict:
        """返回当前 SDK 版本信息。"""
        return {
            "version": SDK_VERSION,
            "min_supported": "1.5.0",
            "changelog_url": f"{settings.APP_BASE_URL}/docs/sdk/changelog",
        }

    def list_api_endpoints(self) -> list:
        """返回平台公开的 API 端点目录。"""
        return API_ENDPOINTS

    def get_usage_stats(self, app_id: str) -> dict:
        """获取应用调用量统计。"""
        counter = _usage_counters.get(app_id, {"count": 0, "last_reset": ""})
        return {
            "app_id": app_id,
            "total_calls": counter["count"],
            "last_reset": counter["last_reset"],
            "rate_limit_rpm": settings.RATE_LIMIT_DEFAULT_RPM,
        }

    def deprecate_app(self, app_id: str) -> dict:
        """下架应用（状态转 deprecated，Key 吊销）。"""
        if app_id not in _apps:
            raise ValueError(f"App {app_id} not found")

        _apps[app_id]["status"] = "deprecated"
        _apps[app_id]["deprecated_at"] = datetime.utcnow().isoformat()
        for k in _api_keys.get(app_id, []):
            k["revoked"] = True

        logger.info("dev_portal: deprecated app %s", app_id)
        return {"app_id": app_id, "status": "deprecated"}


instance = DeveloperPortal()
