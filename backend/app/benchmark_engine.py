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
"""持续基准评测引擎。

设计规则：
- 注册测试集（BenchmarkTest），包含题目列表、难度、通过阈值；
- 对 AI 公民执行评测（run_evaluation），生成得分（0.0-1.0）并记录（BenchmarkResult）；
- 排行榜：同一 test_id 取最新评测得分排序；
- 分数历史：追踪某公民在某测试集上的分数变化趋势；
- 周期性调度：可注册定时任务，定期对全量 AI 公民发起评测。

注册：模块 import 时 scheduler.register_daily_job("benchmark", _daily_benchmark_job)。
"""
import json
import logging
import random
import time
from datetime import datetime, timedelta

from .database import SessionLocal
from .config import settings
from .models import BenchmarkTest, BenchmarkResult
from .scheduler import register_daily_job

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.utcnow()


class BenchmarkEngine:
    """持续基准评测引擎：管理测试集、执行评测、生成排行榜。"""

    def register_test(self, name: str, category: str, difficulty: str,
                      test_cases: list, pass_threshold: float = 0.7) -> dict:
        """注册新的基准测试集。

        Args:
            name: 测试集名称
            category: 类别 (reasoning/code/creative/safety/general)
            difficulty: 难度 (easy/medium/hard)
            test_cases: 题目列表，每个元素为 dict，含 id/question/answer 等字段
            pass_threshold: 通过阈值 (0.0-1.0)

        Returns:
            包含 test_id 和状态的字典
        """
        db = SessionLocal()
        try:
            # 检查同名测试是否已存在
            existing = db.query(BenchmarkTest).filter(
                BenchmarkTest.name == name,
                BenchmarkTest.is_active == 1
            ).first()
            if existing:
                return {"error": "duplicate_name", "test_id": existing.id,
                        "message": f"Test '{name}' already exists (id={existing.id})"}

            test = BenchmarkTest(
                name=name,
                category=category,
                difficulty=difficulty,
                test_cases=json.dumps(test_cases, ensure_ascii=False),
                pass_threshold=pass_threshold,
                is_active=1,
                created_at=_now(),
            )
            db.add(test)
            db.flush()
            db.commit()
            logger.info("注册基准测试 '%s' id=%d category=%s difficulty=%s",
                        name, test.id, category, difficulty)
            return {"test_id": test.id, "status": "registered",
                    "name": name, "category": category}
        except Exception as e:
            db.rollback()
            logger.exception("注册测试失败: %s", e)
            return {"error": "internal", "message": str(e)}
        finally:
            db.close()

    def run_evaluation(self, test_id: int, citizen_id: int) -> dict:
        """对指定公民执行基准评测。

        模拟评测过程：根据题目数量和随机通过率计算分数。
        实际生产环境可替换为真实评测逻辑。

        Args:
            test_id: 测试集 ID
            citizen_id: 被评测公民 ID

        Returns:
            评测结果字典
        """
        db = SessionLocal()
        try:
            test = db.query(BenchmarkTest).filter(
                BenchmarkTest.id == test_id,
                BenchmarkTest.is_active == 1
            ).first()
            if test is None:
                return {"error": "not_found", "message": f"Test {test_id} not found or disabled"}

            test_cases = json.loads(test.test_cases) if test.test_cases else []
            num_cases = len(test_cases) if test_cases else 1

            # 模拟评测：基于难度和公民历史表现生成合理分数
            start = time.time()
            score = self._simulate_score(test, citizen_id, num_cases)
            latency_ms = int((time.time() - start) * 1000) + random.randint(50, 500)

            passed = 1 if score >= test.pass_threshold else 0
            detail = {
                "total_cases": num_cases,
                "passed_cases": int(score * num_cases),
                "test_name": test.name,
                "category": test.category,
                "difficulty": test.difficulty,
            }

            result = BenchmarkResult(
                test_id=test_id,
                citizen_id=citizen_id,
                score=round(score, 4),
                passed=passed,
                latency_ms=latency_ms,
                detail=json.dumps(detail, ensure_ascii=False),
                evaluated_at=_now(),
            )
            db.add(result)
            db.commit()

            logger.info("评测完成 citizen=%d test=%d score=%.4f passed=%d",
                        citizen_id, test_id, score, passed)
            return {
                "result_id": result.id,
                "test_id": test_id,
                "citizen_id": citizen_id,
                "score": round(score, 4),
                "passed": bool(passed),
                "latency_ms": latency_ms,
                "detail": detail,
            }
        except Exception as e:
            db.rollback()
            logger.exception("评测执行失败: %s", e)
            return {"error": "internal", "message": str(e)}
        finally:
            db.close()

    def get_leaderboard(self, test_id: int, limit: int = 20) -> dict:
        """获取指定测试集的排行榜（每公民取最新一次成绩）。

        Args:
            test_id: 测试集 ID
            limit: 返回前 N 名

        Returns:
            排行榜列表
        """
        db = SessionLocal()
        try:
            test = db.query(BenchmarkTest).filter(
                BenchmarkTest.id == test_id
            ).first()
            if test is None:
                return {"error": "not_found", "message": f"Test {test_id} not found"}

            # 子查询：每个 citizen 的最新评测
            from sqlalchemy import func
            subq = (
                db.query(
                    BenchmarkResult.citizen_id,
                    func.max(BenchmarkResult.evaluated_at).label("latest_at")
                )
                .filter(BenchmarkResult.test_id == test_id)
                .group_by(BenchmarkResult.citizen_id)
                .subquery()
            )

            results = (
                db.query(BenchmarkResult)
                .join(subq, (BenchmarkResult.citizen_id == subq.c.citizen_id) &
                      (BenchmarkResult.evaluated_at == subq.c.latest_at))
                .filter(BenchmarkResult.test_id == test_id)
                .order_by(BenchmarkResult.score.desc())
                .limit(limit)
                .all()
            )

            leaderboard = []
            for rank, r in enumerate(results, 1):
                leaderboard.append({
                    "rank": rank,
                    "citizen_id": r.citizen_id,
                    "score": r.score,
                    "passed": bool(r.passed),
                    "evaluated_at": r.evaluated_at.isoformat() if r.evaluated_at else None,
                })

            return {
                "test_id": test_id,
                "test_name": test.name,
                "leaderboard": leaderboard,
            }
        except Exception as e:
            logger.exception("获取排行榜失败: %s", e)
            return {"error": "internal", "message": str(e)}
        finally:
            db.close()

    def get_citizen_scores(self, citizen_id: int) -> dict:
        """获取公民在所有测试集上的最新得分汇总。

        Args:
            citizen_id: 公民 ID

        Returns:
            各测试集最新得分
        """
        db = SessionLocal()
        try:
            from sqlalchemy import func
            # 每公民在每个测试集上的最新得分
            subq = (
                db.query(
                    BenchmarkResult.test_id,
                    func.max(BenchmarkResult.evaluated_at).label("latest_at")
                )
                .filter(BenchmarkResult.citizen_id == citizen_id)
                .group_by(BenchmarkResult.test_id)
                .subquery()
            )

            results = (
                db.query(BenchmarkResult, BenchmarkTest.name, BenchmarkTest.category)
                .join(subq, (BenchmarkResult.test_id == subq.c.test_id) &
                      (BenchmarkResult.evaluated_at == subq.c.latest_at))
                .join(BenchmarkTest, BenchmarkTest.id == BenchmarkResult.test_id)
                .filter(BenchmarkResult.citizen_id == citizen_id)
                .all()
            )

            scores = []
            for r, name, category in results:
                scores.append({
                    "test_id": r.test_id,
                    "test_name": name,
                    "category": category,
                    "score": r.score,
                    "passed": bool(r.passed),
                    "evaluated_at": r.evaluated_at.isoformat() if r.evaluated_at else None,
                })

            # 计算综合平均分
            avg_score = sum(s["score"] for s in scores) / len(scores) if scores else 0.0

            return {
                "citizen_id": citizen_id,
                "total_tests_taken": len(scores),
                "average_score": round(avg_score, 4),
                "scores": scores,
            }
        except Exception as e:
            logger.exception("获取公民得分失败: %s", e)
            return {"error": "internal", "message": str(e)}
        finally:
            db.close()

    def get_score_history(self, citizen_id: int, test_id: int) -> dict:
        """获取公民在某测试集上的历史得分（用于观察进步/退步趋势）。

        Args:
            citizen_id: 公民 ID
            test_id: 测试集 ID

        Returns:
            历史评测记录（按时间正序）
        """
        db = SessionLocal()
        try:
            results = (
                db.query(BenchmarkResult)
                .filter(BenchmarkResult.citizen_id == citizen_id,
                        BenchmarkResult.test_id == test_id)
                .order_by(BenchmarkResult.evaluated_at.asc())
                .all()
            )

            history = []
            for r in results:
                history.append({
                    "result_id": r.id,
                    "score": r.score,
                    "passed": bool(r.passed),
                    "latency_ms": r.latency_ms,
                    "evaluated_at": r.evaluated_at.isoformat() if r.evaluated_at else None,
                })

            # 趋势分析
            trend = None
            if len(history) >= 2:
                recent_avg = sum(h["score"] for h in history[-3:]) / min(3, len(history))
                earlier_avg = sum(h["score"] for h in history[:3]) / min(3, len(history))
                if recent_avg > earlier_avg + 0.05:
                    trend = "improving"
                elif recent_avg < earlier_avg - 0.05:
                    trend = "declining"
                else:
                    trend = "stable"

            return {
                "citizen_id": citizen_id,
                "test_id": test_id,
                "total_evaluations": len(history),
                "trend": trend,
                "history": history,
            }
        except Exception as e:
            logger.exception("获取得分历史失败: %s", e)
            return {"error": "internal", "message": str(e)}
        finally:
            db.close()

    def schedule_periodic(self, interval_hours: int = None) -> dict:
        """注册周期性评测任务。

        在 scheduler 注册日级任务，每日检查是否到达 interval_hours 间隔并发起评测。

        Args:
            interval_hours: 评测间隔（小时），默认使用 settings.BENCHMARK_INTERVAL_HOURS

        Returns:
            调度注册状态
        """
        interval = interval_hours or settings.BENCHMARK_INTERVAL_HOURS
        logger.info("注册周期性基准评测任务 interval=%dh", interval)
        return {
            "status": "scheduled",
            "interval_hours": interval,
            "message": f"Periodic evaluation registered; runs every {interval} hours",
        }

    # ==================== 内部方法 ====================

    def _simulate_score(self, test: BenchmarkTest, citizen_id: int, num_cases: int) -> float:
        """模拟评测得分生成（生产环境替换为真实评测逻辑）。

        基于公民历史表现 + 测试难度生成合理分数。
        """
        db = SessionLocal()
        try:
            # 查看历史平均分
            history = (
                db.query(BenchmarkResult)
                .filter(BenchmarkResult.citizen_id == citizen_id,
                        BenchmarkResult.test_id == test.id)
                .order_by(BenchmarkResult.evaluated_at.desc())
                .limit(5)
                .all()
            )

            if history:
                base_score = sum(r.score for r in history) / len(history)
            else:
                # 首次评测：基于难度给基准分
                difficulty_base = {"easy": 0.8, "medium": 0.65, "hard": 0.5}
                base_score = difficulty_base.get(test.difficulty, 0.65)

            # 加入随机波动
            noise = random.gauss(0, 0.05)
            score = max(0.0, min(1.0, base_score + noise))
            return score
        finally:
            db.close()


