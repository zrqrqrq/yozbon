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
"""G8 测试：把孤儿模块 ml_moderation 接入内容主链（任务风控 screen_task）。

覆盖：
  - moderation.augment_ml 软升级语义：
      * 规则层 BLOCK 时直接沿用，不调模型（不写 ModerationScore）；
      * 模型 decision=block/review 且规则层 OK 时，最多升级为 FLAG，绝不 BLOCK；
      * 模型失败被吞，返回 base_level（不影响主链放行）；
  - screen_task 对放行文本调用 ML 增强并落 ModerationScore 流水；
  - screen_task 命中规则 BLOCK 时不写 ModerationScore（沿用规则结论）。
"""
import pytest

from app import moderation
from app.database import SessionLocal
from app.models import AICitizen, ModerationScore
from app.task_orchestrator import screen_task


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _citizen(db, uid):
    c = AICitizen(host_id=1, ai_uid=uid, name=uid,
                  status="active", occupation="general")
    db.add(c)
    db.flush()
    return c


# ---------------- augment_ml 软升级语义 ----------------

def test_augment_ml_block_base_skips_model():
    """规则层已 BLOCK：直接沿用，不调模型，不写 ModerationScore。"""
    db = SessionLocal()
    try:
        before = db.query(ModerationScore).count()
        level, decision = moderation.augment_ml(
            "anything", base_level=moderation.LEVEL_BLOCK)
        db.expire_all()
        after = db.query(ModerationScore).count()
    finally:
        db.close()
    assert level == moderation.LEVEL_BLOCK
    assert decision == "skipped"
    assert after == before


def test_augment_ml_never_hard_blocks(monkeypatch):
    """模型 decision=block：规则层 OK 时最多升级 FLAG，绝不 BLOCK。"""
    from app.ml_moderation import instance as ml

    monkeypatch.setattr(ml, "_decide", lambda scores: "block", raising=True)
    level, decision = moderation.augment_ml(
        "some text that the fake model flags", content_type="task", citizen_id=7)
    assert decision == "block"           # 模型判定为 block
    assert level == moderation.LEVEL_FLAG  # 但软升级只到 FLAG，硬拦被吞


def test_augment_ml_error_is_swallowed(monkeypatch):
    """模型抛异常被吞：返回 base_level，不影响放行。"""
    from app.ml_moderation import instance as ml

    def _boom(*a, **k):
        raise RuntimeError("model exploded")

    monkeypatch.setattr(ml, "score_content", _boom, raising=True)
    level, decision = moderation.augment_ml(
        "text", base_level=moderation.LEVEL_OK)
    assert level == moderation.LEVEL_OK
    assert decision == "error"


# ---------------- screen_task 接入主链 ----------------

def test_screen_task_populates_moderation_score(db):
    """放行的正常任务文本：screen_task 仍放行，且落一条 ModerationScore。"""
    c = _citizen(db, "f8_ok")
    db.query(ModerationScore).delete()
    db.commit()
    res = screen_task(db, c, "生成一段产品描述文案，介绍一款保温杯")
    assert res["blocked"] is False
    # ML 增强已写入流水
    rows = (db.query(ModerationScore)
            .filter(ModerationScore.content_type == "task")
            .filter(ModerationScore.citizen_id == c.id)
            .all())
    assert len(rows) >= 1


def test_screen_task_blocked_by_rule_skips_ml(db):
    """规则层 BLOCK（外链命中）：直接拒单，不写 ModerationScore。"""
    c = _citizen(db, "f8_block")
    db.query(ModerationScore).delete()
    db.commit()
    res = screen_task(db, c, "请处理 https://spam.example.com 这段")
    assert res["blocked"] is True
    assert res["rule"] == "moderation"
    rows = (db.query(ModerationScore)
            .filter(ModerationScore.citizen_id == c.id)
            .count())
    assert rows == 0
