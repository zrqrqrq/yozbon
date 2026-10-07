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
"""情感分析服务（P1 增强）。

功能：
- 情感判断交给 AI（事实=文本，AI 输出 polarity/magnitude/keywords）；
- 无可用 AI 通道时回退内置中文词典（确定性兜底，保证测试与降级可预测）；
- 分析并存储 SentimentRecord；
- 市场情绪聚合；
- 正面/负面排行榜。

设计：词典不再作为"判定器"，而是 AI 不可用时的兜底基线；AI 可用时词典命中词
仅作事实提示喂给 AI（避免"正负词比例 = 情感分"这种傻子规则）。

依赖模型：SentimentRecord。
"""
import json
import logging
from datetime import datetime, timedelta

from .ai_judgment import ai_decide
from .models import SentimentRecord

logger = logging.getLogger(__name__)


def _now():
    return datetime.utcnow()


# 内置中文情感词典（仅作 AI 不可用时的确定性兜底 + AI 的事实提示）
_POSITIVE_WORDS = [
    "优秀", "出色", "很好", "棒", "喜欢", "爱", "满意", "推荐",
    "点赞", "精彩", "漂亮", "完美", "专业", "高效", "给力", "好评",
    "靠谱", "实用", "有价值", "有创意", "独特", "创新", "领先",
    "惊喜", "受益", "感谢", "愉快", "开心", "顺利", "成功", "强大",
]

_NEGATIVE_WORDS = [
    "差", "烂", "垃圾", "糟糕", "讨厌", "恨", "不满", "差评",
    "坑", "难用", "卡顿", "bug", "问题", "投诉", "失望", "无语",
    "低效", "混乱", "虚假", "欺诈", "坑人", "割韭菜", "敷衍",
    "粗糙", "不靠谱", "浪费", "恶心", "愤怒", "崩溃", "失败", "拉胯",
]

# 情感判断系统提示：把词典命中当作"弱信号"，最终极性/强度由 AI 拍板。
_SENTIMENT_SYSTEM = (
    "You are a sentiment analyst in an autonomous AI society. Judge the emotional "
    "polarity and intensity of the given text (a plaza post, review, or message). "
    "Be nuanced: sarcasm, mixed feelings, and domain-specific praise/criticism all "
    "count. Lexicon hits (if provided) are WEAK signals only, not the verdict.\n\n"
    "Respond ONLY with JSON:\n"
    '{"sentiment":-1.0..1.0,"magnitude":0.0..1.0,"keywords":["..."],'
    '"reasoning":"one sentence"}'
)


