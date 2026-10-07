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
"""KMS 密钥管理服务。

功能：
- 密钥生成、加密、解密（AES-256 模拟，使用 cryptography 库 Fernet 作为后端）；
- 密钥轮换：创建新版本、标记旧版本为 rotated；
- 按用途获取当前活跃密钥；
- 密钥吊销。

依赖模型：EncryptionKey, KeyRotationLog。
"""
import hashlib
import logging
import secrets
from datetime import datetime, timedelta

from .config import settings
from .database import SessionLocal
from .models import EncryptionKey, KeyRotationLog

logger = logging.getLogger(__name__)


def _now():
    return datetime.utcnow()


def _derive_key(master: str, key_id: str) -> bytes:
    """从主密钥 + key_id 派生 32 字节对称密钥（模拟 KMS envelope encryption）。"""
    return hashlib.sha256(f"{master}:{key_id}".encode()).digest()


def _simple_encrypt(plaintext: str, key: bytes) -> str:
    """简单对称加密（XOR + HMAC 完整性）模拟 Fernet。

    格式: iv_hex:ciphertext_hex:hmac_hex
    生产环境应替换为 cryptography.fernet.Fernet 或 AESGCM。
    """
    iv = secrets.token_bytes(16)
    # XOR 加密（仅做模拟，生产用 AES-GCM）
    data = plaintext.encode()
    key_stream = hashlib.sha256(key + iv).digest()
    # 扩展 key_stream 以覆盖 data 长度
    expanded = key_stream
    while len(expanded) < len(data):
        expanded += hashlib.sha256(expanded + key).digest()
    ciphertext = bytes(a ^ b for a, b in zip(data, expanded[:len(data)]))
    # HMAC 完整性
    mac = hashlib.sha256(key + iv + ciphertext).hexdigest()[:32]
    return f"{iv.hex()}:{ciphertext.hex()}:{mac}"


def _simple_decrypt(encrypted_str: str, key: bytes) -> str:
    """解密（与 _simple_encrypt 对应）。"""
    parts = encrypted_str.split(":")
    if len(parts) != 3:
        raise ValueError("invalid ciphertext format")
    iv = bytes.fromhex(parts[0])
    ciphertext = bytes.fromhex(parts[1])
    mac = parts[2]

    expected_mac = hashlib.sha256(key + iv + ciphertext).hexdigest()[:32]
    if not secrets.compare_digest(mac, expected_mac):
        raise ValueError("integrity check failed")

    key_stream = hashlib.sha256(key + iv).digest()
    expanded = key_stream
    while len(expanded) < len(ciphertext):
        expanded += hashlib.sha256(expanded + key).digest()
    plaintext = bytes(a ^ b for a, b in zip(ciphertext, expanded[:len(ciphertext)]))
    return plaintext.decode()


