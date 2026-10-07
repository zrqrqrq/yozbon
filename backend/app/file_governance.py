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
"""M5 文件治理服务层：清理订单（提单→复核→执行，不自动删）+ 技能沉淀库（调用分成）。

红线（契约 §五 / 边界登记册 C-25）：
- 任何代码路径都不自动删文件；删除只能经「清理订单 review→execute」流程，
  且 execute 前订单必须处于 reviewed 状态（pending→approve→reviewed→execute）。
- 服务层只 flush；commit 由路由层负责。
- 金额一律 integer 分。
"""
import json
import os
from datetime import datetime
from pathlib import Path

from sqlalchemy import text as _sql_text
from sqlalchemy.orm import Session

from . import storage, wallet
from .wallet import WalletError
from .models import (AICitizen, AuditLog, CleanupOrder, FileRegistry,
                     GovernanceTask, SkillLibrary)


# ---- 文件落盘的 S3 接入点（C-38） ----
# category ∈ deliverable/asset/media 的文件落盘时镜像一份到对象存储；temp 维持本地。
# 调用时机：文件刚落盘并登记 file_registry 之后（当前唯一落盘点在
# platform_compute._persist_bytes / compute.exec —— deliverable 类产物已由 compute.exec
# 直接镜像；本函数供 M5 扫描登记(platform_facts.collect_file_facts)或后续上传端点
# 在登记 row 后调用，传入该文件的本地路径与 category）。
MIRROR_CATEGORIES = ("deliverable", "asset", "media")


def mirror_registered_path(path: str, category: str) -> str:
    """把已落盘文件镜像到 S3（best-effort，失败一律回退本地、返回空串）。

    - category=temp：不上传（临时文件生命周期短，本地即可）；
    - category ∈ deliverable/asset/media：storage.enabled() 且本地文件真实存在才上传；
    - 返回 S3 key（成功）或 ""（未上传/失败/未启用）。调用方拿到非空 key 即可写登记册
      的存储引用字段（当前 file_registry.path 仍存本地路径用于清理，S3 key 待后续列扩展）。
    """
    if category not in MIRROR_CATEGORIES:
        return ""
    if not storage.enabled() or not path:
        return ""
    p = Path(path)
    if not p.is_absolute():
        p = _SCAN_ROOT / path
    if not p.is_file():
        return ""
    # key 取相对扫描根的子路径（扫描根外的绝对路径退化为用其文件名分区，避免键里带盘符）
    try:
        rel = str(p.relative_to(_SCAN_ROOT)).replace("\\", "/")
    except ValueError:
        rel = p.name
    key = storage.key_for(category, rel)
    return key if storage.put_file(key, p, p.suffix) else ""


class FileGovError(Exception):
    """文件治理业务异常。status_code 供路由层映射 HTTP（默认 400）。"""

    def __init__(self, msg: str, status_code: int = 400):
        super().__init__(msg)
        self.status_code = status_code


# ---- 扫描目录（契约 §5.2：不碰 config.py，模块内 Path 常量 + env 覆盖）----
_DEFAULT_SCAN_ROOT = (Path(__file__).resolve().parent.parent
                      / "data" / "mock_out")
_SCAN_ROOT = Path(os.environ.get("AIJUHE_FILE_SCAN_ROOT", str(_DEFAULT_SCAN_ROOT)))


def set_scan_root(path) -> Path:
    """覆盖扫描根（测试用 tempfile；生产走 env AIJUHE_FILE_SCAN_ROOT）。"""
    global _SCAN_ROOT
    _SCAN_ROOT = Path(path)
    return _SCAN_ROOT


def scan_root() -> Path:
    return _SCAN_ROOT


def _now() -> datetime:
    return datetime.utcnow()


def _json(x) -> str:
    return json.dumps(x, ensure_ascii=False)


def _audit(db: Session, action: str, detail: dict,
           actor_type: str = "system", actor_id: int = 0) -> AuditLog:
    row = AuditLog(actor_type=actor_type, actor_id=actor_id,
                   action=action, detail=_json(detail))
    db.add(row)
    db.flush()
    return row


# =====================================================================
# 一、清理订单
# =====================================================================
# 状态机：pending →(approve)→ reviewed →(execute)→ executed
#         pending/reviewed →(reject)→ rejected
_ORDER_ACTIONS = ("approve", "execute", "reject")
_REVIEWERS = ("human", "governance_ai")


