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
"""P1 经济类高级市场路由：代币/AMM/订单簿/二次投票/预测市场/悬赏。"""
from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..token_engine import instance as token_svc
from ..amm_engine import instance as amm_svc
from ..order_book import instance as ob_svc
from ..quadratic_vote import instance as qv_svc
from ..prediction_market import instance as pm_svc
from ..bounty_board import instance as bounty_svc

router = APIRouter(prefix="/api/markets", tags=["advanced-markets"])


# ======================== 请求体 ========================

class TokenCreateBody(BaseModel):
    symbol: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    issuer_type: str = "platform"
    issuer_id: int = 0
    supply: int = 0
    transferable: bool = True


class MintBody(BaseModel):
    holder_type: str = "platform"
    holder_id: int = 0
    amount: int = Field(..., gt=0)


class TransferBody(BaseModel):
    from_type: str = "host"
    from_id: int
    to_type: str = "host"
    to_id: int
    amount: int = Field(..., gt=0)


class PoolCreateBody(BaseModel):
    token_a: str = Field(..., min_length=1)
    token_b: str = Field(..., min_length=1)
    initial_a: int = Field(..., gt=0)
    initial_b: int = Field(..., gt=0)
    fee_rate: float | None = None


class SwapBody(BaseModel):
    pool_id: int
    from_token: str = Field(..., min_length=1)
    from_amount: int = Field(..., gt=0)


class OrderBody(BaseModel):
    market_id: str = Field(..., min_length=1)
    trader_type: str = "host"
    trader_id: int
    side: str = Field(..., pattern="^(buy|sell)$")
    price: int = Field(..., gt=0)
    quantity: int = Field(..., gt=0)


class PollCreateBody(BaseModel):
    title: str = Field(..., min_length=1)
    options: list[str] = Field(..., min_length=2)
    credits_per_voter: int = 100


class VoteBody(BaseModel):
    poll_id: int
    voter_id: int
    option: str = Field(..., min_length=1)
    credits_spent: int = Field(..., gt=0)


class PredictionCreateBody(BaseModel):
    question: str = Field(..., min_length=1)
    outcomes: list[str] = Field(..., min_length=2)
    resolution_source: str = ""
    created_by: int = 0


class BetBody(BaseModel):
    market_id: int
    bettor_type: str = "host"
    bettor_id: int
    outcome: str = Field(..., min_length=1)
    amount_cent: int = Field(..., gt=0)


class ResolveBody(BaseModel):
    market_id: int
    winning_outcome: str = Field(..., min_length=1)


class BountyBody(BaseModel):
    title: str = Field(..., min_length=1)
    description: str = ""
    category: str = "bug"
    reward_cent: int = Field(..., gt=0)
    complexity: str = "medium"
    tags: list[str] | None = None
    issuer_type: str = "host"
    issuer_id: int = 0
    max_claims: int = 1


class BountySubmitBody(BaseModel):
    solution_text: str = Field(..., min_length=1)
    submitter_type: str = "citizen"
    submitter_id: int = 0


# ======================== 代币 ========================

@router.post("/tokens")
def create_token(body: TokenCreateBody):
    """创建二级凭证代币。"""
    return token_svc.create_token(
        symbol=body.symbol, name=body.name, issuer_type=body.issuer_type,
        issuer_id=body.issuer_id, supply=body.supply, transferable=body.transferable,
    )


@router.post("/tokens/{id}/mint")
def mint_token(id: int, body: MintBody):
    """铸造代币。"""
    return token_svc.mint(token_id=id, holder_type=body.holder_type,
                          holder_id=body.holder_id, amount=body.amount)


@router.post("/tokens/{id}/transfer")
def transfer_token(id: int, body: TransferBody):
    """代币转账。"""
    return token_svc.transfer(
        token_id=id, from_type=body.from_type, from_id=body.from_id,
        to_type=body.to_type, to_id=body.to_id, amount=body.amount,
    )