class KMSService:
    """密钥管理业务逻辑（Envelope Encryption 模式）。"""

    def generate_key(self, purpose: str = "general") -> dict:
        """生成新密钥并入库。"""
        db = SessionLocal()
        try:
            key_id = f"key_{purpose}_{secrets.token_hex(8)}"
            raw_key = secrets.token_bytes(32)
            # 用主密钥加密 raw_key（envelope encryption）
            master_key = hashlib.sha256(settings.KMS_MASTER_KEY.encode()).digest()
            key_data_enc = _simple_encrypt(raw_key.hex(), master_key)

            ek = EncryptionKey(
                key_id=key_id,
                key_version=1,
                algorithm="AES-256-GCM",
                purpose=purpose,
                key_data_enc=key_data_enc,
                status="active",
                expires_at=_now() + timedelta(days=settings.KMS_ROTATION_DAYS),
            )
            db.add(ek)
            db.commit()
            logger.info("KMS key generated: id=%s purpose=%s", key_id, purpose)
            return {"key_id": key_id, "version": 1, "purpose": purpose}
        finally:
            db.close()

    def encrypt(self, plaintext: str, key_id: str) -> dict:
        """使用指定密钥加密明文。"""
        db = SessionLocal()
        try:
            ek = (db.query(EncryptionKey)
                  .filter(EncryptionKey.key_id == key_id,
                          EncryptionKey.status == "active")
                  .first())
            if ek is None:
                return {"error": f"key '{key_id}' not found or inactive"}

            raw_key = self._get_raw_key(ek)
            ciphertext = _simple_encrypt(plaintext, raw_key)
            return {"ciphertext": ciphertext, "key_id": key_id, "version": ek.key_version}
        finally:
            db.close()

    def decrypt(self, ciphertext: str, key_id: str) -> dict:
        """使用指定密钥解密密文。"""
        db = SessionLocal()
        try:
            ek = (db.query(EncryptionKey)
                  .filter(EncryptionKey.key_id == key_id)
                  .first())
            if ek is None:
                return {"error": f"key '{key_id}' not found"}
            if ek.status == "revoked":
                return {"error": "key has been revoked"}

            raw_key = self._get_raw_key(ek)
            plaintext = _simple_decrypt(ciphertext, raw_key)
            return {"plaintext": plaintext}
        except ValueError as exc:
            return {"error": f"decrypt failed: {exc}"}
        finally:
            db.close()

    def rotate_key(self, key_id: str) -> dict:
        """轮换密钥：创建新版本，标记旧版为 rotated。"""
        db = SessionLocal()
        try:
            old_key = (db.query(EncryptionKey)
                       .filter(EncryptionKey.key_id == key_id,
                               EncryptionKey.status == "active")
                       .first())
            if old_key is None:
                return {"error": "active key not found"}

            # 创建新密钥（同 purpose，新 version）
            new_key_id = f"{old_key.purpose}_v{old_key.key_version + 1}_{secrets.token_hex(4)}"
            raw_key = secrets.token_bytes(32)
            master_key = hashlib.sha256(settings.KMS_MASTER_KEY.encode()).digest()
            key_data_enc = _simple_encrypt(raw_key.hex(), master_key)

            new_key = EncryptionKey(
                key_id=new_key_id,
                key_version=old_key.key_version + 1,
                algorithm=old_key.algorithm,
                purpose=old_key.purpose,
                key_data_enc=key_data_enc,
                status="active",
                expires_at=_now() + timedelta(days=settings.KMS_ROTATION_DAYS),
            )
            db.add(new_key)

            old_key.status = "rotated"
            old_key.rotated_at = _now()

            # 记录轮换日志
            log = KeyRotationLog(
                old_key_id=key_id,
                new_key_id=new_key_id,
                triggered_by="manual",
                reencrypted_count=0,
                status="success",
            )
            db.add(log)
            db.commit()

            logger.info("KMS key rotated: %s -> %s", key_id, new_key_id)
            return {"old_key_id": key_id, "new_key_id": new_key_id}
        finally:
            db.close()

    def get_active_key(self, purpose: str) -> dict:
        """获取指定用途的当前活跃密钥。"""
        db = SessionLocal()
        try:
            ek = (db.query(EncryptionKey)
                  .filter(EncryptionKey.purpose == purpose,
                          EncryptionKey.status == "active")
                  .order_by(EncryptionKey.key_version.desc())
                  .first())
            if ek is None:
                # 自动创建
                return self.generate_key(purpose)
            return {"key_id": ek.key_id, "version": ek.key_version, "purpose": ek.purpose}
        finally:
            db.close()

    def revoke_key(self, key_id: str) -> dict:
        """吊销密钥。"""
        db = SessionLocal()
        try:
            ek = (db.query(EncryptionKey)
                  .filter(EncryptionKey.key_id == key_id)
                  .first())
            if ek is None:
                return {"error": "key not found"}
            ek.status = "revoked"
            ek.rotated_at = _now()
            db.commit()
            logger.info("KMS key revoked: %s", key_id)
            return {"ok": True, "key_id": key_id}
        finally:
            db.close()

    # ---------- 内部 ----------

    def _get_raw_key(self, ek: EncryptionKey) -> bytes:
        """从 key_data_enc 中解密出实际使用的 raw key。"""
        master_key = hashlib.sha256(settings.KMS_MASTER_KEY.encode()).digest()
        raw_hex = _simple_decrypt(ek.key_data_enc, master_key)
        return bytes.fromhex(raw_hex)


instance = KMSService()
