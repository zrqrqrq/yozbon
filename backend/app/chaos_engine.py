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
"""混沌工程实验引擎。

管理混沌实验的创建、启动、完成、中止和报告。
实际环境需要外部基础设施支持故障注入，这里记录实验生命周期。
"""
import json
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from .database import SessionLocal
from .config import settings
from .models import ChaosExperiment

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.utcnow()


class ChaosEngine:
    """混沌工程实验引擎。"""

    def create_experiment(self, db: Session, name: str, target_service: str,
                          fault_type: str, params: dict,
                          scheduled_at: datetime = None) -> int:
        """创建混沌实验，返回 experiment_id。"""
        exp = ChaosExperiment(
            name=name,
            target_service=target_service,
            fault_type=fault_type,
            params=json.dumps(params),
            scheduled_at=scheduled_at,
            status="pending",
        )
        db.add(exp)
        db.commit()
        logger.info("chaos experiment created: %s target=%s fault=%s",
                    name, target_service, fault_type)
        return exp.id

    def start(self, db: Session, experiment_id: int):
        """标记 running，模拟注入（实际环境需要外部支持，这里记录日志）。"""
        exp = db.get(ChaosExperiment, experiment_id)
        if exp is None:
            raise ValueError(f"Experiment {experiment_id} not found")
        if exp.status not in ("pending", "scheduled"):
            raise ValueError(f"Experiment status is {exp.status}; cannot start")
        exp.status = "running"
        exp.started_at = _now()
        db.commit()
        logger.warning("chaos experiment %d started: injecting %s into %s",
                       experiment_id, exp.fault_type, exp.target_service)

    def complete(self, db: Session, experiment_id: int, result_summary: str):
        """完成实验并记录结果摘要。"""
        exp = db.get(ChaosExperiment, experiment_id)
        if exp is None:
            raise ValueError(f"Experiment {experiment_id} not found")
        exp.status = "completed"
        exp.result_summary = result_summary
        exp.completed_at = _now()
        db.commit()

    def abort(self, db: Session, experiment_id: int):
        """中止实验。"""
        exp = db.get(ChaosExperiment, experiment_id)
        if exp is None:
            raise ValueError(f"Experiment {experiment_id} not found")
        exp.status = "aborted"
        exp.completed_at = _now()
        db.commit()
        logger.info("chaos experiment %d aborted", experiment_id)

    def list_experiments(self, db: Session, status: str = None) -> list:
        """列出实验，可按状态过滤。"""
        q = db.query(ChaosExperiment)
        if status:
            q = q.filter(ChaosExperiment.status == status)
        items = q.order_by(ChaosExperiment.started_at.desc()).all()
        return [
            {
                "id": e.id,
                "name": e.name,
                "target_service": e.target_service,
                "fault_type": e.fault_type,
                "status": e.status,
                "started_at": e.started_at.isoformat() if e.started_at else None,
                "completed_at": e.completed_at.isoformat() if e.completed_at else None,
            }
            for e in items
        ]

    def get_report(self, db: Session, experiment_id: int) -> dict:
        """获取实验报告。"""
        exp = db.get(ChaosExperiment, experiment_id)
        if exp is None:
            raise ValueError(f"Experiment {experiment_id} not found")
        return {
            "id": exp.id,
            "name": exp.name,
            "target_service": exp.target_service,
            "fault_type": exp.fault_type,
            "params": json.loads(exp.params) if exp.params else {},
            "status": exp.status,
            "result_summary": exp.result_summary,
            "scheduled_at": exp.scheduled_at.isoformat() if exp.scheduled_at else None,
            "started_at": exp.started_at.isoformat() if exp.started_at else None,
            "completed_at": exp.completed_at.isoformat() if exp.completed_at else None,
        }


chaos_engine = ChaosEngine()
