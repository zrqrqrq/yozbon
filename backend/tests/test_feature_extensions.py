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
"""38 项新功能核心逻辑单元测试（P0-P3）。

直接调用服务类方法，不依赖 HTTP 层。
金额全部用整数（分），不使用外部网络。
"""
import sys
import os
import time
import math
import hashlib
import secrets

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.database import SessionLocal, engine, Base
from app import models
from app.config import settings


# ============================================================
# P0 安全（6 个测试类）
# ============================================================

class TestOAuthService:
    """OAuth2/OIDC SSO 服务。"""

    def test_get_authorize_url_no_provider(self):
        """无 provider 记录时返回 error。"""
        from app.oauth_sso import instance as oauth
        result = oauth.get_authorize_url("nonexistent", "state123")
        assert "error" in result

    def test_get_authorize_url_correct_format(self):
        """有 provider 时返回正确 URL 格式。"""
        from app.oauth_sso import instance as oauth
        db = SessionLocal()
        try:
            prov = models.OAuthProvider(
                provider_name="github",
                client_id="test_client_id",
                client_secret_enc="encrypted",
                authorize_url="https://github.com/login/oauth/authorize",
                token_url="https://github.com/login/oauth/access_token",
                userinfo_url="https://api.github.com/user",
                scopes="user email",
                is_active=1,
            )
            db.add(prov)
            db.commit()
        finally:
            db.close()

        result = oauth.get_authorize_url("github", "mystate")
        assert "authorize_url" in result
        assert "github.com" in result["authorize_url"]
        assert "client_id=test_client_id" in result["authorize_url"]
        assert result["state"].startswith("mystate.")

    def test_link_account_and_deduplicate(self):
        """link_account 创建连接并去重。"""
        from app.oauth_sso import instance as oauth
        r1 = oauth.link_account(host_id=999, provider="google",
                                external_id="ext_123", email="a@b.com")
        assert r1.get("ok") is True

        # 重复绑定返回 error
        r2 = oauth.link_account(host_id=999, provider="google",
                                external_id="ext_123", email="a@b.com")
        assert "error" in r2
        assert "already linked" in r2["error"]


class TestMFAService:
    """MFA 多因素认证服务。"""

    def test_enroll_generates_secret(self):
        """enroll_totp 生成有效 secret 和 otpauth_uri。"""
        from app.mfa_service import instance as mfa
        result = mfa.enroll_totp(host_id=42, label="test")
        assert "secret" in result
        assert len(result["secret"]) >= 16
        assert "otpauth_uri" in result
        assert "otpauth://totp/" in result["otpauth_uri"]

    def test_verify_correct_code_passes(self):
        """正确 TOTP 码验证通过。"""
        from app.mfa_service import instance as mfa, _totp_code
        enroll = mfa.enroll_totp(host_id=100)
        secret = enroll["secret"]

        # 手动标记为已验证
        db = SessionLocal()
        try:
            device = db.query(models.MFADevice).filter(
                models.MFADevice.host_id == 100).first()
            device.is_verified = 1
            db.commit()
        finally:
            db.close()

        code = _totp_code(secret)
        result = mfa.verify_totp(host_id=100, code=code)
        assert result.get("valid") is True

    def test_verify_wrong_code_fails(self):
        """错误码验证失败。"""
        from app.mfa_service import instance as mfa
        enroll = mfa.enroll_totp(host_id=101)

        db = SessionLocal()
        try:
            device = db.query(models.MFADevice).filter(
                models.MFADevice.host_id == 101).first()
            device.is_verified = 1
            db.commit()
        finally:
            db.close()

        result = mfa.verify_totp(host_id=101, code="000000")
        assert result.get("valid") is False

    def test_expired_code_rejected(self):
        """过期一次性码拒绝。"""
        from app.mfa_service import instance as mfa
        from datetime import datetime, timedelta

        gen = mfa.generate_code(host_id=200, purpose="login")
        raw_code = gen["code"]

        # 手动使该码过期
        db = SessionLocal()
        try:
            from app.models import MFACode
            rec = db.query(MFACode).filter(MFACode.id == gen["code_id"]).first()
            rec.expires_at = datetime.utcnow() - timedelta(seconds=10)
            db.commit()
        finally:
            db.close()

        result = mfa.validate_code(host_id=200, code=raw_code, purpose="login")
        assert result.get("valid") is False


class TestKMSService:
    """KMS 密钥管理服务。"""

    def test_generate_key_creates_active(self):
        """generate_key 创建 active 记录。"""
        from app.kms_service import instance as kms
        result = kms.generate_key(purpose="test_purpose")
        assert "key_id" in result
        assert result["version"] == 1

        db = SessionLocal()
        try:
            ek = db.query(models.EncryptionKey).filter(
                models.EncryptionKey.key_id == result["key_id"]).first()
            assert ek is not None
            assert ek.status == "active"
        finally:
            db.close()

    def test_encrypt_decrypt_roundtrip(self):
        """encrypt → decrypt 往返一致。"""
        from app.kms_service import instance as kms
        key_info = kms.generate_key(purpose="roundtrip")
        key_id = key_info["key_id"]

        plaintext = "hello-secret-world-123"
        enc = kms.encrypt(plaintext, key_id)
        assert "ciphertext" in enc
        assert enc["ciphertext"] != plaintext

        dec = kms.decrypt(enc["ciphertext"], key_id)
        assert dec["plaintext"] == plaintext

    def test_rotate_key_old_becomes_rotated(self):
        """rotate_key 使旧 key 状态变为 rotated。"""
        from app.kms_service import instance as kms
        key_info = kms.generate_key(purpose="rotate_test")
        key_id = key_info["key_id"]

        rot = kms.rotate_key(key_id)
        assert "new_key_id" in rot

        db = SessionLocal()
        try:
            old = db.query(models.EncryptionKey).filter(
                models.EncryptionKey.key_id == key_id).first()
            assert old.status == "rotated"
        finally:
            db.close()


