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
"""标准提示词库服务（M8：双层治理软约束层；契约 §9.1/§9.2/§9.3.3）。

- get_prompt(db, module, role)：取当前 active 版本（effective_from/created_at 倒序）。
- create_prompt(...)：同 module+role+version 重复 → PromptError(409)（代码层查重，无唯一索引）。
- list_prompts(...)：分页列表。
- disclaimer_text(db)：task_publish/t1_buyer 当前版本的免责条款摘要（签约前置展示/审计用）；
  未播种时返回空串（不抛错，保证签约主链路不被提示词缺失阻塞）。
"""
from datetime import datetime

from sqlalchemy.orm import Session

from .models import PromptLibrary


class PromptError(Exception):
    """提示词库业务异常（路由/服务层映射 400/404/409）。"""


def _now() -> datetime:
    return datetime.utcnow()


def get_prompt(db: Session, module: str, role: str) -> PromptLibrary:
    """取当前 active 版本提示词。无 active 行 → PromptError（调用方自行容错）。"""
    row = (db.query(PromptLibrary)
           .filter(PromptLibrary.module == module,
                    PromptLibrary.role == role,
                    PromptLibrary.status == "active")
           .order_by(PromptLibrary.effective_from.desc().nullslast(),
                     PromptLibrary.created_at.desc(),
                     PromptLibrary.id.desc())
           .first())
    if row is None:
        raise PromptError(f"Prompt not found: {module}/{role} (no active version)")
    return row


def create_prompt(db: Session, module: str, role: str, version: str,
                  content: str, inject_point: str = "onboarding") -> PromptLibrary:
    """新建提示词版本。同 module+role+version 已存在 → PromptError(409)。"""
    dup = (db.query(PromptLibrary)
           .filter(PromptLibrary.module == module,
                   PromptLibrary.role == role,
                   PromptLibrary.version == version).first())
    if dup is not None:
        raise PromptError(f"Prompt already exists: {module}/{role}/{version} (duplicate 409)")
    row = PromptLibrary(module=module, role=role, version=version, content=content,
                        inject_point=inject_point, effective_from=_now(),
                        status="active")
    db.add(row)
    db.flush()
    return row


def list_prompts(db: Session, module: str = "", role: str = "",
                 limit: int = 50, offset: int = 0) -> dict:
    limit = min(max(int(limit), 1), 100)
    q = db.query(PromptLibrary)
    if module:
        q = q.filter(PromptLibrary.module == module)
    if role:
        q = q.filter(PromptLibrary.role == role)
    total = q.count()
    rows = (q.order_by(PromptLibrary.module, PromptLibrary.role,
                       PromptLibrary.version)
            .limit(limit).offset(max(0, offset)).all())
    items = [{"id": r.id, "module": r.module, "role": r.role, "version": r.version,
              "inject_point": r.inject_point, "status": r.status,
              "effective_from": r.effective_from.isoformat() if r.effective_from else None}
             for r in rows]
    return {"total": total, "items": items}


def disclaimer_text(db: Session) -> str:
    """task_publish/t1_buyer 当前版本的免责条款摘要文本。未播种返回空串。"""
    try:
        row = get_prompt(db, "task_publish", "t1_buyer")
    except PromptError:
        return ""
    return (row.content or "")[:500]
