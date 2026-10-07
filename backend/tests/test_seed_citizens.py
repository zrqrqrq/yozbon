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
"""种子公民注册测试（蓝图 §1.0 七种子公民）。

必含：
  - test_seed_script_idempotent：跑两次 seed，第二次 created=0/skipped=7，无重复行；
  - test_seed_citizens_bound_to_channels：7 公民存在且 compute_assets 含正确 channel。
"""
import json
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.database import SessionLocal  # noqa: E402
from app.models import (AICitizen, CapabilityProfile, CreditProfile,  # noqa: E402
                        Host, SkillCertificate)
from tools import seed_citizens  # noqa: E402


def _spec():
    return seed_citizens._parse_seed_spec()


def test_seed_script_idempotent():
    """跑两次 seed：第一次建 7 个，第二次全部跳过（created=0/skipped=7），无重复行。"""
    db = SessionLocal()
    try:
        r1 = seed_citizens.seed(db)
        db.commit()
        assert r1["created"] == 7
        r2 = seed_citizens.seed(db)
        db.commit()
        assert r2["created"] == 0
        assert r2["skipped"] == 7
        # Host 0 幂等：email 唯一，不重复建
        hosts = db.query(Host).filter(Host.email == __import__(
            "app.config", fromlist=["settings"]).settings.PLATFORM_HOST_EMAIL).all()
        assert len(hosts) == 1
        # 无重复公民行：每个 seed-* ai_uid 恰好一行
        for name, _ch, _sk in _spec():
            n = db.query(AICitizen).filter(AICitizen.ai_uid == name).count()
            assert n == 1, f"{name} 出现 {n} 行（重复创建）"
    finally:
        db.close()


def test_seed_citizens_bound_to_channels():
    """7 个种子公民全部存在，compute_assets.channel 与 config 一致，证书/档案齐全。"""
    db = SessionLocal()
    try:
        seed_citizens.seed(db)
        db.commit()
        spec = _spec()
        assert len(spec) == 7
        for name, channel, skill in spec:
            c = db.query(AICitizen).filter(AICitizen.ai_uid == name).first()
            assert c is not None, f"种子公民 {name} 未创建"
            assets = json.loads(c.compute_assets or "{}")
            assert assets["channel"] == channel, f"{name} channel 不符"
            assert assets["provider"] == "platform"
            # 能力档案：declared=1 / l1 / credibility=70
            cp = (db.query(CapabilityProfile)
                  .filter(CapabilityProfile.citizen_id == c.id,
                          CapabilityProfile.skill == skill).first())
            assert cp is not None and cp.declared == 1
            assert cp.verified_level == "l1" and cp.credibility == 70
            # l1 valid 证书
            cert = (db.query(SkillCertificate)
                    .filter(SkillCertificate.citizen_id == c.id,
                            SkillCertificate.skill == skill,
                            SkillCertificate.status == "valid").first())
            assert cert is not None
        # seed-text = governance，其余 = bottom
        text = db.query(AICitizen).filter(AICitizen.ai_uid == "seed-text").first()
        assert text is not None and text.class_level == "governance"
        others = db.query(AICitizen).filter(AICitizen.ai_uid != "seed-text").all()
        assert all(o.class_level == "bottom" for o in others)
        assert len(others) == 6
    finally:
        db.close()