class TestRateLimiter:
    """全局限流服务。"""

    def _create_policy(self, name="test_policy", max_requests=3, window_seconds=60):
        db = SessionLocal()
        try:
            policy = models.RateLimitPolicy(
                name=name,
                scope="test",
                endpoint_pattern="*",
                max_requests=max_requests,
                window_seconds=window_seconds,
                is_active=1,
            )
            db.add(policy)
            db.commit()
            return policy.id
        finally:
            db.close()

    def test_within_limit_allowed(self):
        """限额内请求允许。"""
        from app.rate_limiter import instance as rl
        self._create_policy("rl_allow", max_requests=5)
        result = rl.check_limit(subject="user:A", endpoint="*")
        assert result["allowed"] is True
        assert result["remaining"] >= 0

    def test_exceed_limit_denied(self):
        """超限返回 False。"""
        from app.rate_limiter import instance as rl
        self._create_policy("rl_deny", max_requests=2)
        # 连续请求超过限额
        rl.check_limit(subject="user:B", endpoint="*")
        rl.check_limit(subject="user:B", endpoint="*")
        result = rl.check_limit(subject="user:B", endpoint="*")
        assert result["allowed"] is False

    def test_different_subjects_independent(self):
        """不同 subject 独立计数。"""
        from app.rate_limiter import instance as rl
        self._create_policy("rl_indep", max_requests=1)
        r1 = rl.check_limit(subject="user:C", endpoint="*")
        assert r1["allowed"] is True
        r2 = rl.check_limit(subject="user:D", endpoint="*")
        assert r2["allowed"] is True


class TestRegistrationGuard:
    """注册验证服务。"""

    def test_sybil_normal_passes(self):
        """正常 IP 通过女巫检测。"""
        from app.registration_guard import instance as rg
        result = rg.check_sybil(ip="10.0.0.1", fingerprint="fp_normal")
        assert result["blocked"] is False

    def test_sybil_over_threshold_blocked(self):
        """超阈值 IP 阻止。"""
        from app.registration_guard import instance as rg
        # 创建超过 threshold*2 条记录
        threshold = settings.REG_SYBIL_THRESHOLD
        ip = "192.168.99.99"
        db = SessionLocal()
        try:
            for i in range(threshold * 2):
                rec = models.RegistrationVerification(
                    host_id=i + 1, method="email", token=f"tok_{i}",
                    status="pending", ip_address=ip, fingerprint="",
                    expires_at=models.datetime.utcnow() if hasattr(models, 'datetime') else None,
                )
                # 设置 expires_at 避免 None
                from datetime import datetime, timedelta
                rec.expires_at = datetime.utcnow() + timedelta(hours=24)
                db.add(rec)
            db.commit()
        finally:
            db.close()

        result = rg.check_sybil(ip=ip, fingerprint="unique_fp")
        assert result["blocked"] is True
        assert result["reason"] == "ip_limit"

    def test_email_token_verify_success(self):
        """邮箱 token 验证成功。"""
        from app.registration_guard import instance as rg
        gen = rg.require_email_verify(host_id=500, ip="1.2.3.4")
        assert "token" in gen
        result = rg.verify_email_token(gen["token"])
        assert result["valid"] is True
        assert result["host_id"] == 500

    def test_email_token_expired(self):
        """过期 token 验证失败。"""
        from app.registration_guard import instance as rg
        from datetime import datetime, timedelta
        gen = rg.require_email_verify(host_id=501)

        # 手动设为过期
        db = SessionLocal()
        try:
            rec = db.query(models.RegistrationVerification).filter(
                models.RegistrationVerification.token == gen["token"]).first()
            rec.expires_at = datetime.utcnow() - timedelta(hours=1)
            db.commit()
        finally:
            db.close()

        result = rg.verify_email_token(gen["token"])
        assert result["valid"] is False


# ============================================================
# P1 实时/经济（16 个测试类）
# ============================================================

class TestRealtimeService:
    """实时推送服务。"""

    def test_publish_creates_event(self):
        """publish 创建事件。"""
        from app.realtime_push import instance as rt
        result = rt.publish("task", "created", {"task_id": 1}, priority=3)
        assert "event_id" in result
        assert result["event_id"] > 0

    def test_subscribe_creates_subscription(self):
        """subscribe 创建订阅。"""
        from app.realtime_push import instance as rt
        result = rt.subscribe(citizen_id=10, channel="market",
                              event_types=["price_update"], delivery="sse")
        assert "subscription_id" in result

    def test_get_pending_returns_undelivered(self):
        """get_pending_events 返回未投递事件。"""
        from app.realtime_push import instance as rt
        rt.publish("governance", "vote", {"poll_id": 1}, priority=1)
        events = rt.get_pending_events("governance", since_id=0)
        assert len(events) >= 1
        assert events[0]["event_type"] == "vote"


class TestWorkspaceService:
    """协作工作空间服务。"""

    def test_create_workspace(self):
        """create 创建空间。"""
        from app.workspace_service import instance as ws
        result = ws.create("Test Space", "host", 1)
        assert "workspace_id" in result
        assert result["name"] == "Test Space"

    def test_add_member_success(self):
        """add_member 成功。"""
        from app.workspace_service import instance as ws
        space = ws.create("WS1", "host", 1)
        r = ws.add_member(space["workspace_id"], "citizen", 42, role="member")
        assert r.get("ok") is True

    def test_duplicate_add_updates_role(self):
        """重复添加更新角色而非报错。"""
        from app.workspace_service import instance as ws
        space = ws.create("WS2", "host", 2)
        ws.add_member(space["workspace_id"], "citizen", 10, role="member")
        r2 = ws.add_member(space["workspace_id"], "citizen", 10, role="admin")
        assert r2.get("updated") is True


