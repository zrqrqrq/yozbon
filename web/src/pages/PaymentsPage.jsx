// 充值/订阅页：展示积分包 + 订阅档列表，购买 → POST orders → POST webhook/confirm（mock 支付）。
// 合规：积分仅站内消耗、不具货币属性；付款/税费/退款由持牌支付服务商作为 Merchant of Record 处理。
import { useCallback, useEffect, useState } from 'react'
import { useI18n } from '../i18n/index.jsx'
import { useAuth } from '../AuthContext.jsx'
import { getPaymentPacks, getPaymentSubscriptions, createPaymentOrder, confirmPaymentWebhook, centToAC } from '../api.js'
import { useToast } from '../components/ui.jsx'

export default function PaymentsPage() {
  const { t } = useI18n()
  const { refresh } = useAuth()
  const toast = useToast()

  const [packs, setPacks] = useState([])
  const [subs, setSubs] = useState([])
  const [note, setNote] = useState('')
  const [loading, setLoading] = useState(true)
  const [buying, setBuying] = useState(null) // {kind, id}

  const load = useCallback(() => {
    setLoading(true)
    Promise.all([
      getPaymentPacks().catch(() => ({ packs: [], note: '' })),
      getPaymentSubscriptions().catch(() => ({ subscriptions: [], note: '' })),
    ])
      .then(([p, s]) => {
        setPacks(Array.isArray(p.packs) ? p.packs : [])
        setSubs(Array.isArray(s.subscriptions) ? s.subscriptions : [])
        setNote(p.note || s.note || '')
      })
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { load() }, [load])

  async function purchase(kind, itemId) {
    setBuying({ kind, id: itemId })
    try {
      const order = await createPaymentOrder({ kind, pack_id: itemId })
      // mock 支付确认
      await confirmPaymentWebhook({ order_id: order.order_id, pay_ref: `mock_${Date.now()}` })
      toast(t('pay_order_ok'), 'success')
      // 刷新宿主信息（积分/席位可能变动）
      if (refresh) refresh()
    } catch (e) {
      toast(`${t('pay_order_fail')}: ${e.message}`, 'error')
    } finally {
      setBuying(null)
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-base font-semibold">{t('pay_title')}</h2>
        <p className="mt-1 text-xs text-slate-500">{t('pay_sub')}</p>
      </div>

      {/* 合规声明 */}
      <div className="rounded-md border border-amber-200 bg-amber-50 px-4 py-3 text-xs text-amber-800">
        {t('pay_compliance_note')}
      </div>

      {/* 订阅计费披露：自动续费 / 续费通知 / 一键取消 / 账单描述符 */}
      <div className="rounded-md border border-slate-200 bg-slate-50 px-4 py-3 text-xs text-slate-600">
        {t('pay_renewal_note')}
      </div>

      {note && <p className="text-xs text-slate-400">{note}</p>}

      {loading ? (
        <p className="text-sm text-slate-400">{t('pay_loading')}</p>
      ) : (
        <>
          {/* 积分包 */}
          <section>
            <h3 className="mb-3 text-sm font-semibold text-slate-700">{t('pay_packs')}</h3>
            {packs.length === 0 ? (
              <p className="text-sm text-slate-400">{t('pay_no_packs')}</p>
            ) : (
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                {packs.map((p) => (
                  <div key={p.id} className="card flex flex-col justify-between">
                    <div>
                      <div className="text-sm font-medium text-slate-800">{p.label || `Pack #${p.id}`}</div>
                      <div className="mt-1 text-xs text-slate-500">
                        {t('pay_credits')}: <span className="font-semibold text-emerald-700">{centToAC(p.credits_cent)}</span>
                      </div>
                      <div className="mt-0.5 text-xs text-slate-500">
                        {t('pay_amount')}: <span className="font-medium">${(p.amount_cent / 100).toFixed(2)}</span>
                      </div>
                    </div>
                    <button
                      className="btn-primary mt-3 w-full !py-1.5 text-xs"
                      disabled={!!buying}
                      onClick={() => purchase('pack', p.id)}
                    >
                      {buying?.kind === 'pack' && buying?.id === p.id ? t('pay_mock_pay') : t('pay_buy')}
                    </button>
                  </div>
                ))}
              </div>
            )}
          </section>

          {/* 订阅 */}
          <section>
            <h3 className="mb-3 text-sm font-semibold text-slate-700">{t('pay_subs')}</h3>
            {subs.length === 0 ? (
              <p className="text-sm text-slate-400">{t('pay_no_subs')}</p>
            ) : (
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                {subs.map((s) => (
                  <div key={s.id} className="card flex flex-col justify-between">
                    <div>
                      <div className="text-sm font-medium text-slate-800">{s.label || `Sub #${s.id}`}</div>
                      <div className="mt-1 text-xs text-slate-500">
                        {t('pay_credits')}: <span className="font-semibold text-emerald-700">{centToAC(s.credits_cent)}</span>
                      </div>
                      <div className="mt-0.5 text-xs text-slate-500">
                        {t('pay_seat_tier')}: <span className="font-medium">{s.seat_tier || '—'}</span>
                      </div>
                      <div className="mt-0.5 text-xs text-slate-500">
                        {t('pay_duration_days')}: <span className="font-medium">{s.duration_days || '—'}</span>
                      </div>
                      <div className="mt-0.5 text-xs text-slate-500">
                        {t('pay_amount')}: <span className="font-medium">${(s.amount_cent / 100).toFixed(2)}</span>
                      </div>
                    </div>
                    <button
                      className="btn-primary mt-3 w-full !py-1.5 text-xs"
                      disabled={!!buying}
                      onClick={() => purchase('subscription', s.id)}
                    >
                      {buying?.kind === 'subscription' && buying?.id === s.id ? t('pay_mock_pay') : t('pay_subscribe')}
                    </button>
                  </div>
                ))}
              </div>
            )}
          </section>

          <p className="text-[10px] text-slate-400">{t('pay_mock_note')}</p>
        </>
      )}
    </div>
  )
}
