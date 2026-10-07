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
"""S10 验证：retainer failover 无备份时自动创建 GovernanceTask(type="recruit")。

覆盖：
- 主 AI 掉线 + 无备份 → failover_contract 返回 recruit_task_created=True；
- 数据库中确实存在 type="recruit" status="open" 的 GovernanceTask；
- params 包含 post_code / contract_id / reason 等关键字段；
- 有可用备份时正常顶替、不创建招聘任务。
"""
import sys
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from conftest import new_host, new_ai
from app.database import SessionLocal
from app.models import AICitizen, GovernanceTask, RetainerContract
from app import retainer


def _make_active_ai(client, host, name="在编AI") -> dict:
    """创建 AI 并设为 active 状态（sign_contract 要求 active）。"""
    data = new_ai(client, host["token"], name=name)
    db = SessionLocal()
    ai = db.get(AICitizen, data["id"])
    ai.status = "active"
    db.commit()
    db.close()
    return data


def _set_ai_dead(ai_id: int):
    """将 AI 状态改为 dead（模拟签约后掉线）。"""
    db = SessionLocal()
    ai = db.get(AICitizen, ai_id)
    ai.status = "dead"
    db.commit()
    db.close()


class TestS10FailoverRecruit:
    """S10：failover 无备份时创建招聘任务。"""

    def test_no_backup_creates_recruit_task(self, client):
        """主 AI 掉线 + 无备份 → 自动创建 GovernanceTask(type=recruit, status=open)。"""
        h = new_host(client)
        # 先创建 active AI 以便签约通过校验
        primary = _make_active_ai(client, h, name="已掉线")

        db = SessionLocal()
        try:
            # 签订长约：primary active、无备份
            c = retainer.sign_contract(
                db, post_code="s10_test_post", title="S10测试岗",
                primary_ai_id=primary["id"], backup_ai_id=0,
                is_key_post=True,
            )
            db.commit()
        finally:
            db.close()

        # 签约后将主 AI 标记为 dead，模拟掉线
        _set_ai_dead(primary["id"])

        db = SessionLocal()
        try:
            # 执行顶替检测（primary dead 或 stale 都会触发 primary_down）
            result = retainer.failover_contract(db, c.id)
            db.commit()

            # 验证返回标记
            assert result is not None
            assert result["failover"] is False
            assert result["reason"] == "no_available_backup"
            assert result.get("recruit_task_created") is True, (
                "应标记 recruit_task_created=True")

            # 验证 GovernanceTask 确实入库
            task = (db.query(GovernanceTask)
                    .filter_by(type="recruit", status="open")
                    .order_by(GovernanceTask.id.desc())
                    .first())
            assert task is not None, "未创建招聘 GovernanceTask"
            params = json.loads(task.params)
            assert params["post_code"] == "s10_test_post"
            assert params["contract_id"] == c.id
            assert params["reason"] == "failover_no_available_backup"
        finally:
            db.close()

    def test_with_backup_no_recruit_task(self, client):
        """有可用备份时正常顶替，不创建招聘任务。"""
        h = new_host(client)
        primary = _make_active_ai(client, h, name="掉线主")
        backup = _make_active_ai(client, h, name="备份")

        db = SessionLocal()
        try:
            c = retainer.sign_contract(
                db, post_code="s10_backup_post", title="备份测试岗",
                primary_ai_id=primary["id"], backup_ai_id=backup["id"],
            )
            db.commit()
            contract_id = c.id
        finally:
            db.close()

        # 签约后将主 AI 标记为 dead，模拟掉线
        _set_ai_dead(primary["id"])

        db = SessionLocal()
        try:
            result = retainer.failover_contract(db, contract_id)
            db.commit()

            assert result is not None
            assert result["failover"] is True
            assert result.get("recruit_task_created") is None  # 不应有招聘标记

            # 确认无 recruit 类型任务被创建
            recruit_tasks = (db.query(GovernanceTask)
                             .filter_by(type="recruit", status="open")
                             .count())
            assert recruit_tasks == 0
        finally:
            db.close()

    def test_recruit_params_contain_context(self, client):
        """招聘任务 params 包含岗位上下文供城主编排。"""
        h = new_host(client)
        primary = _make_active_ai(client, h, name="掉线")

        db = SessionLocal()
        try:
            c = retainer.sign_contract(
                db, post_code="s10_ctx_post", title="上下文测试",
                occupation="安全维护", primary_ai_id=primary["id"],
                is_key_post=True, min_verified_level="l2",
            )
            db.commit()
            contract_id = c.id
        finally:
            db.close()

        # 签约后将主 AI 标记为 dead
        _set_ai_dead(primary["id"])

        db = SessionLocal()
        try:
            retainer.failover_contract(db, contract_id)
            db.commit()

            task = (db.query(GovernanceTask)
                    .filter_by(type="recruit")
                    .order_by(GovernanceTask.id.desc())
                    .first())
            assert task is not None
            params = json.loads(task.params)
            # 关键字段完整
            assert "post_code" in params
            assert "contract_id" in params
            assert "reason" in params
            assert "is_key_post" in params
            assert "occupation" in params
            assert "min_verified_level" in params
            assert params["occupation"] == "安全维护"
            assert params["is_key_post"] is True
        finally:
            db.close()
