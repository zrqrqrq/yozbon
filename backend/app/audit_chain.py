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
"""防篡改哈希链审计日志服务。

每区块的 prev_hash 引用前序区块的 payload_hash，形成链式结构。
任何历史区块被修改后，verify_chain 即刻发现断裂。
"""
import hashlib
import json
from datetime import datetime

from sqlalchemy.orm import Session

from .models import AuditChainBlock


def _now() -> datetime:
    return datetime.utcnow()


class AuditChainError(Exception):
    """审计链业务异常。路由层映射为 HTTP 400。"""


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _compute_payload_hash(
    seq: int,
    action: str,
    actor_id: int,
    detail_json: str,
    created_at: datetime,
) -> str:
    """计算区块内容哈希: SHA256(seq:action:actor_id:detail_json:created_at_iso)。"""
    raw = f"{seq}:{action}:{actor_id}:{detail_json}:{created_at.isoformat()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 追加区块
# ---------------------------------------------------------------------------

def append_audit(
    db: Session,
    action: str,
    actor_id: int,
    detail: dict | str = "",
) -> AuditChainBlock:
    """向审计链追加一个新区块。

    自动计算 prev_hash（引用上一区块的 payload_hash）和本区块 payload_hash。
    """
    # 获取链尾
    last_block: AuditChainBlock | None = (
        db.query(AuditChainBlock)
        .order_by(AuditChainBlock.seq.desc())
        .first()
    )
    prev_hash = last_block.payload_hash if last_block else ""
    next_seq = (last_block.seq + 1) if last_block else 1

    # 序列化 detail
    if isinstance(detail, dict):
        detail_json = json.dumps(detail, ensure_ascii=False, separators=(",", ":"))
    else:
        detail_json = str(detail) if detail else "{}"

    created_at = _now()
    payload_hash = _compute_payload_hash(next_seq, action, actor_id, detail_json, created_at)

    block = AuditChainBlock(
        seq=next_seq,
        prev_hash=prev_hash,
        payload_hash=payload_hash,
        action=action,
        actor_id=actor_id,
        detail_json=detail_json,
        created_at=created_at,
    )
    db.add(block)
    db.flush()
    return block


# ---------------------------------------------------------------------------
# 链完整性验证
# ---------------------------------------------------------------------------

def verify_chain(db: Session) -> dict:
    """验证整条审计链的完整性。

    逐区块检查：
    1. prev_hash 是否等于前序区块的 payload_hash
    2. payload_hash 是否等于根据区块内容重新计算的哈希

    返回: {"valid": bool, "blocks_checked": int, "first_invalid_seq": int | None}
    """
    blocks = (
        db.query(AuditChainBlock)
        .order_by(AuditChainBlock.seq.asc())
        .all()
    )

    if not blocks:
        return {"valid": True, "blocks_checked": 0, "first_invalid_seq": None}

    expected_prev_hash = ""

    for block in blocks:
        # 校验 prev_hash 链接
        if block.prev_hash != expected_prev_hash:
            return {
                "valid": False,
                "blocks_checked": block.seq,
                "first_invalid_seq": block.seq,
            }

        # 重新计算 payload_hash
        recomputed = _compute_payload_hash(
            block.seq,
            block.action,
            block.actor_id,
            block.detail_json,
            block.created_at,
        )
        if block.payload_hash != recomputed:
            return {
                "valid": False,
                "blocks_checked": block.seq,
                "first_invalid_seq": block.seq,
            }

        expected_prev_hash = block.payload_hash

    return {"valid": True, "blocks_checked": len(blocks), "first_invalid_seq": None}


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def chain_length(db: Session) -> int:
    """返回审计链总区块数。"""
    return db.query(AuditChainBlock).count()


def get_audit_trail(
    db: Session,
    action: str = "",
    actor_id: int = 0,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """按条件查询审计记录（按 seq 倒序）。"""
    limit = min(max(int(limit), 1), 200)
    q = db.query(AuditChainBlock)

    if action:
        q = q.filter(AuditChainBlock.action == action)
    if actor_id:
        q = q.filter(AuditChainBlock.actor_id == actor_id)

    rows = (
        q.order_by(AuditChainBlock.seq.desc())
        .offset(max(int(offset), 0))
        .limit(limit)
        .all()
    )
    return [
        {
            "seq": r.seq,
            "prev_hash": r.prev_hash,
            "payload_hash": r.payload_hash,
            "action": r.action,
            "actor_id": r.actor_id,
            "detail_json": r.detail_json,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