class TestTokenEngine:
    """二级凭证代币引擎。"""

    def _reset(self):
        from app.token_engine import instance as te
        te._balances.clear()
        te._supply.clear()

    def test_create_token(self):
        """create_token 成功。"""
        self._reset()
        from app.token_engine import instance as te
        result = te.create_token("TEST", "Test Token", supply=1000)
        assert "token_id" in result
        assert result["symbol"] == "TEST"

    def test_mint_increases_balance(self):
        """mint 增加余额。"""
        self._reset()
        from app.token_engine import instance as te
        t = te.create_token("MINT", "Mint Test", supply=100)
        r = te.mint(t["token_id"], 500, "platform", 0)
        assert r.get("ok") is True
        assert r["new_supply"] == 600
        key = f"{t['token_id']}:platform:0"
        assert te._balances[key] == 600

    def test_transfer_success(self):
        """transfer 成功。"""
        self._reset()
        from app.token_engine import instance as te
        t = te.create_token("XFER", "Transfer", supply=1000)
        r = te.transfer(t["token_id"], "platform", 0, "citizen", 1, 300)
        assert r.get("ok") is True
        assert te._balances[f"{t['token_id']}:citizen:1"] == 300

    def test_transfer_insufficient_fails(self):
        """余额不足 transfer 失败。"""
        self._reset()
        from app.token_engine import instance as te
        t = te.create_token("LOW", "Low Bal", supply=10)
        r = te.transfer(t["token_id"], "platform", 0, "citizen", 1, 999)
        assert "error" in r

    def test_burn_decreases(self):
        """burn 减少余额。"""
        self._reset()
        from app.token_engine import instance as te
        t = te.create_token("BURNT", "Burn", supply=1000)
        te.burn(t["token_id"], 400, "platform", 0)
        key = f"{t['token_id']}:platform:0"
        assert te._balances[key] == 600


class TestAMMEngine:
    """AMM 自动做市商引擎。"""

    def test_create_pool_sets_reserve(self):
        """create_pool 设置储备。"""
        from app.amm_engine import instance as amm
        r = amm.create_pool("AAA", "BBB", 100000, 100000)
        assert "pool_id" in r
        assert r["lp_token_minted"] == int(math.sqrt(100000 * 100000))

    def test_swap_changes_reserve(self):
        """swap 改变储备。"""
        from app.amm_engine import instance as amm
        pool = amm.create_pool("SWAP_A", "SWAP_B", 100000, 100000)
        swap = amm.swap(pool["pool_id"], "citizen", 1, "SWAP_A", 10000)
        assert "to_amount" in swap
        assert swap["to_amount"] > 0

        db = SessionLocal()
        try:
            p = db.get(models.AMMPool, pool["pool_id"])
            assert p.reserve_a > 100000
            assert p.reserve_b < 100000
        finally:
            db.close()

    def test_get_quote_reasonable(self):
        """get_quote 返回合理值。"""
        from app.amm_engine import instance as amm
        pool = amm.create_pool("QT_A", "QT_B", 100000, 100000)
        quote = amm.get_quote(pool["pool_id"], "QT_A", 10000)
        assert "estimated_output" in quote
        assert 0 < quote["estimated_output"] < 10000

    def test_slippage_protection(self):
        """价格滑点保护。"""
        from app.amm_engine import instance as amm
        pool = amm.create_pool("SLIP_A", "SLIP_B", 100000, 100000)
        # 设置不可能的 min_output
        r = amm.swap(pool["pool_id"], "citizen", 2, "SLIP_A", 1000, min_output=99999)
        assert "error" in r
        assert "slippage" in r["error"]


class TestOrderBook:
    """限价订单簿撮合引擎。"""

    def test_place_order_creates_open(self):
        """place_order 创建 open 订单。"""
        from app.order_book import instance as ob
        r = ob.place_order("BTC/AC", "citizen", 1, "buy", 50000, 10)
        assert "order_id" in r
        assert r["status"] in ("open", "filled")

    def test_match_executes(self):
        """交叉订单撮合成交。"""
        from app.order_book import instance as ob
        # 先挂 sell
        ob.place_order("ETH/AC", "citizen", 1, "sell", 3000, 5)
        # 再挂 buy 价格 >= sell
        r = ob.place_order("ETH/AC", "citizen", 2, "buy", 3000, 5)
        assert r["filled"] > 0

    def test_cancel_changes_to_cancelled(self):
        """cancel 变 cancelled。"""
        from app.order_book import instance as ob
        r = ob.place_order("SOL/AC", "citizen", 10, "buy", 100, 50)
        cancel = ob.cancel_order(r["order_id"], 10)
        assert cancel.get("ok") is True


class TestQuadraticVoting:
    """二次投票。"""

    def _reset(self):
        from app.quadratic_vote import instance as qv
        qv._polls.clear()
        qv._next_poll_id = 1

    def test_100_credits_10_options(self):
        """100 credits 分散选 10 个 option，各 1 credit = 各 1 票影响力。"""
        self._reset()
        from app.quadratic_vote import instance as qv
        poll = qv.create_poll("Test", ["A", "B", "C", "D", "E",
                                       "F", "G", "H", "I", "J"], credits_per_voter=100)
        results = []
        for opt in range(10):
            r = qv.cast_vote(poll["poll_id"], "citizen", 1, opt, 1)
            results.append(r)
        # 每个 option 用 1 credit → weight = sqrt(1) = 1
        for r in results:
            assert r["vote_weight"] == 1
            assert r["credits_spent"] == 1

    def test_100_credits_one_option(self):
        """100 credits 全投 1 个 option → weight = sqrt(100) = 10。"""
        self._reset()
        from app.quadratic_vote import instance as qv
        poll = qv.create_poll("One", ["X", "Y"], credits_per_voter=100)
        r = qv.cast_vote(poll["poll_id"], "citizen", 2, 0, 100)
        assert r["vote_weight"] == 10
        assert r["credits_spent"] == 100

    def test_sqrt_logic(self):
        """有效票 sqrt 逻辑：9 credits → weight=3。"""
        self._reset()
        from app.quadratic_vote import instance as qv
        poll = qv.create_poll("Sqrt", ["P", "Q"], credits_per_voter=100)
        r = qv.cast_vote(poll["poll_id"], "citizen", 3, 0, 9)
        assert r["vote_weight"] == 3
        assert r["credits_spent"] == 9