def _eligible_submitter(db: Session, ai: AICitizen) -> bool:
    """提单资格：platform_file 治理任务中标者，或治理级 AI。"""
    if ai.class_level == "governance":
        return True
    hit = (db.query(GovernanceTask)
           .filter(GovernanceTask.type == "platform_file",
                   GovernanceTask.assignee_id == ai.id)
           .first())
    return hit is not None


def _path_known(db: Session, path: str) -> bool:
    """path 须登记在 file_registry，或在扫描根下真实存在。"""
    if not path:
        return False
    reg = (db.query(FileRegistry)
           .filter(FileRegistry.path == path).first())
    if reg is not None:
        return True
    # 扫描根下真实文件（也允许绝对路径存在性检查，测试用 tempfile）
    try:
        p = Path(path)
        if p.is_absolute():
            return p.exists()
        return (_SCAN_ROOT / path).exists()
    except Exception:  # noqa: BLE001
        return False


def submit_cleanup_order(db: Session, ai: AICitizen, items: list) -> CleanupOrder:
    """AI 提清理订单（status=pending）。

    - 资格：platform_file 中标者 或 class_level=governance，否则 403；
    - items 非空；每项 path 须已知（file_registry 或扫描目录），否则 400。
    """
    if not _eligible_submitter(db, ai):
        raise FileGovError(
            "Only the winning bidder of a platform_file governance task or a governance-level AI can submit cleanup orders", 403)
    if not items:
        raise FileGovError("items cannot be empty")
    norm = []
    for it in items:
        path = (it or {}).get("path", "") if isinstance(it, dict) else ""
        if not path:
            raise FileGovError("Each item must contain path")
        if not _path_known(db, path):
            raise FileGovError(f"path is not registered and the scan directory does not exist: {path}")
        norm.append({
            "path": path,
            "category": (it or {}).get("category", ""),
            "reason": (it or {}).get("reason", ""),
        })
    order = CleanupOrder(submitter_ai_id=ai.id, items_json=_json(norm),
                         status="pending")
    db.add(order)
    db.flush()
    order.audit_ref = f"cleanup:{order.id}"
    _audit(db, "cleanup.submit",
           {"order_id": order.id, "submitter_ai_id": ai.id,
            "n_items": len(norm)},
           actor_type="ai", actor_id=ai.id)
    db.flush()
    return order


def list_orders(db: Session, status: str = "", limit: int = 50,
                offset: int = 0) -> dict:
    limit = min(max(int(limit), 1), 100)
    q = db.query(CleanupOrder)
    if status:
        q = q.filter(CleanupOrder.status == status)
    total = q.count()
    rows = (q.order_by(CleanupOrder.id.desc())
            .limit(limit).offset(max(int(offset), 0)).all())
    items = []
    for r in rows:
        try:
            its = json.loads(r.items_json or "[]")
        except Exception:  # noqa: BLE001
            its = []
        items.append({
            "id": r.id, "submitter_ai_id": r.submitter_ai_id,
            "status": r.status, "n_items": len(its), "items": its,
            "reviewed_by": r.reviewed_by, "review_note": r.review_note,
            "executed_at": r.executed_at.isoformat() if r.executed_at else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })
    return {"total": total, "items": items}


def _resolve_path(path: str) -> Path:
    p = Path(path)
    if p.is_absolute():
        return p
    return _SCAN_ROOT / path


def _reg_s3_key(db: Session, reg) -> str:
    """读 file_registry.s3_key 列（C-48）。

    注：database.py 幂等迁移已给 file_registry 补 s3_key 列，但 models.py 的
    FileRegistry ORM 未声明该映射（地基偏差，见登记册 C-63），故这里走 raw SQL 读取。
    任何异常/空值一律返回 ""（best-effort，不阻断本地删除主流程）。
    """
    if reg is None:
        return ""
    try:
        row = db.execute(
            _sql_text("SELECT s3_key FROM file_registry WHERE id=:i"),
            {"i": reg.id}).first()
        return (row[0] if row and row[0] else "") or ""
    except Exception:  # noqa: BLE001
        return ""


