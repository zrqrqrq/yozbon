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
"""P0 安全模块路由：OAuth / MFA / KMS / 限流 / 注册验证。"""
from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from ..oauth_sso import instance as oauth_svc
from ..mfa_service import instance as mfa_svc
from ..kms_service import instance as kms_svc
from ..rate_limiter import instance as rate_svc
from ..registration_guard import instance as reg_svc

router = APIRouter(prefix="/api/security", tags=["security"])


# ======================== 请求体 ========================

class OAuthAuthorizeBody(BaseModel):
    provider: str = Field(..., min_length=1)
    state: str = ""


class OAuthCallbackBody(BaseModel):
    provider: str = Field(..., min_length=1)
    code: str = Field(..., min_length=1)
    state: str = ""


class MFAEnrollBody(BaseModel):
    user_id: int
    user_type: str = "host"


class MFAVerifyBody(BaseModel):
    user_id: int
    code: str = Field(..., min_length=6, max_length=6)


class KMSCreateKeyBody(BaseModel):
    purpose: str = Field(..., min_length=1)


class KMSEncryptBody(BaseModel):
    key_id: str = Field(..., min_length=1)
    plaintext: str = Field(..., min_length=1)


class RateLimitPolicyBody(BaseModel):
    name: str = Field(..., min_length=1)
    scope: str = "global"
    limit: int = 100
    window_seconds: int = 60


class RegistrationVerifyBody(BaseModel):
    host_id: int
    token: str = ""
    captcha_answer: str = ""


# ======================== OAuth ========================

@router.post("/oauth/authorize")
def oauth_authorize(body: OAuthAuthorizeBody):
    """获取 OAuth 授权跳转 URL。"""
    state = body.state or "default"
    return oauth_svc.get_authorize_url(provider=body.provider, state=state)


@router.post("/oauth/callback")
def oauth_callback(body: OAuthCallbackBody):
    """处理 OAuth 回调（code 换 token 并绑定账号）。"""
    return oauth_svc.handle_callback(provider=body.provider, code=body.code, state=body.state)


# ======================== MFA ========================

@router.post("/mfa/enroll")
def mfa_enroll(body: MFAEnrollBody):
    """注册 MFA（生成 TOTP secret 和二维码信息）。"""
    return mfa_svc.enroll(user_id=body.user_id, user_type=body.user_type)


@router.post("/mfa/verify")
def mfa_verify(body: MFAVerifyBody):
    """验证 TOTP 验证码。"""
    return mfa_svc.verify(user_id=body.user_id, code=body.code)


# ======================== KMS ========================

@router.post("/kms/keys")
def kms_create_key(body: KMSCreateKeyBody):
    """创建加密密钥。"""
    return kms_svc.create_key(purpose=body.purpose)


@router.post("/kms/encrypt")
def kms_encrypt(body: KMSEncryptBody):
    """使用指定密钥加密数据。"""
    return kms_svc.encrypt(key_id=body.key_id, plaintext=body.plaintext)


# ======================== Rate Limit ========================

@router.get("/rate-limit/status")
def rate_limit_status(subject: str = Query(...), endpoint: str = Query(default="*")):
    """获取当前限流状态（剩余配额、重置时间）。"""
    return rate_svc.check_limit(subject=subject, endpoint=endpoint)


@router.post("/rate-limit/policies")
def rate_limit_create_policy(body: RateLimitPolicyBody):
    """创建限流策略。"""
    return rate_svc.create_policy(
        name=body.name, scope=body.scope,
        limit=body.limit, window_seconds=body.window_seconds,
    )


# ======================== Registration ========================

@router.post("/registration/verify")
def registration_verify(body: RegistrationVerifyBody):
    """提交注册验证（邮箱/CAPTCHA）。"""
    if body.token:
        return reg_svc.verify_email(host_id=body.host_id, token=body.token)
    return reg_svc.verify_captcha(host_id=body.host_id, answer=body.captcha_answer)


@router.get("/registration/check-sybil")
def registration_check_sybil(host_id: int = Query(...), ip: str = Query(default="")):
    """女巫检测：基于 IP/指纹判断是否批量注册。"""
    return reg_svc.check_sybil(host_id=host_id, ip=ip)
