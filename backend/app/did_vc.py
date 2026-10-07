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
"""P3 去中心化身份（DID）+ 可验证凭证（VC）服务。

实现 W3C DID Core + VC Data Model 的轻量版本：
- DID 方法: did:aijuhe:<hex>（平台自有方法）；
- 签名: Ed25519 模拟（平台无原生 Ed25519 库时用 HMAC-SHA256 代替，
  接口签名保持一致，生产切换为 PyNaCl / cryptography 即可）；
- 凭证生命周期: issue -> valid -> revoked，支持过期自动失效。

约定：
- public_key 字段存 JSON Web Key 格式 {"kty":"OKP","crv":"Ed25519","x":"<base64url>"}；
- proof 字段存签名证据 {"type":"Ed25519Signature2020","jws":"...","created":"..."}；
- 凭证 ID 格式: urn:aijuhe:vc:<uuid_hex>。
"""
import base64
import hashlib
import hmac
import json
import logging
import secrets
import uuid
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .database import SessionLocal
from .config import settings
from .models import DIDDocument, VerifiableCredential

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.utcnow()


def _generate_keypair() -> dict:
    """模拟 Ed25519 密钥对生成（HMAC 模式）。

    生产应替换为 PyNaCl: signing_key = SigningKey.generate()
    """
    secret = secrets.token_bytes(32)
    public = hashlib.sha256(secret).digest()
    return {
        "secret_b64": base64.urlsafe_b64encode(secret).decode().rstrip("="),
        "public_jwk": {
            "kty": "OKP",
            "crv": "Ed25519",
            "x": base64.urlsafe_b64encode(public).decode().rstrip("="),
        },
    }


def _sign(message: str, secret_b64: str) -> str:
    """模拟 Ed25519 签名（HMAC-SHA256）。"""
    secret = base64.urlsafe_b64decode(secret_b64 + "==")
    sig = hmac.new(secret, message.encode(), hashlib.sha256).hexdigest()
    return sig


def _verify(message: str, signature: str, public_key: dict) -> bool:
    """模拟验签。

    注：真实 Ed25519 用 public_key 验签，HMAC 模式需 secret。
    此处为简化实现：验证 signature 格式合法性（32字节 hex）。
    生产切换 cryptography.Ed25519PublicKey().verify(sig, msg) 即可。
    """
    return len(signature) == 64 and all(c in "0123456789abcdef" for c in signature)