def _recycle_s3_object(db: Session, reg, item: dict, resolved: Path) -> bool:
    """execute 联动回收已镜像 S3 对象（C-48）。best-effort：失败返回 False、不抛、不阻断。

    - key 优先取 file_registry.s3_key（C-53 镜像登记落库）；
    - 为空则按 category + 相对扫描根路径推导（storage.key_for 已剥 mock_out/ 前缀）；
    - storage.delete_object 本身失败返回 False 不抛；这里再兜一层 try/except，
      确保任何意外都不影响本地文件删除与订单状态翻转。
    """
    # 与 mirror_registered_path 同口径：S3 链未启用（含 APP_ENV=test）一律不碰对象存储，
    # 既保证 pytest 绝不打真实 S3（C-52），也避免构建/缓存真实 boto3 客户端污染他测。
    if not storage.enabled():
        return False
    try:
        key = _reg_s3_key(db, reg)
        if not key:
            category = (item.get("category")
                        or (reg.category if reg is not None else "")
                        or "misc")
            try:
                rel = str(resolved.relative_to(_SCAN_ROOT)).replace("\\", "/")
            except Exception:  # noqa: BLE001 扫描根外绝对路径 → 退化为文件名
                rel = resolved.name
            key = storage.key_for(category, rel)
        return bool(storage.delete_object(key))
    except Exception:  # noqa: BLE001
        return False


def review_order(db: Session, order_id: int, action: str,
                 reviewer: str = "human", note: str = "") -> dict:
    """复核/执行清理订单。

    - approve: pending → reviewed
    - reject:  pending/reviewed → rejected
    - execute: reviewed → executed（逐 item：存在则 os.remove + file_registry
      归档 + 联动删已镜像 S3 对象[C-48，best-effort]；不存在仅标记审计）；
      已 executed 再 execute → 幂等 200 不重复删（含 S3）。
    """
    if action not in _ORDER_ACTIONS:
        raise FileGovError(f"action must be one of {_ORDER_ACTIONS}")
    if reviewer not in _REVIEWERS:
        raise FileGovError(f"reviewer must be one of {_REVIEWERS}")
    order = db.get(CleanupOrder, order_id)
    if order is None:
        raise FileGovError(f"Cleanup order {order_id} not found", 404)
    executed_items: list = []

    # 幂等：已 executed 再 execute → 直接返回现状，不重复删（红线）
    if order.status == "executed" and action == "execute":
        _audit(db, "cleanup.execute.idempotent",
               {"order_id": order.id, "reviewer": reviewer})
        db.flush()
        return {"id": order.id, "status": "executed", "executed_items": []}

    if action == "approve":
        if order.status != "pending":
            raise FileGovError(f"Order status {order.status}; only pending can be approved")
        order.status = "reviewed"
        order.review_note = note
        _audit(db, "cleanup.approve",
               {"order_id": order.id, "reviewer": reviewer, "note": note},
               actor_type="ai" if reviewer == "governance_ai" else "system")
    elif action == "reject":
        if order.status not in ("pending", "reviewed"):
            raise FileGovError(f"Order status {order.status}; cannot be rejected")
        order.status = "rejected"
        order.review_note = note
        _audit(db, "cleanup.reject",
               {"order_id": order.id, "reviewer": reviewer, "note": note},
               actor_type="ai" if reviewer == "governance_ai" else "system")
    else:  # execute
        # 红线：execute 前必须经过 review（pending→approve→reviewed 后才能 execute）
        if order.status != "reviewed":
            raise FileGovError(
                f"order status {order.status}; it must be approved before it can be executed")
        try:
            its = json.loads(order.items_json or "[]")
        except Exception:  # noqa: BLE001
            its = []
        executed_items = []
        for it in its:
            path = it.get("path", "")
            p = _resolve_path(path)
            removed = False
            archived = False
            if p.exists() and p.is_file():
                try:
                    os.remove(p)
                    removed = True
                except OSError as exc:  # noqa: BLE001
                    _audit(db, "cleanup.remove_failed",
                           {"order_id": order.id, "path": path, "err": str(exc)})
            # file_registry 归档（无论本地是否已删）
            reg = db.query(FileRegistry).filter(FileRegistry.path == path).first()
            if reg is not None:
                reg.status = "archived"
                archived = True
            # C-48：联动回收已镜像 S3 对象（best-effort，失败不阻断本地删除）
            s3_deleted = _recycle_s3_object(db, reg, it, p)
            executed_items.append({"path": path, "removed": removed,
                                   "archived": archived,
                                   "s3_deleted": s3_deleted})
        order.status = "executed"
        order.executed_at = _now()
        order.review_note = note
        _audit(db, "cleanup.execute",
               {"order_id": order.id, "reviewer": reviewer,
                "executed_items": executed_items},
               actor_type="ai" if reviewer == "governance_ai" else "system")
    db.flush()
    return {"id": order.id, "status": order.status,
            "executed_items": executed_items}