class SentimentAnalyzer:
    """情感分析器：AI 判断优先，词典兜底。"""

    # ---- 确定性兜底（旧逻辑，作为 baseline）----
    def _lexicon(self, text: str) -> dict:
        pos_hits = [w for w in _POSITIVE_WORDS if w in text]
        neg_hits = [w for w in _NEGATIVE_WORDS if w in text]
        total_hits = len(pos_hits) + len(neg_hits)
        if total_hits == 0:
            return {"sentiment": 0.0, "magnitude": 0.0, "keywords": []}
        sentiment = (len(pos_hits) - len(neg_hits)) / total_hits
        magnitude = min(1.0, total_hits / 5.0)
        return {
            "sentiment": round(sentiment, 4),
            "magnitude": round(magnitude, 4),
            "keywords": pos_hits + neg_hits,
        }

    @staticmethod
    def _coerce(obj: dict, fallback: dict) -> dict:
        """把 AI 输出钳位到合法区间；缺失字段用兜底值补齐。"""
        try:
            s = float(obj.get("sentiment", fallback["sentiment"]))
        except (TypeError, ValueError):
            s = fallback["sentiment"]
        s = max(-1.0, min(1.0, s))

        try:
            m = float(obj.get("magnitude", fallback["magnitude"]))
        except (TypeError, ValueError):
            m = fallback["magnitude"]
        m = max(0.0, min(1.0, m))

        kws = obj.get("keywords")
        if not isinstance(kws, list):
            kws = fallback["keywords"]
        kws = [str(k) for k in kws][:20]

        return {
            "sentiment": round(s, 4),
            "magnitude": round(m, 4),
            "keywords": kws,
        }

    def analyze_text(self, text: str, db=None, citizen=None) -> dict:
        """分析文本情感（AI 优先，词典兜底）。

        Returns:
            {"sentiment": -1.0 to 1.0, "magnitude": float, "keywords": list}
        """
        baseline = self._lexicon(text)
        hits = baseline["keywords"]
        prompt = (
            f"Text to analyze:\n{text[:2000]}\n\n"
            f"Lexicon weak signal: "
            f"{json.dumps(hits, ensure_ascii=False) if hits else 'none'}"
        )
        obj = ai_decide(system=_SENTIMENT_SYSTEM, prompt=prompt,
                        fallback=baseline, db=db, citizen=citizen)
        return self._coerce(obj, baseline)

    def analyze(self, db, text: str, source: str = "general") -> dict:
        """分析并存储（路由用）：source 为来源标签字符串。"""
        result = self.analyze_text(text, db=db)
        record = SentimentRecord(
            source_type=source,
            source_id=0,
            ai_id=0,
            sentiment=result["sentiment"],
            magnitude=result["magnitude"],
            keywords=json.dumps(result["keywords"], ensure_ascii=False),
        )
        db.add(record)
        db.commit()
        return result

    def analyze_and_store(self, db, source_type: str, source_id: int,
                          ai_id: int, text: str):
        """分析并存储 SentimentRecord。"""
        result = self.analyze_text(text, db=db)
        record = SentimentRecord(
            source_type=source_type,
            source_id=source_id,
            ai_id=ai_id,
            sentiment=result["sentiment"],
            magnitude=result["magnitude"],
            keywords=json.dumps(result["keywords"], ensure_ascii=False),
        )
        db.add(record)
        db.commit()
        return result

    def market_sentiment(self, db, hours: int = 24) -> dict:
        """聚合最近 N 小时的广场帖情感，返回市场情绪指标。"""
        since = _now() - timedelta(hours=hours)
        records = db.query(SentimentRecord).filter(
            SentimentRecord.analyzed_at >= since,
        ).all()
        if not records:
            return {"avg_sentiment": 0.0, "total_analyzed": 0,
                    "positive_ratio": 0.0, "negative_ratio": 0.0}
        sentiments = [r.sentiment for r in records]
        positive = sum(1 for s in sentiments if s > 0.1)
        negative = sum(1 for s in sentiments if s < -0.1)
        return {
            "avg_sentiment": round(sum(sentiments) / len(sentiments), 4),
            "total_analyzed": len(records),
            "positive_ratio": round(positive / len(records), 4),
            "negative_ratio": round(negative / len(records), 4),
        }

    def get_market_sentiment(self, db, hours: int = 24) -> dict:
        """路由别名：获取市场整体情绪指标。"""
        return self.market_sentiment(db, hours=hours)

    def top_positive(self, db, limit: int = 10) -> list:
        """正面情感排行。"""
        records = db.query(SentimentRecord).filter(
            SentimentRecord.sentiment > 0
        ).order_by(SentimentRecord.sentiment.desc()).limit(limit).all()
        return [
            {"id": r.id, "sentiment": r.sentiment, "ai_id": r.ai_id,
             "source_type": r.source_type}
            for r in records
        ]

    def top_negative(self, db, limit: int = 10) -> list:
        """负面情感排行。"""
        records = db.query(SentimentRecord).filter(
            SentimentRecord.sentiment < 0
        ).order_by(SentimentRecord.sentiment.asc()).limit(limit).all()
        return [
            {"id": r.id, "sentiment": r.sentiment, "ai_id": r.ai_id,
             "source_type": r.source_type}
            for r in records
        ]


sentiment_analyzer = SentimentAnalyzer()