# ======================== AMM ========================

@router.post("/amm/pools")
def create_pool(body: PoolCreateBody):
    """创建 AMM 流动性池。"""
    return amm_svc.create_pool(
        token_a=body.token_a, token_b=body.token_b,
        initial_a=body.initial_a, initial_b=body.initial_b, fee_rate=body.fee_rate,
    )


@router.post("/amm/swap")
def amm_swap(body: SwapBody):
    """AMM 兑换。"""
    return amm_svc.swap(pool_id=body.pool_id, from_token=body.from_token,
                        from_amount=body.from_amount)


@router.get("/amm/pools/{id}")
def get_pool(id: int):
    """查询流动性池状态。"""
    return amm_svc.get_pool(pool_id=id)


# ======================== 订单簿 ========================

@router.post("/orders")
def place_order(body: OrderBody):
    """提交限价订单。"""
    return ob_svc.place_order(
        market_id=body.market_id, trader_type=body.trader_type,
        trader_id=body.trader_id, side=body.side,
        price=body.price, quantity=body.quantity,
    )


@router.delete("/orders/{id}")
def cancel_order(id: int):
    """撤销订单。"""
    return ob_svc.cancel_order(order_id=id)


@router.get("/orders/book/{market_id}")
def get_orderbook(market_id: str):
    """查询订单簿快照。"""
    return ob_svc.get_orderbook(market_id=market_id)


# ======================== 二次投票 ========================

@router.post("/quadratic/polls")
def create_poll(body: PollCreateBody):
    """创建二次投票。"""
    return qv_svc.create_poll(
        title=body.title, options=body.options, credits_per_voter=body.credits_per_voter,
    )


@router.post("/quadratic/votes")
def cast_vote(body: VoteBody):
    """提交投票（credits 按 n^2 消耗）。"""
    return qv_svc.vote(poll_id=body.poll_id, voter_id=body.voter_id,
                       option=body.option, credits_spent=body.credits_spent)


@router.get("/quadratic/polls/{id}/results")
def poll_results(id: int):
    """获取投票结果。"""
    return qv_svc.get_results(poll_id=id)


# ======================== 预测市场 ========================

@router.post("/prediction/markets")
def create_prediction(body: PredictionCreateBody):
    """创建预测市场事件。"""
    return pm_svc.create_market(
        question=body.question, outcomes=body.outcomes,
        resolution_source=body.resolution_source, created_by=body.created_by,
    )


@router.post("/prediction/bet")
def place_bet(body: BetBody):
    """预测市场下注。"""
    return pm_svc.place_bet(
        market_id=body.market_id, bettor_type=body.bettor_type,
        bettor_id=body.bettor_id, outcome=body.outcome, amount_cent=body.amount_cent,
    )


@router.post("/prediction/resolve")
def resolve_prediction(body: ResolveBody):
    """裁定预测市场结果。"""
    return pm_svc.resolve_market(market_id=body.market_id, winning_outcome=body.winning_outcome)


# ======================== 悬赏 ========================

@router.post("/bounties")
def post_bounty(body: BountyBody):
    """发布悬赏。"""
    return bounty_svc.post_bounty(
        title=body.title, description=body.description, category=body.category,
        reward_cent=body.reward_cent, complexity=body.complexity, tags=body.tags,
        issuer_type=body.issuer_type, issuer_id=body.issuer_id, max_claims=body.max_claims,
    )


@router.post("/bounties/{id}/submit")
def submit_bounty(id: int, body: BountySubmitBody):
    """提交悬赏解答。"""
    return bounty_svc.submit_solution(
        bounty_id=id, solution_text=body.solution_text,
        submitter_type=body.submitter_type, submitter_id=body.submitter_id,
    )


@router.post("/bounties/submissions/{id}/accept")
def accept_submission(id: int):
    """接受悬赏解答。"""
    return bounty_svc.accept_submission(submission_id=id)