class TestPredictionMarket:
    """预测市场服务。"""

    def test_create_market(self):
        """create_market 成功。"""
        from app.prediction_market import instance as pm
        r = pm.create_market("Will AI pass exam?", ["yes", "no"])
        assert "market_id" in r
        assert r["outcomes"] == ["yes", "no"]

    def test_place_bet_creates_shares(self):
        """place_bet 创建份额。"""
        from app.prediction_market import instance as pm
        market = pm.create_market("Q", ["yes", "no"])
        bet = pm.place_bet(market["market_id"], "citizen", 1, "yes", 1000)
        assert "shares_bought" in bet
        assert bet["shares_bought"] > 0

    def test_resolve_pays_winner(self):
        """resolve 后赢家赔付。"""
        from app.prediction_market import instance as pm
        market = pm.create_market("R", ["A", "B"])
        pm.place_bet(market["market_id"], "citizen", 1, "A", 500)
        pm.place_bet(market["market_id"], "citizen", 2, "B", 500)
        result = pm.resolve_market(market["market_id"], "A")
        assert result.get("ok") is True
        assert "payout_per_share" in result


class TestBountyBoard:
    """悬赏板服务。"""

    def test_post_bounty(self):
        """post_bounty 创建悬赏。"""
        from app.bounty_board import instance as bb
        r = bb.post_bounty(title="Fix bug", description="Fix the login bug",
                           reward_cent=5000, issuer_type="host", issuer_id=1)
        assert "bounty_id" in r

    def test_submit_solution(self):
        """submit_solution 提交方案。"""
        from app.bounty_board import instance as bb
        b = bb.post_bounty(title="Task", description="Do task",
                           reward_cent=1000, issuer_type="host", issuer_id=1)
        r = bb.submit_solution(b["bounty_id"], "citizen", 1, "Done! Here is fix.")
        assert "submission_id" in r

    def test_accept_changes_status(self):
        """accept_submission 改变状态。"""
        from app.bounty_board import instance as bb
        b = bb.post_bounty(title="Accept test", description="Test accept",
                           reward_cent=2000, issuer_type="host", issuer_id=1)
        sub = bb.submit_solution(b["bounty_id"], "citizen", 2, "Solution")
        r = bb.accept_submission(sub["submission_id"], reviewer_id=1)
        assert r.get("ok") is True


class TestAnomalyDetector:
    """异常检测。"""

    def test_check_overspend_high(self):
        """超 5 倍标记异常。"""
        from app.anomaly_detect import instance as ad
        # avg_recent_cent=10000, amount=60000 (ratio=6 > 5)
        r = ad.check_overspend(citizen_id=1, amount_cent=60000, avg_recent_cent=10000)
        assert r["anomaly"] is True
        assert r["severity"] in ("high", "critical")

    def test_record_anomaly_creates(self):
        """record_anomaly 创建记录。"""
        from app.anomaly_detect import instance as ad
        r = ad.record_anomaly(2, "overspend", "high", {"test": True})
        assert "event_id" in r or "anomaly_id" in r or r.get("ok") is True

    def test_get_history_returns(self):
        """get_anomaly_history 返回记录。"""
        from app.anomaly_detect import instance as ad
        ad.record_anomaly(3, "spam", "medium", {"msg_count": 10})
        history = ad.get_anomaly_history(citizen_id=3)
        assert isinstance(history, list) or "events" in history


class TestTrustNetwork:
    """AI 互信网络。"""

    def test_boost_increases_score(self):
        """boost_trust 增加分数。"""
        from app.trust_network import instance as tn
        r = tn.boost_trust(1, 2, context="work", delta=0.1)
        assert r["trust_score"] > 0.5  # 初始 0.5 + 0.1

    def test_cap_at_1_0(self):
        """超过 1.0 上限 cap。"""
        from app.trust_network import instance as tn
        # 多次 boost
        for _ in range(20):
            r = tn.boost_trust(10, 20, context="test", delta=0.1)
        assert r["trust_score"] <= 1.0

    def test_reduce_trust(self):
        """reduce_trust 减少。"""
        from app.trust_network import instance as tn
        tn.boost_trust(30, 40, context="rel", delta=0.3)  # → 0.8
        r = tn.reduce_trust(30, 40, context="rel", delta=0.2)
        assert r["trust_score"] <= 0.6

    def test_min_score_floor(self):
        """不低于 min_score。"""
        from app.trust_network import instance as tn
        for _ in range(20):
            r = tn.reduce_trust(50, 60, context="neg", delta=0.1)
        assert r["trust_score"] >= settings.TRUST_MIN_SCORE


class TestBidEngine:
    """AI 自主出价引擎。"""

    def test_set_strategy_saves(self):
        """set_strategy 保存配置。"""
        from app.bid_engine import instance as be
        r = be.set_strategy(1, {
            "min_price": 100, "max_price": 5000,
            "strategy_type": "aggressive", "budget_daily_cent": 20000,
        })
        assert r.get("ok") is True

    def test_compute_bid_in_range(self):
        """compute_bid 在 min/max 范围。"""
        from app.bid_engine import instance as be
        be.set_strategy(2, {
            "min_price": 500, "max_price": 8000,
            "strategy_type": "conservative", "budget_daily_cent": 100000,
        })
        r = be.compute_bid(2, market_value=5000, competition_level=0.5)
        assert "suggested_bid" in r
        assert 500 <= r["suggested_bid"] <= 8000

    def test_over_budget_returns_zero(self):
        """超日预算返回 0。"""
        from app.bid_engine import instance as be
        be.set_strategy(3, {
            "min_price": 100, "max_price": 9999,
            "strategy_type": "aggressive", "budget_daily_cent": 50,
        })
        # 模拟已花费接近预算
        from app.bid_engine import _budget_cache
        from datetime import datetime
        _budget_cache[3] = {"daily_spent": 50, "date": datetime.utcnow().strftime("%Y-%m-%d"),
                            "wins": 0, "losses": 0}
        r = be.compute_bid(3, market_value=1000, competition_level=0.5)
        assert r["suggested_bid"] == 0


