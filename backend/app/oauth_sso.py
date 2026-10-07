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
"""OAuth2/OIDC 统一登录服务。

功能：
- 生成第三方 OAuth 授权跳转 URL；
- 处理回调（exchange code → token → userinfo → link/unlink account）；
- 管理用户与第三方账号绑定关系；
- 支持 token 自动刷新。

依赖模型：OAuthProvider, OAuthConnection。
"""
import hashlib
import hmac
import logging
from datetime import datetime, timedelta

import httpx

from .config import settings
from .database import SessionLocal
from .models import OAuthProvider, OAuthConnection

logger = logging.getLogger(__name__)


def _now():
    return datetime.utcnow()


class OAuthService:
    """OAuth2/OIDC 统一登录业务逻辑。"""

    # ---------- 授权跳转 ----------

    def get_authorize_url(self, provider: str, state: str) -> dict:
        """生成 OAuth 授权跳转 URL 和 state 签名。

        Args:
            provider: 提供方标识（google/github/microsoft）。
            state: 客户端传入的 CSRF state（与 server_secret 联合签名防篡改）。

        Returns:
            {"authorize_url": str, "state": str} 或 error。
        """
        db = SessionLocal()
        try:
            prov = (db.query(OAuthProvider)
                    .filter(OAuthProvider.provider_name == provider,
                            OAuthProvider.is_active == 1)
                    .first())
            if prov is None:
                return {"error": f"provider '{provider}' not found or inactive"}

            # 签名 state 防篡改
            signed_state = hmac.new(
                settings.OAUTH_STATE_SECRET.encode(),
                state.encode(),
                hashlib.sha256,
            ).hexdigest()[:32]

            url = (
                f"{prov.authorize_url}"
                f"?client_id={prov.client_id}"
                f"&redirect_uri={settings.APP_BASE_URL}/api/auth/oauth/{provider}/callback"
                f"&response_type=code"
                f"&scope={prov.scopes.replace(' ', '%20')}"
                f"&state={state}.{signed_state}"
            )
            return {"authorize_url": url, "state": f"{state}.{signed_state}"}
        finally:
            db.close()

    # ---------- 回调处理 ----------

    def handle_callback(self, provider: str, code: str, state: str) -> dict:
        """处理 OAuth 回调：验证 state → 交换 token → 获取用户信息 → 绑定/登录。

        Returns:
            {"host_id": int, "provider": str, "external_id": str, "email": str}
            或 {"error": str}。
        """
        # 验证 state 签名
        parts = state.rsplit(".", 1)
        if len(parts) != 2:
            return {"error": "invalid state format"}
        raw_state, sig = parts
        expected_sig = hmac.new(
            settings.OAUTH_STATE_SECRET.encode(),
            raw_state.encode(),
            hashlib.sha256,
        ).hexdigest()[:32]
        if not hmac.compare_digest(sig, expected_sig):
            return {"error": "state signature mismatch"}

        db = SessionLocal()
        try:
            prov = (db.query(OAuthProvider)
                    .filter(OAuthProvider.provider_name == provider,
                            OAuthProvider.is_active == 1)
                    .first())
            if prov is None:
                return {"error": "provider not found"}

            # 交换 code → token（实际生产走 httpx.post）
            token_data = self._exchange_code(prov, code)
            if "error" in token_data:
                return token_data

            access_token = token_data.get("access_token", "")
            refresh_token = token_data.get("refresh_token", "")
            expires_in = token_data.get("expires_in", 3600)

            # 获取用户信息
            userinfo = self._fetch_userinfo(prov, access_token)
            if "error" in userinfo:
                return userinfo

            external_id = str(userinfo.get("sub") or userinfo.get("id", ""))
            email = userinfo.get("email", "")

            if not external_id:
                return {"error": "cannot extract external user id"}

            # 查找已有连接或自动创建
            conn = (db.query(OAuthConnection)
                    .filter(OAuthConnection.provider_name == provider,
                            OAuthConnection.external_user_id == external_id)
                    .first())

            if conn is None:
                # 首次登录：需前端带 host_id 调用 link_account
                return {
                    "new_user": True,
                    "provider": provider,
                    "external_id": external_id,
                    "email": email,
                }

            # 更新 token
            conn.access_token_enc = access_token  # 生产应走 kms_service.encrypt
            conn.refresh_token_enc = refresh_token
            conn.token_expires_at = _now() + timedelta(seconds=expires_in)
            db.commit()

            return {
                "host_id": conn.host_id,
                "provider": provider,
                "external_id": external_id,
                "email": conn.email or email,
            }
        finally:
            db.close()

    # ---------- 账号绑定 ----------

    def link_account(self, host_id: int, provider: str, external_id: str, email: str = "") -> dict:
        """绑定第三方账号到宿主。"""
        db = SessionLocal()
        try:
            # 检查是否已绑定
            existing = (db.query(OAuthConnection)
                        .filter(OAuthConnection.host_id == host_id,
                                OAuthConnection.provider_name == provider,
                                OAuthConnection.external_user_id == external_id)
                        .first())
            if existing:
                return {"error": "account already linked"}

            conn = OAuthConnection(
                host_id=host_id,
                provider_name=provider,
                external_user_id=external_id,
                email=email,
            )
            db.add(conn)
            db.commit()
            logger.info("OAuth linked: host=%d provider=%s ext=%s", host_id, provider, external_id)
            return {"ok": True, "connection_id": conn.id}
        finally:
            db.close()

    def unlink_account(self, host_id: int, provider: str) -> dict:
        """解绑第三方账号。"""
        db = SessionLocal()
        try:
            conns = (db.query(OAuthConnection)
                     .filter(OAuthConnection.host_id == host_id,
                             OAuthConnection.provider_name == provider)
                     .all())
            if not conns:
                return {"error": "no linked account found"}
            for c in conns:
                db.delete(c)
            db.commit()
            logger.info("OAuth unlinked: host=%d provider=%s", host_id, provider)
            return {"ok": True, "removed": len(conns)}
        finally:
            db.close()

    # ---------- Token 刷新 ----------

    def refresh_token(self, connection_id: int) -> dict:
        """刷新已过期的 access_token。"""
        db = SessionLocal()
        try:
            conn = db.get(OAuthConnection, connection_id)
            if conn is None:
                return {"error": "connection not found"}
            if not conn.refresh_token_enc:
                return {"error": "no refresh token available"}

            prov = (db.query(OAuthProvider)
                    .filter(OAuthProvider.provider_name == conn.provider_name,
                            OAuthProvider.is_active == 1)
                    .first())
            if prov is None:
                return {"error": "provider unavailable"}

            token_data = self._do_refresh(prov, conn.refresh_token_enc)
            if "error" in token_data:
                return token_data

            conn.access_token_enc = token_data.get("access_token", conn.access_token_enc)
            if token_data.get("refresh_token"):
                conn.refresh_token_enc = token_data["refresh_token"]
            conn.token_expires_at = _now() + timedelta(
                seconds=token_data.get("expires_in", 3600))
            db.commit()
            return {"ok": True, "expires_at": conn.token_expires_at.isoformat()}
        finally:
            db.close()

    # ---------- 内部方法 ----------

    def _exchange_code(self, prov: OAuthProvider, code: str) -> dict:
        """向 provider 交换 code → token（生产环境实际 HTTP 请求）。"""
        try:
            resp = httpx.post(prov.token_url, data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": prov.client_id,
                "client_secret": prov.client_secret_enc,  # 应走 decrypt
                "redirect_uri": f"{settings.APP_BASE_URL}/api/auth/oauth/{prov.provider_name}/callback",
            }, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.warning("OAuth code exchange failed: %s", exc)
            return {"error": f"token exchange failed: {exc}"}

    def _fetch_userinfo(self, prov: OAuthProvider, access_token: str) -> dict:
        """拉取第三方 userinfo。"""
        try:
            resp = httpx.get(prov.userinfo_url, headers={
                "Authorization": f"Bearer {access_token}",
            }, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.warning("OAuth userinfo fetch failed: %s", exc)
            return {"error": f"userinfo fetch failed: {exc}"}

    def _do_refresh(self, prov: OAuthProvider, refresh_token: str) -> dict:
        """执行 refresh token 请求。"""
        try:
            resp = httpx.post(prov.token_url, data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": prov.client_id,
                "client_secret": prov.client_secret_enc,
            }, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.warning("OAuth refresh failed: %s", exc)
            return {"error": f"refresh failed: {exc}"}


instance = OAuthService()
