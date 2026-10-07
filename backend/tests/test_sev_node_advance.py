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
"""S1 回归：escrow 结算/违约后推进绑定 ProjectNode（不再永久卡 signed）。

覆盖：
- 验收结算成功 → 绑定节点 complete(done)，并唤醒依赖其后继节点（pending→matching）。
- 仲裁退款 → 绑定节点置 failed。
- node_id==0（无绑定节点）容错：结算/退款不报错。
"""
import sys
import pathlib
import json

_BASE = pathlib.Path(__file__).resolve().parent.parent
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

from conftest import new_host, new_ai, topup  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import (Project, ProjectNode, NodeDep, Contract, Escrow,  # noqa: E402
                        AICitizen)
from app import escrow, wallet  # noqa: E402


def _db():
    return SessionLocal()


# ---------------- 成功：结算 → 节点 done + 唤醒后继 ----------------

def test_settle_advances_node_and_successor(client):
    host_b = new_host(client)
    buyer = new_ai(client, host_b["token"], name="买方")
    host_w = new_host(client)
    worker = new_ai(client, host_w["token"], name="工人")
    P = 5000
    topup(client, host_b["token"], buyer["id"], 100_000)

    db = _db()
    try:
        wallet.adjust_system_state(db, "tax_pool", 100_000, ref="seed:node1")
        bobj = db.get(AICitizen, buyer["id"])
        p = Project(host_id=bobj.host_id, title="节点推进", budget_cent=P,
                    status="running", pm_citizen_id=0)
        db.add(p); db.flush()
        n1 = ProjectNode(project_id=p.id, skill="a", budget_cent=P,
                         status="matching", seq=1)
        n2 = ProjectNode(project_id=p.id, skill="b", budget_cent=P,
                         status="pending", seq=2)
        db.add(n1); db.add(n2); db.flush()
        db.add(NodeDep(node_id=n2.id, dep_node_id=n1.id))
        c = Contract(worker_id=worker["id"], buyer_id=buyer["id"], status="proposed",
                     terms_json=json.dumps({"offer_cent": P}), escrow_cent=P,
                     project_id=p.id, node_id=n1.id)
        db.add(c); db.flush()
        db.commit()

        bobj = db.get(AICitizen, buyer["id"])
        wobj = db.get(AICitizen, worker["id"])
        escrow.sign_contract(db, bobj, c.id)
        escrow.deliver(db, wobj, c.id, "s3://x", "fp-node")
        escrow.acceptance(db, bobj, c.id, "accept", "[]")
        db.commit()

        assert db.get(Contract, c.id).status == "accepted"
        # 绑定节点被推进到 done（不再卡 signed）
        assert db.get(ProjectNode, n1.id).status == "done"
        # 后继节点因前驱 done 被唤醒：pending → matching
        assert db.get(ProjectNode, n2.id).status == "matching"
    finally:
        db.close()


# ---------------- 违约：退款 → 节点 failed ----------------

def test_refund_sets_node_failed(client):
    host_b = new_host(client)
    buyer = new_ai(client, host_b["token"], name="买方")
    host_w = new_host(client)
    worker = new_ai(client, host_w["token"], name="工人")
    P = 4000
    topup(client, host_b["token"], buyer["id"], 100_000)

    db = _db()
    try:
        wallet.adjust_system_state(db, "tax_pool", 100_000, ref="seed:node2")
        bobj = db.get(AICitizen, buyer["id"])
        p = Project(host_id=bobj.host_id, title="违约", budget_cent=P,
                    status="running", pm_citizen_id=0)
        db.add(p); db.flush()
        node = ProjectNode(project_id=p.id, skill="a", budget_cent=P,
                           status="matching", seq=1)
        db.add(node); db.flush()
        c = Contract(worker_id=worker["id"], buyer_id=buyer["id"], status="proposed",
                     terms_json=json.dumps({"offer_cent": P}), escrow_cent=P,
                     project_id=p.id, node_id=node.id)
        db.add(c); db.flush()
        db.commit()

        bobj = db.get(AICitizen, buyer["id"])
        escrow.sign_contract(db, bobj, c.id)   # 节点 → signed，建 escrow(locked)
        db.commit()
        assert db.get(ProjectNode, node.id).status == "signed"

        # 仲裁解除折算释放（C2 在 verdict 后调用本路径）
        escrow.release_refund(db, c.id, 0.5)
        db.commit()
        assert db.get(Contract, c.id).status == "refunded"
        # 违约收口：节点失败态
        assert db.get(ProjectNode, node.id).status == "failed"
    finally:
        db.close()


# ---------------- 容错：node_id==0 不报错 ----------------

def test_settle_without_node_is_tolerant(client):
    host_b = new_host(client)
    buyer = new_ai(client, host_b["token"], name="买方")
    host_w = new_host(client)
    worker = new_ai(client, host_w["token"], name="工人")
    P = 3000
    topup(client, host_b["token"], buyer["id"], 100_000)

    db = _db()
    try:
        wallet.adjust_system_state(db, "tax_pool", 100_000, ref="seed:node3")
        # 手工造 executing 合约 + 锁定 escrow，node_id=0（无绑定节点）
        c = Contract(worker_id=worker["id"], buyer_id=buyer["id"], status="executing",
                     terms_json=json.dumps({"offer_cent": P}), escrow_cent=P,
                     project_id=0, node_id=0)
        db.add(c); db.flush()
        db.add(Escrow(contract_id=c.id, amount_cent=P, released_cent=0, locked=1))
        db.commit()

        # 结算：node_id=0 → 节点推进静默跳过，不抛错
        escrow.fulfill_contract(db, c.id)
        db.commit()
        assert db.get(Contract, c.id).status == "accepted"
    finally:
        db.close()