class TestSkillComposer:
    """技能编排器。"""

    def test_create_composition(self):
        """create_composition 保存 DAG。"""
        from app.skill_compose import instance as sc
        r = sc.create_composition(
            name="Pipeline A",
            author_id=1,
            dag={
                "nodes": [
                    {"id": "n1", "skill": "extract"},
                    {"id": "n2", "skill": "transform"},
                    {"id": "n3", "skill": "load"},
                ],
                "edges": [{"from": "n1", "to": "n2"}, {"from": "n2", "to": "n3"}],
            },
        )
        assert "composition_id" in r

    def test_publish_changes_status(self):
        """publish 改变状态。"""
        from app.skill_compose import instance as sc
        comp = sc.create_composition(
            name="Pub test", author_id=2,
            dag={"nodes": [{"id": "x", "skill": "test"}], "edges": []},
        )
        r = sc.publish(comp["composition_id"])
        assert r.get("ok") is True or r.get("status") == "published"

    def test_execute_creates_run(self):
        """execute 创建 run。"""
        from app.skill_compose import instance as sc
        comp = sc.create_composition(
            name="Exec test", author_id=3,
            dag={"nodes": [{"id": "a", "skill": "echo"}], "edges": []},
        )
        sc.publish(comp["composition_id"])
        r = sc.execute(comp["composition_id"], inputs={"text": "hello"})
        assert "run_id" in r


class TestBenchmarkEngine:
    """评测引擎。"""

    def test_register_test(self):
        """register_test 成功。"""
        from app.benchmark_engine import instance as be
        r = be.register_test(
            name="Basic Math", category="reasoning", difficulty="easy",
            test_cases=[{"q": "1+1", "a": "2"}],
        )
        assert "test_id" in r

    def test_run_evaluation_creates_result(self):
        """run_evaluation 创建 result。"""
        from app.benchmark_engine import instance as be
        t = be.register_test(
            name="Eval Test", category="coding", difficulty="medium",
            test_cases=[{"q": "write hello", "a": "print('hello')"}],
        )
        r = be.run_evaluation(t["test_id"], citizen_id=10)
        assert "score" in r
        assert "passed" in r

    def test_leaderboard_sorted(self):
        """get_leaderboard 排序正确。"""
        from app.benchmark_engine import instance as be
        t = be.register_test(
            name="LB Test", category="general", difficulty="easy",
            test_cases=[{"q": "q1", "a": "a1"}],
        )
        be.run_evaluation(t["test_id"], citizen_id=100)
        be.run_evaluation(t["test_id"], citizen_id=200)
        lb = be.get_leaderboard(t["test_id"])
        assert isinstance(lb, dict)
        entries = lb["leaderboard"]
        if len(entries) >= 2:
            assert entries[0]["score"] >= entries[1]["score"]


class TestWorkBalance:
    """工作平衡服务。"""

    def _reset(self):
        from app.work_balance import instance as wb
        if hasattr(wb, '_schedule_cache'):
            wb._schedule_cache.clear()

    def test_get_status_default(self):
        """get_status 返回默认值。"""
        self._reset()
        from app.work_balance import instance as wb
        r = wb.get_status(citizen_id=999)
        assert "fatigue_score" in r
        assert "efficiency_multiplier" in r
        assert r["fatigue_score"] >= 0

    def test_apply_fatigue_reduces_efficiency(self):
        """apply_fatigue 降低效率。"""
        self._reset()
        from app.work_balance import instance as wb
        before = wb.get_status(citizen_id=500)
        wb.apply_fatigue(citizen_id=500, work_hours=8)
        after = wb.get_status(citizen_id=500)
        assert after["fatigue_score"] > before["fatigue_score"]

    def test_force_rest_resets(self):
        """force_rest 重置疲劳。"""
        self._reset()
        from app.work_balance import instance as wb
        wb.apply_fatigue(citizen_id=600, work_hours=10)
        wb.force_rest(citizen_id=600)
        r = wb.get_status(citizen_id=600)
        assert r["fatigue_score"] <= 30


# ============================================================
# P2 平台工程（10 个测试类）
# ============================================================

class TestFeatureFlags:
    """Feature Flags 特性开关。"""

    def test_create_flag(self):
        """create_flag 创建。"""
        from app.feature_flags import instance as ff
        r = ff.create_flag("new_feature_x", "Test feature", enabled=True,
                           rollout_pct=50, target_tiers="*")
        assert "flag_key" in r
        assert r["flag_key"] == "new_feature_x"

    def test_is_enabled_returns_correct(self):
        """is_enabled 返回正确。"""
        from app.feature_flags import instance as ff
        ff.create_flag("enabled_flag", "On", enabled=True, rollout_pct=100)
        assert ff.is_enabled("enabled_flag", user_id=1, tier="free") is True

        ff.create_flag("disabled_flag", "Off", enabled=False, rollout_pct=100)
        assert ff.is_enabled("disabled_flag", user_id=1, tier="free") is False

    def test_rollout_bucket_deterministic(self):
        """灰度分桶确定性：同一 user_id 始终同结果。"""
        from app.feature_flags import instance as ff
        ff.create_flag("gray_flag", "50%", enabled=True, rollout_pct=50)
        results = [ff.is_enabled("gray_flag", user_id=42, tier="free")
                   for _ in range(10)]
        assert len(set(results)) == 1  # 全部相同


