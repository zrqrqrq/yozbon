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
"""弹劾服务（P1 治理增强）。

功能：
- 发起弹劾案；
- 投票（for/against）；
- 投票结束后判定结果（超过 quorum 阈值则 removal，否则 dismissed）；
- 获取活跃弹劾案 / 单案详情。

依赖模型：ImpeachmentCase, AICitizen。
"""
import json
import logging
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError

from .config import settings
from .database import SessionLocal
from .models import AICitizen, Delegation, ImpeachmentCase, ImpeachmentVote

logger = logging.getLogger(__name__)


def _now():
    return datetime.utcnow()


class ImpeachmentService:
    """弹劾流程管理。"""

    def initiate(self, db, target_type: str, target_id: int,
                 initiated_by: int, charges: str) -> int:
        """创建弹劾案。

        Args:
            db: SQLAlchemy session。
            target_type: 弹劾对象类型（governor/delegate 等）。
            target_id: 弹劾对象 ID。
            initiated_by: 发起人宿主 ID。
            charges: 弹劾罪名描述。

        Returns:
            弹劾案 ID。
        """
        case = ImpeachmentCase(
            target_type=target_type,
            target_id=target_id,
            initiated_by=initiated_by,
            charges=charges,
            status="open",
        )
        db.add(case)
        db.commit()
        return case.id

    def vote(self, db, case_id: int, voter_host_id: int, vote: str):
        """投票（一人一票去重）。

        A-H2 修复：改为写独立 ImpeachmentVote 表，靠 (case_id, voter_host_id)
        唯一约束在 DB 层拒绝重复投票，消除 voters_json read-modify-write 无锁的
        并发吞票/重复计票。case 上的计数字段作为快速读取的派生缓存同步累加。

        Args:
            db: SQLAlchemy session。
            case_id: 弹劾案 ID。
            voter_host_id: 投票者宿主 ID（由鉴权层注入，不接受客户端伪造）。
            vote: "for" 或 "against"。

        Raises:
            ValueError: 案件不存在/未开放/重复投票。
        """
        case = db.query(ImpeachmentCase).filter(ImpeachmentCase.id == case_id).first()
        if not case or case.status != "open":
            raise ValueError(f"impeachment {case_id} not found or not open")
        # 写投票表（唯一约束去重）
        db.add(ImpeachmentVote(case_id=case_id, voter_host_id=voter_host_id, vote=vote))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise ValueError("already voted")
        # 派生计数累加（vote 表为去重真源）
        case.vote_total += 1
        if vote == "for":
            case.vote_for += 1
        elif vote == "against":
            case.vote_against += 1
        voters = self._voters(case)
        if voter_host_id not in voters:
            voters.append(voter_host_id)
            case.voters_json = json.dumps(voters)
        db.commit()

    @staticmethod
    def _voters(case) -> list:
        try:
            v = json.loads(getattr(case, "voters_json", None) or "[]")
            return v if isinstance(v, list) else []
        except Exception:  # noqa: BLE001
            return []

    def resolve(self, db, case_id: int):
        """投票结束后判定结果。

        双重阈值：参与票数须达 IMPEACHMENT_MIN_VOTES（最小法定人数），
        且赞成比例达 IMPEACHMENT_QUORUM 才 removal；否则 dismissed。
        removal 时 target 被撤换（governor 设 frozen，delegate 撤销授权）。
        仅由宿主鉴权端点调用（换城主权限）。

        Args:
            db: SQLAlchemy session。
            case_id: 弹劾案 ID。
        """
        case = db.query(ImpeachmentCase).filter(ImpeachmentCase.id == case_id).first()
        if not case or case.status != "open":
            return
        # 最小参与人数（法定人数）：不足直接 dismiss，杜绝单票/少数票罢黜城主
        if case.vote_total < settings.IMPEACHMENT_MIN_VOTES:
            case.status = "dismissed"
            case.resolved_at = _now()
            db.commit()
            return

        ratio = case.vote_for / case.vote_total
        if ratio >= settings.IMPEACHMENT_QUORUM:
            case.status = "removal"
            # 执行撤换
            if case.target_type == "governor":
                ai = db.query(AICitizen).filter(AICitizen.id == case.target_id).first()
                if ai:
                    ai.status = "frozen"
            elif case.target_type == "delegate":
                # A-L2 修复：弹劾通过后撤销被罢免 delegate AI 名下所有活跃委托授权，
                # 罢免真正落地（此前仅注释留接口、未实际撤权）。无活跃委托则空循环。
                from . import delegations
                active_dels = (db.query(Delegation)
                               .filter(Delegation.ai_id == case.target_id,
                                       Delegation.status == "active").all())
                for d in active_dels:
                    delegations.revoke_delegation(db, d.id)
        else:
            case.status = "dismissed"
        case.resolved_at = _now()
        db.commit()

    def get_active_cases(self, db) -> list:
        """获取所有活跃的弹劾案。"""
        cases = db.query(ImpeachmentCase).filter(
            ImpeachmentCase.status == "open"
        ).all()
        return [
            {
                "id": c.id,
                "target_type": c.target_type,
                "target_id": c.target_id,
                "charges": c.charges,
                "vote_for": c.vote_for,
                "vote_against": c.vote_against,
                "vote_total": c.vote_total,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in cases
        ]

    def get_case(self, db, case_id: int) -> dict:
        """获取单个弹劾案详情。"""
        c = db.query(ImpeachmentCase).filter(ImpeachmentCase.id == case_id).first()
        if not c:
            return {}
        return {
            "id": c.id,
            "target_type": c.target_type,
            "target_id": c.target_id,
            "initiated_by": c.initiated_by,
            "charges": c.charges,
            "status": c.status,
            "vote_for": c.vote_for,
            "vote_against": c.vote_against,
            "vote_total": c.vote_total,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "resolved_at": c.resolved_at.isoformat() if c.resolved_at else None,
        }


impeachment_service = ImpeachmentService()
