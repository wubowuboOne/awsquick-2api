// Account usage for gateway's auth.usage hook (account card / Usage page).
//
// Quick meters agent time in minutes per entitlement unit. The tenant data
// plane answers `getAvailableEntitlementUnits` over the same qbs-event route
// the desktop app's cloud calls use:
//   POST {tenant_url}/integration/quick-work/qbs-event
//   {eventType, eventBody:{getAvailableEntitlementUnits:{awsAccountId}}}
// → {entitlements:[{unitType, numMinsAvailable, numMinsProvisioned, …}],
//    subscriptionMetadata:{subscriptionType, freeTrialMetadata:{status, expiryDate}}}
// (verified live on a social/AIDP account; the body must be non-empty).

const UNIT_NAMES = {
  AUTOMATION_AGENT_HOUR_UNIT: 'Agent hours',
  RESEARCH_AGENT_HOUR_UNIT: 'Research hours',
}

const num = (v) => (Number.isFinite(Number(v)) ? Number(v) : 0)
const hours = (mins) => Math.round((mins / 60) * 100) / 100

/** QUICK_LITE_FREE_TRIAL → "Quick Lite (free trial)". */
export function planName(type) {
  const t = String(type || '').trim().toUpperCase()
  if (!t) return ''
  const trial = /_FREE_TRIAL$/.test(t)
  const words = t.replace(/_FREE_TRIAL$/, '').split('_').filter(Boolean)
  const name = words.map((w) => w[0] + w.slice(1).toLowerCase()).join(' ')
  return trial ? `${name} (free trial)` : name
}

function unitName(type) {
  if (UNIT_NAMES[type]) return UNIT_NAMES[type]
  const words = String(type || 'Usage').replace(/_UNIT$/, '').split('_').filter(Boolean)
  return words.map((w) => w[0] + w.slice(1).toLowerCase()).join(' ')
}

/** Quick's own usage widget resets on the 1st of next month (local time). */
function nextMonthStart(now = new Date()) {
  return new Date(now.getFullYear(), now.getMonth() + 1, 1).toISOString()
}

/**
 * Map a getAvailableEntitlementUnits response to gateway's usage shape. Units
 * drawing on the same pool (identical minutes) collapse into one window.
 */
export function usageFromEntitlements(resp, { user, now } = {}) {
  const windows = []
  const seen = new Map()
  for (const e of Array.isArray(resp?.entitlements) ? resp.entitlements : []) {
    const total = num(e.numMinsProvisioned)
    const left = num(e.numMinsAvailable)
    const key = `${total}|${left}`
    if (seen.has(key)) continue
    seen.set(key, true)
    if (total <= 0) {
      windows.push({ name: unitName(e.unitType), balance: hours(Math.max(0, left)), unit: 'h' })
      continue
    }
    const usedMins = Math.max(0, total - left)
    windows.push({
      name: unitName(e.unitType),
      used: Math.min(100, (usedMins / total) * 100),
      amount: hours(usedMins),
      limit: hours(total),
      unit: 'h',
      resetsAt: nextMonthStart(now),
    })
  }
  const sub = resp?.subscriptionMetadata
  const plan = planName(sub?.subscriptionType)
  const trial = sub?.freeTrialMetadata
  const until = trial && String(trial.status).toUpperCase() === 'ACTIVE' && num(trial.expiryDate) > 0
    ? new Date(num(trial.expiryDate)).toISOString()
    : ''
  return {
    ...(plan ? { plan } : {}),
    ...(until ? { until } : {}),
    ...(user ? { user } : {}),
    signIn: 'kept',
    windows,
  }
}

/** The account as Quick names it: the email/user tail of the actor ARN. */
function userOf(sess) {
  const arn = String(sess?.actor_arn || '')
  const m = /:user\/[^/]+\/(.+)$/.exec(arn)
  return sess?.email || (m ? m[1] : '')
}

/**
 * auth.usage for one account. `getSession` resolves the account's live Quick
 * session ({id_token, tenant_url, aws_account_id, actor_arn}).
 */
export async function quickUsage(getSession, { fetchImpl, signal } = {}) {
  try {
    const sess = await getSession()
    const tenant = String(sess?.tenant_url || '').replace(/\/+$/, '')
    if (!tenant || !sess?.id_token) return { error: 'Amazon Quick 会话不可用，无法查询用量' }
    const f = fetchImpl ?? ((...a) => globalThis.fetch(...a))
    const type = 'getAvailableEntitlementUnits'
    const r = await f(`${tenant}/integration/quick-work/qbs-event`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${sess.id_token}`, 'x-amzn-qs-internal-trace-id': crypto.randomUUID() },
      body: JSON.stringify({ eventType: type, eventBody: { [type]: { awsAccountId: sess.aws_account_id || '' } } }),
      signal,
    })
    if (r.status === 401 || r.status === 403) return { error: 'Amazon Quick 登录已失效，请重新登录', signIn: 'expired' }
    const j = await r.json().catch(() => null)
    const resp = j?.eventResponse?.[`${type}Response`]
    if (!r.ok || !resp) return { error: `用量查询失败：${String(j?.errorMessage || `HTTP ${r.status}`).slice(0, 200)}` }
    return usageFromEntitlements(resp, { user: userOf(sess) })
  } catch (e) {
    if (e?.kind === 'auth') return { error: 'Amazon Quick 登录已失效，请重新登录', signIn: 'expired' }
    return { error: `用量查询失败：${String(e?.message ?? e).slice(0, 200)}` }
  }
}