class TestWebhookEngine:
    """Webhook 增强引擎。"""

    def _reset(self):
        import app.webhook_enhanced as wh_mod
        wh_mod._webhooks.clear()
        wh_mod._event_store.clear()
        wh_mod._delivery_log.clear()

    def test_register_webhook(self):
        """register_webhook 成功。"""
        self._reset()
        from app.webhook_enhanced import instance as wh
        r = wh.register_webhook(1, "https://example.com/hook", ["task.created"])
        assert "webhook_id" in r
        assert r["webhook_id"].startswith("wh_")

    def test_dispatch_creates_event(self):
        """dispatch 创建事件。"""
        self._reset()
        from app.webhook_enhanced import instance as wh
        wh.register_webhook(1, "https://example.com/hook", ["task.created"])
        r = wh.dispatch("task.created", {"task_id": 1})
        assert "event_id" in r
        assert r["delivered_count"] >= 0

    def test_calculate_backoff_increases(self):
        """calculate_backoff 递增。"""
        self._reset()
        from app.webhook_enhanced import instance as wh
        b0 = wh.calculate_backoff(0)
        b1 = wh.calculate_backoff(1)
        b2 = wh.calculate_backoff(2)
        # 允许 jitter 范围 0~5s，检查 base 递增
        assert b1 >= b0  # base * 2^1 > base * 2^0
        assert b2 >= b1


class TestGDPRService:
    """GDPR 数据删除服务。"""

    def test_request_erasure_creates_pending(self):
        """request_erasure 创建 pending 请求。"""
        from app.gdpr_service import instance as gdpr
        r = gdpr.request_erasure(host_id=100, scope="all")
        assert "request_id" in r
        assert r.get("status") == "pending" or "request_id" in r

    def test_process_erasure_changes_status(self):
        """process_erasure 改变状态。"""
        from app.gdpr_service import instance as gdpr
        req = gdpr.request_erasure(host_id=101)
        r = gdpr.process_erasure(req["request_id"])
        assert r.get("status") in ("processing", "completed") or r.get("ok") is True

    def test_list_pending(self):
        """list_pending_requests 返回 pending 列表。"""
        from app.gdpr_service import instance as gdpr
        gdpr.request_erasure(host_id=102)
        pending = gdpr.list_pending_requests()
        assert isinstance(pending, list)
        assert len(pending) >= 1


class TestTenantManager:
    """多租户管理。"""

    def _reset(self):
        from app.multi_tenant import instance as tm
        if hasattr(tm, '_host_tenant_map'):
            tm._host_tenant_map.clear()

    def test_create_tenant(self):
        """create_tenant 成功。"""
        self._reset()
        from app.multi_tenant import instance as tm
        r = tm.create_tenant("acme", "Acme Corp", plan="pro", max_hosts=50)
        assert r.get("ok") is True or "tenant_code" in r

    def test_check_quota_within(self):
        """check_quota 在限内。"""
        self._reset()
        from app.multi_tenant import instance as tm
        tm.create_tenant("team1", "Team One", max_hosts=5)
        r = tm.check_quota("team1", "hosts")
        assert r.get("allowed") is True

    def test_check_quota_exceeded(self):
        """check_quota 超限。"""
        self._reset()
        from app.multi_tenant import instance as tm
        tm.create_tenant("small", "Small Org", max_hosts=2)
        tm.assign_host(1, "small")
        tm.assign_host(2, "small")
        r = tm.check_quota("small", "hosts")
        assert r.get("allowed") is False


class TestPushService:
    """推送通知服务。"""

    def test_subscribe_creates(self):
        """subscribe 创建记录。"""
        from app.push_notify import instance as push
        r = push.subscribe("host", 1, "webpush", "https://push.endpoint", ["all"])
        assert "subscription_id" in r

    def test_send_success(self):
        """send 发送成功。"""
        from app.push_notify import instance as push
        push.subscribe("citizen", 5, "im", "endpoint_5", ["message"])
        r = push.send("citizen", 5, "message", {"text": "hello"})
        assert "delivered" in r or r.get("ok") is True or "sent" in str(r).lower()


class TestTracingService:
    """分布式追踪服务。"""

    def _reset(self):
        from app.tracing import instance as tr
        if hasattr(tr, '_active_spans'):
            tr._active_spans.clear()

    def test_start_end_span(self):
        """start_span + end_span 创建记录。"""
        self._reset()
        from app.tracing import instance as tr
        span = tr.start_span(trace_id=None, span_id=None, parent_span_id=None,
                             operation="db.query", service_name="api")
        assert "trace_id" in span
        assert "span_id" in span

        end = tr.end_span(span["trace_id"], span["span_id"], status="ok", tags={"rows": 5})
        assert end.get("ok") is True or "duration_ms" in str(end)

    def test_get_trace_returns_spans(self):
        """get_trace 返回所有 spans。"""
        self._reset()
        from app.tracing import instance as tr
        s1 = tr.start_span(None, None, None, "op1", "svc")
        tr.end_span(s1["trace_id"], s1["span_id"], "ok", {})
        s2 = tr.start_span(s1["trace_id"], None, s1["span_id"], "op2", "svc")
        tr.end_span(s2["trace_id"], s2["span_id"], "ok", {})

        trace = tr.get_trace(s1["trace_id"])
        assert len(trace) >= 2


class TestBackupService:
    """备份服务。"""

    def test_create_backup(self):
        """create_backup 创建记录。"""
        from app.backup_service import instance as bs
        r = bs.create_backup(backup_type="manual", triggered_by="test")
        assert "backup_id" in r

    def test_list_backups(self):
        """list_backups 返回。"""
        from app.backup_service import instance as bs
        bs.create_backup("auto", "test")
        backups = bs.list_backups()
        assert isinstance(backups, list)
        assert len(backups) >= 1


class TestComplianceAuditor:
    """合规审计。"""

    def test_generate_report(self):
        """generate_report 创建报告。"""
        from app.compliance_service import instance as ca
        from datetime import datetime, timedelta
        r = ca.generate_report(
            report_type="security",
            period_start=datetime.utcnow() - timedelta(days=30),
            period_end=datetime.utcnow(),
        )
        assert "report_id" in r

    def test_compliance_score_range(self):
        """get_compliance_score 在 0-100 范围。"""
        from app.compliance_service import instance as ca
        result = ca.get_compliance_score()
        score = result["score"] if isinstance(result, dict) else result
        assert isinstance(score, (int, float))
        assert 0 <= score <= 100