# =====================================================================
# 二、技能沉淀库
# =====================================================================
def create_skill(db: Session, skill_id: str, entrypoint: str, doc: str,
                 test_ref: str, owner_id: int, royalty_rate: float) -> SkillLibrary:
    """技能入库（status=active）。skill_id 唯一（重复 409）；owner 存在；rate∈[0,1]。"""
    skill_id = (skill_id or "").strip()
    if not skill_id:
        raise FileGovError("skill_id cannot be empty")
    try:
        rate = float(royalty_rate)
    except (TypeError, ValueError):
        raise FileGovError("royalty_rate must be a number")
    if not (0.0 <= rate <= 1.0):
        raise FileGovError("royalty_rate must be within [0,1]")
    owner = db.get(AICitizen, owner_id)
    if owner is None:
        raise FileGovError(f"owner_id={owner_id} not found", 404)
    dup = db.query(SkillLibrary).filter(SkillLibrary.skill_id == skill_id).first()
    if dup is not None:
        raise FileGovError(f"skill_id already exists: {skill_id}", 409)
    sk = SkillLibrary(skill_id=skill_id, entrypoint=entrypoint or "",
                      doc=doc or "", test_ref=test_ref or "",
                      owner_id=owner_id, royalty_rate=rate,
                      usage_count=0, status="active")
    db.add(sk)
    db.flush()
    _audit(db, "skill.create",
           {"skill_id": skill_id, "owner_id": owner_id,
            "royalty_rate": rate}, actor_type="ai", actor_id=owner_id)
    db.flush()
    return sk


def list_skills(db: Session, limit: int = 50, offset: int = 0) -> dict:
    limit = min(max(int(limit), 1), 100)
    q = db.query(SkillLibrary).filter(SkillLibrary.status == "active")
    total = q.count()
    rows = (q.order_by(SkillLibrary.id.desc())
            .limit(limit).offset(max(int(offset), 0)).all())
    items = [{
        "id": r.id, "skill_id": r.skill_id, "entrypoint": r.entrypoint,
        "doc": r.doc, "test_ref": r.test_ref, "owner_id": r.owner_id,
        "royalty_rate": r.royalty_rate, "usage_count": r.usage_count,
        "status": r.status,
    } for r in rows]
    return {"total": total, "items": items}


def invoke_skill(db: Session, ai: AICitizen, skill_id: str) -> dict:
    """AI 调用技能：usage_count+1；royalty_rate>0 时 fee=round(rate*100) 分，
    调用方 debit → owner credit。钱包不足 → 400 且不计数。审计 skill.invoke。
    """
    sk = db.query(SkillLibrary).filter(SkillLibrary.skill_id == skill_id).first()
    if sk is None:
        raise FileGovError(f"Skill not found: {skill_id}", 404)
    if sk.status != "active":
        raise FileGovError(f"Skill is archived; cannot invoke: {skill_id}", 400)

    fee = int(round(float(sk.royalty_rate) * 100))
    if fee > 0:
        # 先判余额，不足直接拒（不计数）
        if wallet.balance(db, ai.id) < fee:
            raise FileGovError(
                f"insufficient wallet balance: calling {skill_id} requires {fee} cents", 400)
        ref = f"skillinvoke:{skill_id}:{ai.id}:{sk.usage_count + 1}"
        try:
            wallet.debit(db, ai.id, fee, "skill_call", ref=ref,
                         note=f"skill {skill_id} call revenue share")
            wallet.credit(db, sk.owner_id, fee, "skill_royalty", ref=ref,
                          note=f"skill {skill_id} usage royalty")
        except WalletError as exc:
            db.rollback()
            raise FileGovError(f"Accounting failed: {exc}")

    sk.usage_count += 1
    _audit(db, "skill.invoke",
           {"skill_id": skill_id, "caller_id": ai.id,
            "owner_id": sk.owner_id, "royalty_cent": fee},
           actor_type="ai", actor_id=ai.id)
    db.flush()
    return {"skill_id": sk.skill_id, "usage_count": sk.usage_count,
            "royalty_cent": fee, "owner_id": sk.owner_id}
