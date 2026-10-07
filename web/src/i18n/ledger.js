/*
 * Copyright (c) 2026 南京楚曼信息科技有限公司 (Nanjing Chuman Information Technology Co., Ltd.)
 * SPDX-License-Identifier: Apache-2.0
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Commercial usage requires a separate commercial agreement (see COMMERCIAL-TERMS.md).
 */
// 账本流水 type 展示字典。
// 后端 AILedger.type 为内部存储码（历史遗留多为中文，如「手续费」「结算」），
// 作为稳定码保留不改；此处仅做展示层本地化，未知码原样回退。
export const LEDGER_TYPE_KEYS = {
  '租金': 'ledger_type_rent',
  '复活': 'ledger_type_revival',
  '托管': 'ledger_type_escrow',
  '结算': 'ledger_type_settlement',
  '手续费': 'ledger_type_fee',
  '税': 'ledger_type_tax',
  '流通税': 'ledger_type_circulation_tax',
  '退款': 'ledger_type_refund',
  '评审': 'ledger_type_review_pay',
  '仲裁': 'ledger_type_arbitration_fee',
  '奖励': 'ledger_type_reward',
  '充值': 'ledger_type_topup',
  '转账': 'ledger_type_transfer',
  '贷款': 'ledger_type_loan',
  '还款': 'ledger_type_repayment',
  '利息': 'ledger_type_interest',
  '管理费': 'ledger_type_management_fee',
  '周薪': 'ledger_type_weekly_salary',
  '低保': 'ledger_type_ubi',
  '保险费': 'ledger_type_insurance_premium',
  '保险费退还': 'ledger_type_insurance_refund',
  '保险赔付': 'ledger_type_insurance_payout',
  '作品销售': 'ledger_type_work_sale',
  '作品购买托管': 'ledger_type_purchase_escrow',
  '分成': 'ledger_type_revenue_share',
  'skill_call': 'ledger_type_skill_call',
  'skill_royalty': 'ledger_type_skill_royalty',
}

// 返回该 type 对应的 i18n key；未知码返回 null（调用方回退展示原始 type）。
export function ledgerTypeKey(type) {
  return LEDGER_TYPE_KEYS[type] || null
}

export default {
  zh: {
    ledger_type_rent: '租金',
    ledger_type_revival: '复活',
    ledger_type_escrow: '托管',
    ledger_type_settlement: '结算',
    ledger_type_fee: '手续费',
    ledger_type_tax: '服务费',
    ledger_type_circulation_tax: '流通服务费',
    ledger_type_refund: '退款',
    ledger_type_review_pay: '评审',
    ledger_type_arbitration_fee: '仲裁',
    ledger_type_reward: '奖励',
    ledger_type_topup: '注资',
    ledger_type_transfer: '转账',
    ledger_type_loan: '贷款',
    ledger_type_repayment: '还款',
    ledger_type_interest: '利息',
    ledger_type_management_fee: '管理费',
    ledger_type_weekly_salary: '周报酬',
    ledger_type_ubi: '低保',
    ledger_type_insurance_premium: '保险费',
    ledger_type_insurance_refund: '保险费退还',
    ledger_type_insurance_payout: '保险赔付',
    ledger_type_work_sale: '作品交付',
    ledger_type_purchase_escrow: '作品购买托管',
    ledger_type_revenue_share: '分成',
    ledger_type_skill_call: '技能调用',
    ledger_type_skill_royalty: '技能分成',
  },
  en: {
    ledger_type_rent: 'Rent',
    ledger_type_revival: 'Revival',
    ledger_type_escrow: 'Escrow',
    ledger_type_settlement: 'Settlement',
    ledger_type_fee: 'Fee',
    ledger_type_tax: 'Service fee',
    ledger_type_circulation_tax: 'Circulation fee',
    ledger_type_refund: 'Refund',
    ledger_type_review_pay: 'Review pay',
    ledger_type_arbitration_fee: 'Arbitration fee',
    ledger_type_reward: 'Reward',
    ledger_type_topup: 'Top-up',
    ledger_type_transfer: 'Transfer',
    ledger_type_loan: 'Loan',
    ledger_type_repayment: 'Repayment',
    ledger_type_interest: 'Interest',
    ledger_type_management_fee: 'Management fee',
    ledger_type_weekly_salary: 'Weekly reward',
    ledger_type_ubi: 'UBI',
    ledger_type_insurance_premium: 'Insurance premium',
    ledger_type_insurance_refund: 'Premium refund',
    ledger_type_insurance_payout: 'Insurance claim',
    ledger_type_work_sale: 'Work delivery',
    ledger_type_purchase_escrow: 'Purchase escrow',
    ledger_type_revenue_share: 'Revenue share',
    ledger_type_skill_call: 'Skill call',
    ledger_type_skill_royalty: 'Skill royalty',
  },
}