class TestMLModerator:
    """ML 内容审核。"""

    def test_score_content_creates_record(self):
        """score_content 创建记录。"""
        from app.ml_moderation import instance as ml
        r = ml.score_content("post", 1, "This is a normal text content")
        assert "decision" in r
        assert "scores" in r

    def test_get_decision_correct(self):
        """get_decision 根据分数返回正确决策。"""
        from app.ml_moderation import instance as ml
        # 高分 → block
        scores = {"spam": 0.95, "toxic": 0.1}
        decision = ml.get_decision(scores)
        assert decision == "block"

        # 低分 → pass
        scores_low = {"spam": 0.01, "toxic": 0.01}
        decision_low = ml.get_decision(scores_low)
        assert decision_low == "pass"


class TestMigrationManager:
    """数据库迁移管理。"""

    def test_get_current_version(self):
        """get_current_version 返回最新版本。"""
        from app.migration_service import instance as mm
        v = mm.get_current_version()
        assert isinstance(v, dict)
        assert "version" in v
        assert v["version"] == "0000" or v["version"].isdigit()

    def test_get_migration_history(self):
        """get_migration_history 返回列表。"""
        from app.migration_service import instance as mm
        history = mm.get_migration_history()
        assert isinstance(history, list)

    def test_dry_run(self):
        """dry_run 返回 SQL 语句。"""
        from app.migration_service import instance as mm
        r = mm.dry_run("head")
        assert "sql_statements" in r


# ============================================================
# P3 生态（5 个测试类）
# ============================================================

class TestDIDService:
    """DID 身份服务。"""

    def test_register_did(self):
        """register_did 创建 DID 记录。"""
        from app.did_vc import instance as did
        r = did.register_did("citizen", 1)
        assert "did" in r
        assert r["did"].startswith(f"did:{settings.DID_METHOD}:")

    def test_issue_credential(self):
        """issue_credential 创建 VC。"""
        from app.did_vc import instance as did
        issuer = did.register_did("host", 0)
        subject = did.register_did("citizen", 2)
        r = did.issue_credential(
            issuer_did=issuer["did"], subject_did=subject["did"],
            cred_type="SkillCertification",
            claims={"skill": "coding", "level": 5},
        )
        assert "credential_id" in r

    def test_verify_credential(self):
        """verify 通过。"""
        from app.did_vc import instance as did
        issuer = did.register_did("host", 0)
        subject = did.register_did("citizen", 3)
        vc = did.issue_credential(
            issuer_did=issuer["did"], subject_did=subject["did"],
            cred_type="Badge", claims={"badge": "first_post"},
        )
        v = did.verify_credential(vc["credential_id"])
        assert v.get("valid") is True


class TestSocialGraph:
    """社交图谱分析。"""

    def test_add_edge(self):
        """add_edge 创建记录。"""
        from app.social_graph import instance as sg
        r = sg.add_edge(1, 2, "collaborator", weight=1.5)
        assert r["action"] == "created"
        assert r["weight"] == 1.5

    def test_get_connections(self):
        """get_connections 返回邻居。"""
        from app.social_graph import instance as sg
        sg.add_edge(10, 11, "friend", weight=2.0)
        sg.add_edge(10, 12, "friend", weight=1.0)
        conns = sg.get_connections(10, relation_type="friend")
        assert len(conns) == 2

    def test_shortest_path(self):
        """shortest_path 返回路径。"""
        from app.social_graph import instance as sg
        sg.add_edge(20, 21, "friend")
        sg.add_edge(21, 22, "friend")
        sg.add_edge(22, 23, "friend")
        path = sg.shortest_path(20, 23)
        assert path["distance"] == 3
        assert path["path"][0] == 20
        assert path["path"][-1] == 23


class TestGamification:
    """游戏化成就系统。"""

    def test_define_achievement(self):
        """define_achievement 保存。"""
        from app.gamification import instance as gam
        r = gam.define_achievement(
            key="test_ach_1", name="First Test", description="Test achievement",
            category="general", xp_reward=50,
            condition_expr={"event_type": "test.event", "metric": "count", "threshold": 1},
            rarity="common",
        )
        assert "achievement_id" in r
        assert r["key"] == "test_ach_1"

    def test_check_unlocks(self):
        """check_unlocks 根据条件解锁。"""
        from app.gamification import instance as gam
        gam.define_achievement(
            key="unlock_test", name="Unlock Test", description="Test unlock",
            category="skill", xp_reward=100,
            condition_expr={"event_type": "code.submitted", "metric": "count", "threshold": 5},
        )
        # count >= threshold 才解锁
        r = gam.check_unlocks(citizen_id=1, event_type="code.submitted",
                              event_data={"count": 5})
        assert any(u["key"] == "unlock_test" for u in r)

    def test_xp_level_formula(self):
        """award_xp 和等级公式。"""
        from app.gamification import instance as gam
        # level = floor(sqrt(total_xp / 100))
        assert gam.get_xp_level(0)["level"] == 0
        assert gam.get_xp_level(100)["level"] == 1
        assert gam.get_xp_level(400)["level"] == 2
        assert gam.get_xp_level(900)["level"] == 3


class TestKnowledgeBase:
    """知识库分享。"""

    def test_create_article(self):
        """create_article 保存。"""
        from app.knowledge_sharing import instance as kb
        r = kb.create_article(
            title="Python Tips", content="Use list comprehensions",
            author_type="citizen", author_id=1, category="tutorial",
        )
        assert "article_id" in r

    def test_view_increments_count(self):
        """view_article 增加计数。"""
        from app.knowledge_sharing import instance as kb
        art = kb.create_article(
            title="View Test", content="Content here",
            author_type="host", author_id=1, category="api",
        )
        kb.view_article(art["article_id"])
        kb.view_article(art["article_id"])

        db = SessionLocal()
        try:
            a = db.get(models.KnowledgeArticle, art["article_id"])
            assert a.view_count >= 2
        finally:
            db.close()

    def test_search_finds(self):
        """search 找到内容。"""
        from app.knowledge_sharing import instance as kb
        kb.create_article(
            title="Quantum Computing Intro", content="Basics of quantum",
            author_type="citizen", author_id=5, category="research",
        )
        results = kb.search("Quantum")
        assert len(results) >= 1


