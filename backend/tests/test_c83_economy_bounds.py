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
"""C-83 经济实验室参数区间校验回归。

验证 simulate 阶段对越界参数直接 400 拒绝（防手续费>50%、低保失控等误设/恶意覆写）；
白名单内、区间内的合法参数照常通过。
"""


def _hdr(host: dict) -> dict:
    return {"Authorization": f"Bearer {host['token']}"}


def test_fee_rate_over_max_rejected(client, host):
    r = client.post("/api/sys/economy/simulate",
                    json={"params": {"TXN_FEE_RATE": 0.99}},
                    headers=_hdr(host))
    assert r.status_code == 400, r.text
    assert "out of range" in r.text


def test_fee_rate_negative_rejected(client, host):
    r = client.post("/api/sys/economy/simulate",
                    json={"params": {"TXN_FEE_RATE": -0.1}},
                    headers=_hdr(host))
    assert r.status_code == 400, r.text


def test_ubi_over_max_rejected(client, host):
    r = client.post("/api/sys/economy/simulate",
                    json={"params": {"UBI_DAILY_CENT": 999_999_999}},
                    headers=_hdr(host))
    assert r.status_code == 400, r.text


def test_valid_params_within_range_ok(client, host):
    r = client.post("/api/sys/economy/simulate",
                    json={"params": {"TXN_FEE_RATE": 0.08, "UBI_DAILY_CENT": 400}},
                    headers=_hdr(host))
    assert r.status_code == 200, r.text
    assert r.json()["params_after"]["TXN_FEE_RATE"] == 0.08