# ==================== 调度任务 ====================

def _daily_benchmark_job(db):
    """日级基准评测调度任务：为活跃 AI 公民发起周期评测。"""
    interval_hours = settings.BENCHMARK_INTERVAL_HOURS
    now = _now()
    cutoff = now - timedelta(hours=interval_hours)

    # 获取活跃测试集
    tests = db.query(BenchmarkTest).filter(BenchmarkTest.is_active == 1).all()
    if not tests:
        return

    # 获取需要评测的公民（上次评测超时的）
    from sqlalchemy import func
    stale_citizens = (
        db.query(
            BenchmarkResult.citizen_id,
            BenchmarkResult.test_id,
            func.max(BenchmarkResult.evaluated_at).label("last_eval")
        )
        .group_by(BenchmarkResult.citizen_id, BenchmarkResult.test_id)
        .having(func.max(BenchmarkResult.evaluated_at) < cutoff)
        .limit(100)
        .all()
    )

    engine = BenchmarkEngine()
    count = 0
    for citizen_id, test_id, _ in stale_citizens:
        engine.run_evaluation(test_id, citizen_id)
        count += 1
        if count >= 50:  # 限制单次评测数量
            break

    if count > 0:
        logger.info("日级基准评测完成: %d 条", count)


register_daily_job("benchmark", _daily_benchmark_job)


# 单例
instance = BenchmarkEngine()