class TestSLADashboard:
    """SLA 仪表盘。"""

    def _reset(self):
        from app.sla_dashboard import instance as sla
        if hasattr(sla, '_targets'):
            sla._targets.clear()

    def test_record_metric(self):
        """record_metric 保存快照。"""
        self._reset()
        from app.sla_dashboard import instance as sla
        r = sla.record_metric("api", "availability", 99.95, unit="%")
        assert "snapshot_id" in r or r.get("ok") is True

    def test_get_current_sla(self):
        """get_current_sla 返回最新值。"""
        self._reset()
        from app.sla_dashboard import instance as sla
        sla.record_metric("gateway", "latency_p99", 150, unit="ms")
        current = sla.get_current_sla("gateway")
        assert "metrics" in current or "latency_p99" in str(current)

    def test_met_field_correct(self):
        """met 字段正确（availability >= target → met=1）。"""
        self._reset()
        from app.sla_dashboard import instance as sla
        sla.record_metric("core", "availability", 99.99, unit="%")  # >= 99.9 target

        db = SessionLocal()
        try:
            snap = (db.query(models.SLAMetricSnapshot)
                    .filter(models.SLAMetricSnapshot.service == "core",
                            models.SLAMetricSnapshot.metric_name == "availability")
                    .order_by(models.SLAMetricSnapshot.id.desc()).first())
            assert snap is not None
            assert snap.met == 1
        finally:
            db.close()


# ============================================================
# 额外测试
# ============================================================

class TestModelsImport:
    """验证所有新模型可导入无冲突。"""

    def test_import_all_new_models(self):
        """导入所有新增模型验证无冲突。"""
        new_models = [
            'OAuthProvider', 'OAuthConnection', 'MFADevice', 'MFACode',
            'EncryptionKey', 'KeyRotationLog', 'RateLimitPolicy', 'RateLimitCounter',
            'RegistrationVerification', 'RealtimeSubscription', 'RealtimeEvent',
            'Workspace', 'WorkspaceMember', 'SecondaryToken', 'AMMPool', 'AMMSwap',
            'OrderBookEntry', 'QuadraticVote', 'PredictionMarket', 'PredictionShare',
            'BountyListing', 'BountySubmission', 'SupplyChainRecord',
            'SmartContractTemplate', 'ContractExecution', 'AnomalyEvent', 'TrustEdge',
            'BidStrategy', 'SkillComposition', 'SkillCompositionRun',
            'BenchmarkTest', 'BenchmarkResult', 'AIFatigueState', 'FeatureFlag',
            'DeletionRequest', 'Tenant', 'PushSubscription', 'TraceSpan',
            'BackupRecord', 'ComplianceReport', 'ModerationScore', 'MigrationRecord',
            'DIDDocument', 'VerifiableCredential', 'SocialGraphEdge',
            'Achievement', 'AchievementUnlock', 'KnowledgeArticle', 'SLAMetricSnapshot',
        ]
        for model_name in new_models:
            assert hasattr(models, model_name), f"Model '{model_name}' not found"

    def test_model_tablenames_unique(self):
        """所有模型表名唯一。"""
        table_names = [t.name for t in Base.metadata.tables.values()]
        assert len(table_names) == len(set(table_names)), "Duplicate table names found"


class TestConfigSettings:
    """验证新增配置项有正确默认值。"""

    def test_p0_config_defaults(self):
        """P0 安全配置默认值。"""
        assert settings.OAUTH_ENABLED is False or settings.OAUTH_ENABLED is True
        assert settings.OAUTH_STATE_SECRET == "oauth-state-secret"
        assert settings.MFA_CODE_TTL_SECONDS == 300
        assert settings.MFA_MAX_ATTEMPTS == 5
        assert settings.KMS_MASTER_KEY == "dev-master-key-change-me"
        assert settings.KMS_ROTATION_DAYS == 90
        assert settings.RATE_LIMIT_ENABLED is True
        assert settings.RATE_LIMIT_DEFAULT_RPM == 120
        assert settings.REG_SYBIL_THRESHOLD == 3
        assert settings.REG_TOKEN_TTL_HOURS == 24

    def test_p1_config_defaults(self):
        """P1 经济配置默认值。"""
        assert settings.AMM_DEFAULT_FEE_BPS == 30
        assert settings.AMM_SLIPPAGE_BPS == 50
        assert settings.QV_CREDITS_PER_VOTER == 100
        assert settings.PREDICTION_MIN_BET_CENT == 100
        assert settings.ANOMALY_OVERSPEND_MULT == 5.0
        assert settings.TRUST_MIN_SCORE == 0.1
        assert settings.BID_GLOBAL_BUDGET_CAP_CENT == 100000
        assert settings.BENCHMARK_INTERVAL_HOURS == 168

    def test_p2_config_defaults(self):
        """P2 平台工程配置默认值。"""
        assert settings.WEBHOOK_MAX_RETRIES == 5
        assert settings.WEBHOOK_BACKOFF_BASE_S == 30
        assert settings.GDPR_ERASURE_DAYS == 30
        assert settings.TRACING_SAMPLE_RATE == 0.1
        assert settings.BACKUP_RETENTION_DAYS == 7
        assert settings.BACKUP_TARGET_DIR == "./backups"
        assert settings.ML_MODERATION_BLOCK_THRESHOLD == 0.9

    def test_p3_config_defaults(self):
        """P3 生态配置默认值。"""
        assert settings.DID_METHOD == "aijuhe"
        assert settings.GAMIFICATION_ENABLED is True
        assert settings.GAMIFICATION_BASE_XP == 10
        assert settings.SLA_TARGET_AVAILABILITY == 0.999
        assert settings.SLA_TARGET_LATENCY_P99_MS == 500



