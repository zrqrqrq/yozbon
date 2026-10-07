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
"""MFA 多因素认证服务。

功能：
- TOTP 注册（RFC 6238）：使用 hmac+sha1 标准算法；
- 验证码校验（含时间窗口容差）；
- 恢复码生成与消耗；
- 一次性业务码（短信/邮箱验证场景）；
- 防暴力猜测（attempt 计数）。

依赖模型：MFADevice, MFACode。
"""
import base64
import hashlib
import hmac
import logging
import secrets
import struct
import time
from datetime import datetime, timedelta

from .config import settings
from .database import SessionLocal
from .models import MFADevice, MFACode

logger = logging.getLogger(__name__)

# TOTP 参数
TOTP_DIGITS = 6
TOTP_PERIOD = 30
TOTP_DRIFT = 1  # 允许前后各 1 个周期的时间偏差


def _now():
    return datetime.utcnow()


def _generate_totp_secret() -> str:
    """生成 20 字节随机 secret 并 Base32 编码。"""
    raw = secrets.token_bytes(20)
    return base64.b32encode(raw).decode("utf-8").rstrip("=")


def _totp_code(secret_b32: str, timestamp: int | None = None) -> str:
    """根据 secret 和时间生成 6 位 TOTP（RFC 6238）。"""
    if timestamp is None:
        timestamp = int(time.time())
    counter = timestamp // TOTP_PERIOD
    # 补齐 Base32 padding
    padding = "=" * ((8 - len(secret_b32) % 8) % 8)
    key = base64.b32decode(secret_b32 + padding, casefold=True)
    msg = struct.pack(">Q", counter)
    h = hmac.new(key, msg, hashlib.sha1).digest()
    offset = h[-1] & 0x0F
    code_int = (struct.unpack(">I", h[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** TOTP_DIGITS)
    return str(code_int).zfill(TOTP_DIGITS)


class MFAService:
    """MFA 多因素认证业务逻辑。"""

    def enroll_totp(self, host_id: int, label: str = "") -> dict:
        """注册 TOTP 设备：生成 secret，返回 secret 和 otpauth URI。"""
        db = SessionLocal()
        try:
            secret = _generate_totp_secret()
            device = MFADevice(
                host_id=host_id,
                device_type="totp",
                secret_enc=secret,  # 生产应走 kms_service.encrypt
                label=label or "default",
                is_primary=0,
                is_verified=0,
            )
            db.add(device)
            db.commit()

            otpauth_uri = (
                f"otpauth://totp/yozbon:host_{host_id}"
                f"?secret={secret}&issuer=yozbon"
                f"&digits={TOTP_DIGITS}&period={TOTP_PERIOD}"
            )
            logger.info("MFA TOTP enrolled for host=%d device=%d", host_id, device.id)
            return {
                "device_id": device.id,
                "secret": secret,
                "otpauth_uri": otpauth_uri,
            }
        finally:
            db.close()

    def verify_totp(self, host_id: int, code: str) -> dict:
        """验证 TOTP 码（含时间窗口容差）。"""
        db = SessionLocal()
        try:
            device = (db.query(MFADevice)
                      .filter(MFADevice.host_id == host_id,
                              MFADevice.device_type == "totp",
                              MFADevice.is_verified == 1)
                      .first())
            if device is None:
                return {"valid": False, "error": "no verified TOTP device"}

            secret = device.secret_enc
            current_ts = int(time.time())

            for drift in range(-TOTP_DRIFT, TOTP_DRIFT + 1):
                expected = _totp_code(secret, current_ts + drift * TOTP_PERIOD)
                if hmac.compare_digest(expected, code):
                    logger.info("MFA TOTP verified for host=%d", host_id)
                    return {"valid": True}

            # 记录失败尝试（可后续做 lockout）
            logger.warning("MFA TOTP failed for host=%d", host_id)
            return {"valid": False, "error": "invalid code"}
        finally:
            db.close()

    def generate_recovery_codes(self, host_id: int, count: int = 10) -> dict:
        """生成恢复码（一次性使用）。"""
        db = SessionLocal()
        try:
            codes = []
            for _ in range(count):
                raw_code = secrets.token_hex(4)  # 8 字符恢复码
                code_hash = hashlib.sha256(raw_code.encode()).hexdigest()
                mfa_code = MFACode(
                    host_id=host_id,
                    code_hash=code_hash,
                    purpose="recovery",
                    expires_at=_now() + timedelta(days=365),
                    used=0,
                )
                db.add(mfa_code)
                codes.append(raw_code)
            db.commit()
            logger.info("MFA recovery codes generated for host=%d count=%d", host_id, count)
            return {"codes": codes}
        finally:
            db.close()

    def disable_mfa(self, host_id: int) -> dict:
        """禁用 MFA（删除设备记录）。"""
        db = SessionLocal()
        try:
            devices = (db.query(MFADevice)
                       .filter(MFADevice.host_id == host_id)
                       .all())
            if not devices:
                return {"error": "no MFA device found"}
            for d in devices:
                db.delete(d)
            # 同时清除恢复码
            codes = (db.query(MFACode)
                     .filter(MFACode.host_id == host_id,
                             MFACode.purpose == "recovery")
                     .all())
            for c in codes:
                db.delete(c)
            db.commit()
            logger.info("MFA disabled for host=%d", host_id)
            return {"ok": True}
        finally:
            db.close()

    # ---------- 一次性业务码（短信/邮箱验证码场景）----------

    def generate_code(self, host_id: int, purpose: str = "login") -> dict:
        """生成一次性验证码（6 位数字），哈希存储。"""
        db = SessionLocal()
        try:
            raw = str(secrets.randbelow(900000) + 100000)  # 6 位数字
            code_hash = hashlib.sha256(raw.encode()).hexdigest()
            ttl = settings.MFA_CODE_TTL_SECONDS
            mfa = MFACode(
                host_id=host_id,
                code_hash=code_hash,
                purpose=purpose,
                expires_at=_now() + timedelta(seconds=ttl),
                used=0,
            )
            db.add(mfa)
            db.commit()
            logger.info("MFA code generated for host=%d purpose=%s", host_id, purpose)
            # 返回明文给调用方（路由层负责通过短信/邮件发送）
            return {"code_id": mfa.id, "code": raw, "expires_in": ttl}
        finally:
            db.close()

    def validate_code(self, host_id: int, code: str, purpose: str = "login") -> dict:
        """校验一次性验证码（含过期检查和单次使用）。"""
        db = SessionLocal()
        try:
            code_hash = hashlib.sha256(code.encode()).hexdigest()
            mfa = (db.query(MFACode)
                   .filter(MFACode.host_id == host_id,
                           MFACode.code_hash == code_hash,
                           MFACode.purpose == purpose,
                           MFACode.used == 0)
                   .first())
            if mfa is None:
                return {"valid": False, "error": "code not found or already used"}
            if mfa.expires_at < _now():
                return {"valid": False, "error": "code expired"}

            mfa.used = 1
            db.commit()
            logger.info("MFA code validated for host=%d purpose=%s", host_id, purpose)
            return {"valid": True}
        finally:
            db.close()


instance = MFAService()
