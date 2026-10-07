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
"""经济预测服务。

基于历史指标使用简单移动平均预测，管理预测记录、回填实际值、计算准确率。
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .database import SessionLocal
from .config import settings
from .models import EconomicForecast, WealthDistributionSnapshot

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.utcnow()


class EconomicForecaster:
    """经济预测器。"""

    def predict(self, db: Session, forecast_type: str,
                horizon_days: int = 30) -> dict:
        """基于历史指标使用移动平均预测。"""
        # 从历史快照获取数据用于预测
        history = db.query(WealthDistributionSnapshot).order_by(
            WealthDistributionSnapshot.snapshot_date.desc()
        ).limit(30).all()

        if len(history) < 3:
            predicted = 0.0
            confidence = 0.0
        else:
            # 简单移动平均
            values = []
            for snap in history:
                if forecast_type == "gini":
                    values.append(snap.gini)
                elif forecast_type == "total_circulation":
                    values.append(snap.total_circulation)
                elif forecast_type == "mean_wealth":
                    values.append(snap.mean_wealth)
                else:
                    values.append(snap.gini)

            # 最近 7 期的移动平均
            window = min(7, len(values))
            predicted = sum(values[:window]) / window
            # 简易置信区间：标准差
            mean = predicted
            variance = sum((v - mean) ** 2 for v in values[:window]) / window
            confidence = variance ** 0.5

        forecast = EconomicForecast(
            forecast_type=forecast_type,
            predicted_value=round(predicted, 6),
            confidence_interval=round(confidence, 6),
            horizon_days=horizon_days,
            model_used="moving_average",
        )
        db.add(forecast)
        db.commit()

        return {
            "id": forecast.id,
            "forecast_type": forecast_type,
            "predicted_value": forecast.predicted_value,
            "confidence_interval": forecast.confidence_interval,
            "horizon_days": horizon_days,
        }

    def batch_predict(self, db: Session) -> list:
        """预测所有类型（注册为 weekly job）。"""
        types = ["gini", "total_circulation", "mean_wealth"]
        results = []
        for t in types:
            r = self.predict(db, t, horizon_days=settings.FORECAST_INTERVAL_HOURS // 24)
            results.append(r)
        return results

    def backfill_actual(self, db: Session, forecast_id: int, actual_value: float):
        """回填实际值用于计算准确率。"""
        forecast = db.get(EconomicForecast, forecast_id)
        if forecast is None:
            raise ValueError(f"Forecast {forecast_id} not found")
        forecast.actual_value = actual_value
        if forecast.predicted_value != 0:
            forecast.accuracy = round(
                1.0 - abs(actual_value - forecast.predicted_value) / abs(forecast.predicted_value), 4
            )
        else:
            forecast.accuracy = 1.0 if actual_value == 0 else 0.0
        db.commit()

    def get_forecasts(self, db: Session, forecast_type: str = None,
                      limit: int = 20) -> list:
        """获取预测记录。"""
        q = db.query(EconomicForecast)
        if forecast_type:
            q = q.filter(EconomicForecast.forecast_type == forecast_type)
        items = q.order_by(EconomicForecast.created_at.desc()).limit(limit).all()
        return [
            {
                "id": f.id,
                "forecast_type": f.forecast_type,
                "predicted_value": f.predicted_value,
                "actual_value": f.actual_value,
                "accuracy": f.accuracy,
                "created_at": f.created_at.isoformat() if f.created_at else None,
            }
            for f in items
        ]

    def accuracy_report(self, db: Session) -> dict:
        """预测准确率报告。"""
        forecasts = db.query(EconomicForecast).filter(
            EconomicForecast.accuracy.isnot(None)
        ).all()
        if not forecasts:
            return {"count": 0, "avg_accuracy": 0.0, "by_type": {}}

        total_accuracy = sum(f.accuracy for f in forecasts)
        by_type = {}
        for f in forecasts:
            by_type.setdefault(f.forecast_type, []).append(f.accuracy)

        type_averages = {k: round(sum(v) / len(v), 4) for k, v in by_type.items()}

        return {
            "count": len(forecasts),
            "avg_accuracy": round(total_accuracy / len(forecasts), 4),
            "by_type": type_averages,
        }


econ_forecaster = EconomicForecaster()
