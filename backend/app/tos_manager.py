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
"""服务条款（TOS）管理服务。

管理 TOS 版本发布、用户接受记录、强制版本检查和接受率统计。
"""
import json
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from .database import SessionLocal
from .config import settings
from .models import TermsOfServiceVersion, TOSAcceptance

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.utcnow()


class TOSManager:
    """服务条款管理服务。"""

    def publish_version(self, db: Session, version: str, content: str,
                        effective_date: datetime, mandatory: bool = True) -> int:
        """发布新版本的 TOS，返回 version_id。"""
        tos = TermsOfServiceVersion(
            version=version,
            content=content,
            effective_date=effective_date,
            mandatory=1 if mandatory else 0,
        )
        db.add(tos)
        db.commit()
        logger.info("TOS version %s published, id=%d", version, tos.id)
        return tos.id

    def publish(self, db: Session, version: str, content: str,
                effective_date=None, publisher_id: int = 0,
                mandatory: bool = True) -> int:
        """发布 TOS 版本（HTTP 端点入口别名，等价 publish_version）。

        与 publish_version 的差异仅在入参宽松化，便于路由层直接透传：
        - effective_date 兼容 datetime / ISO 字符串 / 空串（空串→当前时间）；
        - publisher_id：TermsOfServiceVersion 表无发布者列，此处仅写审计日志留痕
          （不改动表结构）。
        返回新建版本行的 version_id。
        """
        # 解析生效时间：支持 datetime、ISO 字符串；无法解析/为空则回退当前时间
        if isinstance(effective_date, datetime):
            eff = effective_date
        elif effective_date:
            try:
                eff = datetime.fromisoformat(str(effective_date))
            except ValueError:
                eff = _now()
        else:
            eff = _now()
        version_id = self.publish_version(db, version=version, content=content,
                                          effective_date=eff, mandatory=mandatory)
        # publisher_id 留痕：版本表无发布者字段，仅落审计日志，不污染 TOS 行
        if publisher_id:
            from .models import AuditLog
            db.add(AuditLog(actor_type="system", actor_id=int(publisher_id),
                            action="tos.publish",
                            detail=json.dumps({"version_id": version_id,
                                               "version": version,
                                               "publisher_id": int(publisher_id)},
                                              ensure_ascii=False)))
            db.commit()
        return version_id

    def resolve_version_id(self, db: Session, version: str):
        """按版本字符串查最新版本行 id（同名多版本取最新发布）；不存在返回 None。"""
        tos = db.query(TermsOfServiceVersion).filter(
            TermsOfServiceVersion.version == version
        ).order_by(TermsOfServiceVersion.published_at.desc()).first()
        return tos.id if tos is not None else None

    def accept(self, db: Session, version_id: int, host_id: int,
               ai_id: int = None, ip_address: str = ""):
        """记录宿主/AI 接受 TOS。"""
        tos = db.get(TermsOfServiceVersion, version_id)
        if tos is None:
            raise ValueError(f"TOS version {version_id} not found")

        acceptance = TOSAcceptance(
            version_id=version_id,
            host_id=host_id,
            ai_id=ai_id,
            ip_address=ip_address,
        )
        db.add(acceptance)
        # A-M3 修复：原子 UPDATE 自增（避免 read-modify-write 并发计数丢失）。
        db.query(TermsOfServiceVersion).filter(
            TermsOfServiceVersion.id == version_id).update(
            {TermsOfServiceVersion.accepted_count:
             TermsOfServiceVersion.accepted_count + 1},
            synchronize_session=False)
        db.commit()

    def check_required(self, db: Session, host_id: int) -> dict:
        """检查该宿主是否已接受当前强制版本。"""
        # 找最新的强制版本
        latest = db.query(TermsOfServiceVersion).filter(
            TermsOfServiceVersion.mandatory == 1
        ).order_by(TermsOfServiceVersion.published_at.desc()).first()

        if latest is None:
            return {"required": False, "accepted": True, "version": None}

        acceptance = db.query(TOSAcceptance).filter(
            TOSAcceptance.version_id == latest.id,
            TOSAcceptance.host_id == host_id,
        ).first()

        return {
            "required": True,
            "accepted": acceptance is not None,
            "version": latest.version,
            "version_id": latest.id,
            "effective_date": latest.effective_date.isoformat() if latest.effective_date else None,
        }

    def get_latest(self, db: Session) -> dict:
        """获取最新 TOS 版本。"""
        tos = db.query(TermsOfServiceVersion).order_by(
            TermsOfServiceVersion.published_at.desc()
        ).first()
        if tos is None:
            return {}
        return {
            "id": tos.id,
            "version": tos.version,
            "content": tos.content,
            "effective_date": tos.effective_date.isoformat() if tos.effective_date else None,
            "mandatory": bool(tos.mandatory),
            "accepted_count": tos.accepted_count,
        }

    def list_versions(self, db: Session) -> list:
        """列出所有 TOS 版本。"""
        items = db.query(TermsOfServiceVersion).order_by(
            TermsOfServiceVersion.published_at.desc()
        ).all()
        return [
            {
                "id": t.id,
                "version": t.version,
                "effective_date": t.effective_date.isoformat() if t.effective_date else None,
                "mandatory": bool(t.mandatory),
                "accepted_count": t.accepted_count,
            }
            for t in items
        ]

    def stats(self, db: Session, version_id: int) -> dict:
        """接受率统计。"""
        tos = db.get(TermsOfServiceVersion, version_id)
        if tos is None:
            raise ValueError(f"TOS version {version_id} not found")
        acceptances = db.query(TOSAcceptance).filter(
            TOSAcceptance.version_id == version_id
        ).all()
        unique_hosts = len(set(a.host_id for a in acceptances))
        return {
            "version_id": version_id,
            "version": tos.version,
            "total_acceptances": len(acceptances),
            "unique_hosts": unique_hosts,
        }


tos_manager = TOSManager()