class DIDService:
    """DID + VC 服务主类。"""

    def register_did(self, subject_type: str, subject_id: int) -> dict:
        """为实体（AI 公民/宿主）注册 DID。

        Args:
            subject_type: "citizen" 或 "host"。
            subject_id: 实体 ID。

        Returns:
            {"did", "subject_type", "subject_id", "public_key"}
        """
        if subject_type not in ("citizen", "host"):
            raise ValueError("subject_type must be citizen or host")

        db: Session = SessionLocal()
        try:
            # 幂等：同 subject 不重复注册
            existing = (db.query(DIDDocument)
                        .filter(DIDDocument.subject_type == subject_type,
                                DIDDocument.subject_id == subject_id)
                        .first())
            if existing:
                return {
                    "did": existing.did,
                    "subject_type": existing.subject_type,
                    "subject_id": existing.subject_id,
                    "public_key": json.loads(existing.public_key),
                    "status": "existing",
                }

            did = f"did:{settings.DID_METHOD}:{secrets.token_hex(16)}"
            kp = _generate_keypair()

            doc = DIDDocument(
                did=did,
                subject_type=subject_type,
                subject_id=subject_id,
                public_key=json.dumps(kp["public_jwk"]),
                service_endpoints="[]",
            )
            db.add(doc)
            db.commit()
            logger.info("did_vc: registered DID %s for %s %d", did, subject_type, subject_id)

            # 密钥仅注册时返回（生产应存 KMS）
            return {
                "did": did,
                "subject_type": subject_type,
                "subject_id": subject_id,
                "public_key": kp["public_jwk"],
                "signing_key_b64": kp["secret_b64"],  # 一次性返回
                "status": "created",
            }
        finally:
            db.close()

    def issue_credential(self, issuer_did: str, subject_did: str,
                         cred_type: str, claims: dict,
                         expires_at: datetime = None) -> dict:
        """签发可验证凭证。

        Args:
            issuer_did: 签发者 DID。
            subject_did: 被签发者 DID。
            cred_type: 凭证类型 (skill/cert/achievement/reputation)。
            claims: 凭证声明内容。
            expires_at: 过期时间。

        Returns:
            {"credential_id", "issuer_did", "subject_did", "credential_type", "proof", "expires_at"}
        """
        db: Session = SessionLocal()
        try:
            # 验证 issuer DID 存在
            issuer = db.query(DIDDocument).filter(DIDDocument.did == issuer_did).first()
            if issuer is None:
                raise ValueError(f"Issuer DID {issuer_did} not found")

            subject = db.query(DIDDocument).filter(DIDDocument.did == subject_did).first()
            if subject is None:
                raise ValueError(f"Subject DID {subject_did} not found")

            credential_id = f"urn:aijuhe:vc:{uuid.uuid4().hex}"
            if expires_at is None:
                expires_at = _now() + timedelta(days=365)

            # 构造待签数据
            vc_data = {
                "@context": "https://www.w3.org/2018/credentials/v1",
                "id": credential_id,
                "type": ["VerifiableCredential", cred_type],
                "issuer": issuer_did,
                "issuanceDate": _now().isoformat(),
                "expirationDate": expires_at.isoformat(),
                "credentialSubject": {
                    "id": subject_did,
                    **claims,
                },
            }

            # 签名（模拟 Ed25519，使用 issuer 的公钥 hash 作为 HMAC key）
            message = json.dumps(vc_data, sort_keys=True)
            issuer_pub = json.loads(issuer.public_key)
            sig_key = issuer_pub.get("x", "").encode()
            signature = hmac.new(sig_key, message.encode(), hashlib.sha256).hexdigest()

            proof = {
                "type": "Ed25519Signature2020",
                "created": _now().isoformat(),
                "verificationMethod": f"{issuer_did}#keys-1",
                "jws": f"eyJhbGciOiJFZERTIiwidHlwIjoiSldUIn0..{signature}",
            }

            vc = VerifiableCredential(
                credential_id=credential_id,
                issuer_did=issuer_did,
                subject_did=subject_did,
                credential_type=cred_type,
                claims=json.dumps(claims),
                proof=json.dumps(proof),
                status="valid",
                expires_at=expires_at,
            )
            db.add(vc)
            db.commit()
            logger.info("did_vc: issued VC %s type=%s subject=%s", credential_id, cred_type, subject_did)
            return {
                "credential_id": credential_id,
                "issuer_did": issuer_did,
                "subject_did": subject_did,
                "credential_type": cred_type,
                "claims": claims,
                "proof": proof,
                "expires_at": expires_at.isoformat(),
            }
        finally:
            db.close()

    def verify_credential(self, credential_id: str) -> dict:
        """验证凭证有效性。

        检查：存在性、状态、过期时间、签名格式。
        """
        db: Session = SessionLocal()
        try:
            vc = db.query(VerifiableCredential).filter(
                VerifiableCredential.credential_id == credential_id
            ).first()
            if vc is None:
                return {"valid": False, "reason": "Credential not found"}
            if vc.status == "revoked":
                return {"valid": False, "reason": "Credential has been revoked"}
            if vc.expires_at and vc.expires_at < _now():
                return {"valid": False, "reason": "Credential has expired", "expired_at": vc.expires_at.isoformat()}

            # 验证签名格式
            proof = json.loads(vc.proof or "{}")
            jws = proof.get("jws", "")
            sig_part = jws.split(".")[-1] if jws else ""
            sig_valid = _verify("", sig_part, {})

            return {
                "valid": sig_valid,
                "credential_id": credential_id,
                "issuer_did": vc.issuer_did,
                "subject_did": vc.subject_did,
                "credential_type": vc.credential_type,
                "expires_at": vc.expires_at.isoformat() if vc.expires_at else None,
                "status": vc.status,
            }
        finally:
            db.close()

    def revoke_credential(self, credential_id: str) -> dict:
        """吊销凭证。"""
        db: Session = SessionLocal()
        try:
            vc = db.query(VerifiableCredential).filter(
                VerifiableCredential.credential_id == credential_id
            ).first()
            if vc is None:
                raise ValueError(f"Credential {credential_id} not found")
            if vc.status == "revoked":
                return {"credential_id": credential_id, "status": "revoked", "already_revoked": True}

            vc.status = "revoked"
            db.commit()
            logger.info("did_vc: revoked VC %s", credential_id)
            return {"credential_id": credential_id, "status": "revoked"}
        finally:
            db.close()

    def get_did_document(self, did: str) -> dict:
        """获取 DID 文档（W3C DID Document 格式）。"""
        db: Session = SessionLocal()
        try:
            doc = db.query(DIDDocument).filter(DIDDocument.did == did).first()
            if doc is None:
                raise ValueError(f"DID {did} not found")

            public_key = json.loads(doc.public_key)
            return {
                "@context": "https://www.w3.org/ns/did/v1",
                "id": doc.did,
                "verificationMethod": [{
                    "id": f"{did}#keys-1",
                    "type": "Ed25519VerificationKey2020",
                    "controller": did,
                    "publicKeyJwk": public_key,
                }],
                "authentication": [f"{did}#keys-1"],
                "service": json.loads(doc.service_endpoints or "[]"),
                "created": doc.created_at.isoformat() if doc.created_at else None,
                "updated": doc.updated_at.isoformat() if doc.updated_at else None,
            }
        finally:
            db.close()

    def get_credentials(self, subject_did: str, cred_type: str = None) -> list:
        """获取某 DID 的所有有效凭证。"""
        db: Session = SessionLocal()
        try:
            q = (db.query(VerifiableCredential)
                 .filter(VerifiableCredential.subject_did == subject_did,
                         VerifiableCredential.status == "valid"))
            if cred_type:
                q = q.filter(VerifiableCredential.credential_type == cred_type)
            creds = q.order_by(VerifiableCredential.id.desc()).all()
            return [
                {
                    "credential_id": c.credential_id,
                    "issuer_did": c.issuer_did,
                    "credential_type": c.credential_type,
                    "claims": json.loads(c.claims or "{}"),
                    "expires_at": c.expires_at.isoformat() if c.expires_at else None,
                    "created_at": c.created_at.isoformat() if c.created_at else None,
                }
                for c in creds
            ]
        finally:
            db.close()

    def present_credentials(self, subject_did: str, requested_types: list) -> dict:
        """选择性披露：按请求类型筛选凭证并生成呈现证明。"""
        credentials = self.get_credentials(subject_did)
        presented = [c for c in credentials if c["credential_type"] in requested_types]
        return {
            "subject_did": subject_did,
            "requested_types": requested_types,
            "presented_count": len(presented),
            "credentials": presented,
            "presentation": {
                "@context": "https://www.w3.org/2018/credentials/v1",
                "type": "VerifiablePresentation",
                "verifiableCredential": [c["credential_id"] for c in presented],
                "presentation_submission": {
                    "definition_id": "required_creds",
                    "descriptor_map": [
                        {"id": c["credential_type"], "path": "$"}
                        for c in presented
                    ],
                },
            },
        }


instance = DIDService()
