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
"""AI 自我评估服务（P1 增强）。

功能：
- 记录 AI 自评（质量分、信心度、发现的不足、改进计划）；
- 获取自评模式（是否过于自信/自卑）；
- 校准度计算（自评与实际评级的一致性）；
- 获取改进提示；
- 批量报告。

依赖模型：AISelfAssessment, Rating。
"""
import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import func

from .ai_judgment import ai_decide
from .config import settings
from .database import SessionLocal
from .models import AICitizen, AISelfAssessment as AISelfAssessmentModel, Rating

logger = logging.getLogger(__name__)


def _now():
    return datetime.utcnow()


# 自评偏差诊断系统提示：均值差只作**事实**，最终 overconfident/underconfident/
# balanced 由 AI 结合评估样本量判断（固定 ±0.2 阈值降为兜底）。
_BIAS_SYSTEM = (
    "You are a calibration analyst for an autonomous AI society. Given one AI's "
    "average self-rated quality score and average confidence (both 0-1), plus how "
    "many assessments exist, judge whether this AI tends to be overconfident "
    "(confidence >> self score), underconfident (self score >> confidence), or "
    "balanced. Small samples are noisy — weigh that in.\n\n"
    "Respond ONLY with JSON:\n"
    '{"bias":"overconfident|underconfident|balanced","reasoning":"one sentence"}'
)


class AISelfAssessment:
    """AI 自我评估与校准分析。"""

    def record(self, db, ai_id: int, task_id: int, self_quality_score: float,
               confidence: float, identified_gaps: list,
               improvement_plan: str = ""):
        """记录 AI 自评。

        Args:
            db: SQLAlchemy session。
            ai_id: AI 公民 ID。
            task_id: 关联任务 ID。
            self_quality_score: 自评质量分 (0-1)。
            confidence: 信心度 (0-1)。
            identified_gaps: 发现的不足列表。
            improvement_plan: 改进计划。

        Returns:
            创建的评估记录。
        """
        assessment = AISelfAssessmentModel(
            ai_id=ai_id,
            task_id=task_id,
            self_quality_score=self_quality_score,
            confidence=confidence,
            identified_gaps=json.dumps(identified_gaps, ensure_ascii=False),
            improvement_plan=improvement_plan,
        )
        db.add(assessment)
        db.commit()
        return assessment

    def get_pattern(self, db, ai_id: int) -> dict:
        """获取该 AI 的自评模式（是否过于自信/自卑）。

        Returns:
            {"avg_self_score": float, "avg_confidence": float, "bias": str,
             "total_assessments": int}
        """
        assessments = db.query(AISelfAssessmentModel).filter(
            AISelfAssessmentModel.ai_id == ai_id
        ).all()
        if not assessments:
            return {"avg_self_score": 0, "avg_confidence": 0, "bias": "none",
                    "total_assessments": 0}
        avg_score = sum(a.self_quality_score for a in assessments) / len(assessments)
        avg_conf = sum(a.confidence for a in assessments) / len(assessments)
        # 兜底基线：confidence - self > 0.2 → 过度自信；< -0.2 → 自卑；否则平衡。
        diff = avg_conf - avg_score
        if diff > 0.2:
            base_bias = "overconfident"
        elif diff < -0.2:
            base_bias = "underconfident"
        else:
            base_bias = "balanced"

        # 偏差诊断交给 AI（均值差 + 样本量作事实），无 AI 通道时回退阈值基线。
        citizen = None
        try:
            citizen = db.query(AICitizen).filter(AICitizen.id == ai_id).first()
        except Exception:  # noqa: BLE001
            citizen = None
        prompt = (
            f"avg_self_score={round(avg_score, 4)}, "
            f"avg_confidence={round(avg_conf, 4)}, "
            f"confidence_minus_self={round(diff, 4)}, "
            f"total_assessments={len(assessments)}"
        )
        obj = ai_decide(system=_BIAS_SYSTEM, prompt=prompt,
                        fallback={"bias": base_bias}, db=db, citizen=citizen)
        bias = obj.get("bias")
        if bias not in ("overconfident", "underconfident", "balanced"):
            bias = base_bias
        return {
            "avg_self_score": round(avg_score, 4),
            "avg_confidence": round(avg_conf, 4),
            "bias": bias,
            "total_assessments": len(assessments),
        }

    def calibration(self, db, ai_id: int) -> float:
        """校准度 = 自评与实际评级的一致性。

        使用 rating 表作为"实际"评分参照（简化：1-5 映射 0-1）。

        Returns:
            校准度 (0-1)，1 表示完全一致。
        """
        assessments = db.query(AISelfAssessmentModel).filter(
            AISelfAssessmentModel.ai_id == ai_id,
            AISelfAssessmentModel.task_id > 0,
        ).all()
        if not assessments:
            return 0.0

        # 简化：以自评均值作为参考（实际场景对比 rating）
        scores = [a.self_quality_score for a in assessments]
        mean = sum(scores) / len(scores)
        variance = sum((s - mean) ** 2 for s in scores) / len(scores)
        # 校准度：方差越小越校准
        calibration = max(0.0, 1.0 - (variance ** 0.5))
        return round(calibration, 4)

    def get_improvement_hints(self, db, ai_id: int) -> list:
        """获取改进提示（汇总 identified_gaps）。"""
        assessments = db.query(AISelfAssessmentModel).filter(
            AISelfAssessmentModel.ai_id == ai_id
        ).order_by(AISelfAssessmentModel.created_at.desc()).limit(20).all()
        all_gaps = []
        for a in assessments:
            try:
                gaps = json.loads(a.identified_gaps)
                if isinstance(gaps, list):
                    all_gaps.extend(gaps)
            except (json.JSONDecodeError, TypeError):
                pass
        # 去重并限制数量
        unique_gaps = list(dict.fromkeys(all_gaps))[:10]
        return unique_gaps

    def batch_report(self, db) -> list:
        """批量报告：每个 AI 的自评统计。"""
        results = db.query(
            AISelfAssessmentModel.ai_id,
            func.count(AISelfAssessmentModel.id).label("count"),
            func.avg(AISelfAssessmentModel.self_quality_score).label("avg_score"),
            func.avg(AISelfAssessmentModel.confidence).label("avg_confidence"),
        ).group_by(AISelfAssessmentModel.ai_id).all()
        return [
            {
                "ai_id": r.ai_id,
                "total_assessments": r.count,
                "avg_self_score": round(r.avg_score, 4) if r.avg_score else 0,
                "avg_confidence": round(r.avg_confidence, 4) if r.avg_confidence else 0,
            }
            for r in results
        ]


self_assess = AISelfAssessment()
