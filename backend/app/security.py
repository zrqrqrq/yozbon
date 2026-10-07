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
"""认证：复用 RunVerseHub security.py 的模式（密码哈希 + JWT）。"""
import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta

from .config import settings


def hash_password(password: str) -> str:
    """PBKDF2-SHA256（与 RunVerseHub 同款，密码明文不进库）。"""
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
    return f"{salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, hex_dk = stored.split("$", 1)
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
    return hmac.compare_digest(dk.hex(), hex_dk)


# token scope（§14 AI 访问权限分层：readonly / workflow / host）
TOKEN_SCOPES = ("readonly", "workflow", "host")


def create_token(sub, typ: str = "host", scope: str | None = None) -> str:
    """JWT 风格签名 token（HS256，复用 RunVerseHub 约定）。

    §14：payload 增 scope 字段区分令牌档位：
      typ="host"   → scope="host"    （人类宿主）
      typ="ai_ro"  → scope="readonly"（公开前端注册 AI 的只读会话，sub=citizen_id）
      AI key aik_* → scope="workflow"（API 正式入驻后端密钥，见 deps.issue_ai_key）
    """
    import base64
    import json
    header = base64.urlsafe_b64encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode()).rstrip(b"=").decode()
    data = {"sub": sub, "typ": typ, "jti": str(uuid.uuid4()),
            "exp": (datetime.utcnow() + timedelta(minutes=settings.JWT_EXPIRE_MINUTES)).timestamp()}
    if scope:
        data["scope"] = scope
    payload = base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()
    sig = hmac.new(settings.SECRET_KEY.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    return f"{header}.{payload}.{base64.urlsafe_b64encode(sig).rstrip(b'=').decode()}"


def decode_token(token: str) -> dict:
    """返回 payload；无效/过期抛 ValueError。"""
    import base64
    import json
    try:
        header, payload, sig = token.split(".")
    except ValueError:
        raise ValueError("bad token")
    expected = hmac.new(settings.SECRET_KEY.encode(),
                        f"{header}.{payload}".encode(), hashlib.sha256).digest()
    got = base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4))
    if not hmac.compare_digest(got, expected):
        raise ValueError("bad signature")
    data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    if data.get("exp", 0) < datetime.utcnow().timestamp():
        raise ValueError("expired")
    return data
