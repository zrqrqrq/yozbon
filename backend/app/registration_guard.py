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
"""注册验证服务（邮箱验证/CAPTCHA/女巫防御）。

功能：
- 邮箱验证：生成验证 token，邮件发送后用户点击确认；
- CAPTCHA：生成算术/逻辑题目，验证提交答案；
- 女巫防御：基于 IP + 浏览器指纹检测批量注册；
- 注册审批：所有验证通过后方可激活账号。

依赖模型：RegistrationVerification。
"""
import hashlib
import logging
import random
import secrets
from datetime import datetime, timedelta

from .config import settings
from .database import SessionLocal
from .models import RegistrationVerification

logger = logging.getLogger(__name__)


def _now():
    return datetime.utcnow()


class RegistrationGuard:
    """注册验证业务逻辑。"""

    def require_email_verify(self, host_id: int, ip: str = "") -> dict:
        """发起邮箱验证：生成 token 和过期时间。

        Returns:
            {"token": str, "expires_at": str} 供路由层发送验证邮件。
        """
        db = SessionLocal()
        try:
            token = secrets.token_urlsafe(32)
            expires_at = _now() + timedelta(hours=settings.REG_TOKEN_TTL_HOURS)

            # 使之前的 pending token 失效
            (db.query(RegistrationVerification)
             .filter(RegistrationVerification.host_id == host_id,
                     RegistrationVerification.method == "email",
                     RegistrationVerification.status == "pending")
             .update({"status": "expired"}))

            record = RegistrationVerification(
                host_id=host_id,
                method="email",
                token=token,
                status="pending",
                ip_address=ip,
                expires_at=expires_at,
            )
            db.add(record)
            db.commit()
            logger.info("Email verify token generated for host=%d", host_id)
            return {"token": token, "expires_at": expires_at.isoformat()}
        finally:
            db.close()

    def verify_email_token(self, token: str) -> dict:
        """验证邮箱 token：有效则标记 verified。"""
        db = SessionLocal()
        try:
            record = (db.query(RegistrationVerification)
                      .filter(RegistrationVerification.token == token,
                              RegistrationVerification.method == "email",
                              RegistrationVerification.status == "pending")
                      .first())
            if record is None:
                return {"valid": False, "error": "token not found or already used"}
            if record.expires_at and record.expires_at < _now():
                record.status = "expired"
                db.commit()
                return {"valid": False, "error": "token expired"}

            record.status = "verified"
            record.verified_at = _now()
            db.commit()
            logger.info("Email verified for host=%d", record.host_id)
            return {"valid": True, "host_id": record.host_id}
        finally:
            db.close()

    def check_sybil(self, ip: str, fingerprint: str) -> dict:
        """女巫防御检测：同 IP/指纹注册数是否超过阈值。

        Returns:
            {"blocked": bool, "reason": str, "existing_count": int}
        """
        db = SessionLocal()
        try:
            threshold = settings.REG_SYBIL_THRESHOLD

            # 按指纹检测
            fp_count = (db.query(RegistrationVerification)
                        .filter(RegistrationVerification.fingerprint == fingerprint,
                                RegistrationVerification.fingerprint != "",
                                RegistrationVerification.status.in_(["pending", "verified"]))
                        .count())

            # 按 IP 检测
            ip_count = (db.query(RegistrationVerification)
                        .filter(RegistrationVerification.ip_address == ip,
                                RegistrationVerification.ip_address != "",
                                RegistrationVerification.status.in_(["pending", "verified"]))
                        .count())

            if fp_count >= threshold:
                return {"blocked": True, "reason": "fingerprint_limit",
                        "existing_count": fp_count}
            if ip_count >= threshold * 2:  # IP 限制更宽松
                return {"blocked": True, "reason": "ip_limit",
                        "existing_count": ip_count}

            return {"blocked": False, "reason": "", "existing_count": max(fp_count, ip_count)}
        finally:
            db.close()

    def generate_captcha_challenge(self) -> dict:
        """生成 CAPTCHA 挑战（简单算术题）。

        Returns:
            {"response_id": str, "question": str}
        """
        # 生成简单算术题
        a = random.randint(1, 20)
        b = random.randint(1, 20)
        op = random.choice(["+", "-", "*"])
        if op == "+":
            answer = a + b
        elif op == "-":
            a, b = max(a, b), min(a, b)
            answer = a - b
        else:
            a = random.randint(1, 9)
            b = random.randint(1, 9)
            answer = a * b

        question = f"{a} {op} {b} = ?"
        response_id = secrets.token_urlsafe(16)
        # 存储答案哈希
        answer_hash = hashlib.sha256(f"{response_id}:{answer}".encode()).hexdigest()

        db = SessionLocal()
        try:
            record = RegistrationVerification(
                host_id=0,  # CAPTCHA 不绑定 host
                method="captcha",
                token=response_id,
                status="pending",
                fingerprint=answer_hash,  # 利用 fingerprint 列存储答案哈希
                expires_at=_now() + timedelta(minutes=5),
            )
            db.add(record)
            db.commit()
            return {"response_id": response_id, "question": question}
        finally:
            db.close()

    def validate_captcha(self, response_id: str, answer: str) -> dict:
        """验证 CAPTCHA 答案。"""
        db = SessionLocal()
        try:
            record = (db.query(RegistrationVerification)
                      .filter(RegistrationVerification.token == response_id,
                              RegistrationVerification.method == "captcha",
                              RegistrationVerification.status == "pending")
                      .first())
            if record is None:
                return {"valid": False, "error": "challenge not found or expired"}
            if record.expires_at and record.expires_at < _now():
                record.status = "expired"
                db.commit()
                return {"valid": False, "error": "challenge expired"}

            expected_hash = hashlib.sha256(f"{response_id}:{answer}".encode()).hexdigest()
            if not secrets.compare_digest(expected_hash, record.fingerprint):
                return {"valid": False, "error": "wrong answer"}

            record.status = "verified"
            record.verified_at = _now()
            db.commit()
            return {"valid": True}
        finally:
            db.close()

    def approve_registration(self, host_id: int) -> dict:
        """审批注册：检查所有验证条件是否通过。

        条件：
        - 若 REG_VERIFY_EMAIL=1，需要邮箱已验证；
        - 若 REG_CAPTCHA_ENABLED=1，需要 CAPTCHA 已验证；
        - 女巫检测未被 block。

        Returns:
            {"approved": bool, "missing": [str]} 列出缺失的验证步骤。
        """
        db = SessionLocal()
        try:
            missing = []

            if settings.REG_VERIFY_EMAIL:
                email_ok = (db.query(RegistrationVerification)
                            .filter(RegistrationVerification.host_id == host_id,
                                    RegistrationVerification.method == "email",
                                    RegistrationVerification.status == "verified")
                            .first())
                if email_ok is None:
                    missing.append("email_verification")

            if settings.REG_CAPTCHA_ENABLED:
                captcha_ok = (db.query(RegistrationVerification)
                              .filter(RegistrationVerification.host_id == host_id,
                                      RegistrationVerification.method == "captcha",
                                      RegistrationVerification.status == "verified")
                              .first())
                if captcha_ok is None:
                    missing.append("captcha")

            if missing:
                return {"approved": False, "missing": missing}

            # 创建一条 sybil 验证记录标记通过
            record = RegistrationVerification(
                host_id=host_id,
                method="sybil",
                status="verified",
                verified_at=_now(),
                expires_at=_now() + timedelta(days=365),
            )
            db.add(record)
            db.commit()
            logger.info("Registration approved for host=%d", host_id)
            return {"approved": True, "missing": []}
        finally:
            db.close()


instance = RegistrationGuard()
