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
"""训练进度检查点 + 异常检测服务。

功能：
- 记录训练过程中的中间检查点（epoch/step/loss/benchmark/GPU时/已耗资金）；
- 自动异常检测：loss 上升且步距异常大 → 疑似发散；资金超预算 → 超支；
- 提供进度历史查询、异常列表、摘要统计。

本模块只 flush，commit 由调用方/路由层负责。
"""
from datetime import datetime

from sqlalchemy.orm import Session

from .models import TrainingProgress, TrainingCampaign


class TrainingProgressError(Exception):
    """训练进度业务异常（路由层映射为 HTTP 400）。"""


def _now() -> datetime:
    return datetime.utcnow()


# ======================== 检查点记录 ========================

def record_checkpoint(db: Session, campaign_id: int, epoch: int, step: int,
                      loss: float, benchmark: float = 0,
                      gpu_hours_used: float = 0,
                      funding_spent_cent: int = 0,
                      message: str = "") -> TrainingProgress:
    """记录训练检查点，自动检测异常。

    异常检测规则：
    1. loss 较上一检查点上升 AND step > 上一检查点.step + 100 → 疑似训练发散；
    2. funding_spent_cent > campaign.goal_funding_cent → 资金超支。
    """
    campaign = db.get(TrainingCampaign, campaign_id)
    if campaign is None:
        raise TrainingProgressError(f"Training campaign {campaign_id} not found")

    # 获取上一个检查点用于异常判定
    prev = (db.query(TrainingProgress)
            .filter(TrainingProgress.campaign_id == campaign_id)
            .order_by(TrainingProgress.id.desc())
            .first())

    anomaly = 0

    # 规则 1：loss 上升且步距异常大（疑似发散）
    if prev is not None:
        if loss > prev.loss and step > prev.step + 100:
            anomaly = 1

    # 规则 2：资金超支
    if campaign.goal_funding_cent > 0 and funding_spent_cent > campaign.goal_funding_cent:
        anomaly = 1

    progress = TrainingProgress(
        campaign_id=campaign_id,
        epoch=epoch,
        step=step,
        loss=loss,
        benchmark=benchmark,
        gpu_hours_used=gpu_hours_used,
        funding_spent_cent=funding_spent_cent,
        anomaly=anomaly,
        message=message,
    )
    db.add(progress)
    db.flush()
    return progress


# ======================== 查询 ========================

def get_latest_progress(db: Session, campaign_id: int) -> TrainingProgress | None:
    """获取指定训练活动的最新检查点。"""
    return (db.query(TrainingProgress)
            .filter(TrainingProgress.campaign_id == campaign_id)
            .order_by(TrainingProgress.id.desc())
            .first())


def get_progress_history(db: Session, campaign_id: int,
                         limit: int = 50) -> list[dict]:
    """获取训练进度历史（倒序），返回字典列表。"""
    limit = min(max(int(limit), 1), 200)
    rows = (db.query(TrainingProgress)
            .filter(TrainingProgress.campaign_id == campaign_id)
            .order_by(TrainingProgress.id.desc())
            .limit(limit)
            .all())
    return [
        {
            "id": r.id,
            "campaign_id": r.campaign_id,
            "epoch": r.epoch,
            "step": r.step,
            "loss": r.loss,
            "benchmark": r.benchmark,
            "gpu_hours_used": r.gpu_hours_used,
            "funding_spent_cent": r.funding_spent_cent,
            "anomaly": r.anomaly,
            "message": r.message,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


def detect_anomalies(db: Session, campaign_id: int) -> list[dict]:
    """返回指定训练活动的所有异常检查点。"""
    rows = (db.query(TrainingProgress)
            .filter(TrainingProgress.campaign_id == campaign_id,
                    TrainingProgress.anomaly == 1)
            .order_by(TrainingProgress.id.desc())
            .all())
    return [
        {
            "id": r.id,
            "campaign_id": r.campaign_id,
            "epoch": r.epoch,
            "step": r.step,
            "loss": r.loss,
            "benchmark": r.benchmark,
            "gpu_hours_used": r.gpu_hours_used,
            "funding_spent_cent": r.funding_spent_cent,
            "message": r.message,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


# ======================== 摘要统计 ========================

def progress_summary(db: Session, campaign_id: int) -> dict:
    """训练进度摘要：最新 loss、已完成 epoch 数、GPU 总时、已耗资金、异常数。"""
    latest = get_latest_progress(db, campaign_id)

    anomaly_count = (db.query(TrainingProgress)
                     .filter(TrainingProgress.campaign_id == campaign_id,
                             TrainingProgress.anomaly == 1)
                     .count())

    epochs_completed = 0
    gpu_hours = 0.0
    funding_spent = 0
    latest_loss = 0.0

    if latest is not None:
        epochs_completed = latest.epoch
        gpu_hours = latest.gpu_hours_used
        funding_spent = latest.funding_spent_cent
        latest_loss = latest.loss

    return {
        "latest_loss": latest_loss,
        "epochs_completed": epochs_completed,
        "gpu_hours": gpu_hours,
        "funding_spent": funding_spent,
        "anomaly_count": anomaly_count,
    }
