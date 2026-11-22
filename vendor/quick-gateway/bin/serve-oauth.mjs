#!/usr/bin/env node
// Quick 反代网关（多账号 + 密钥 + 调用账本 + 额度熔断）
//
//   node bin/serve-oauth.mjs
//   PORT=3426 DATA_DIR=... node bin/serve-oauth.mjs
//
// 鉴权：/v1/* 支持  Authorization: Bearer <key>  或  ?key=<key>
//       未创建任何密钥时放行（本机开发模式）
// 接口：/v1/models /v1/chat/completions
//       /accounts  /usage  /stats  /calls  /keys  /login  /login/submit  /health
import { createServer } from 'node:http'
import { readFileSync, writeFileSync, appendFileSync, mkdirSync, existsSync } from 'node:fs'
import { HttpsProxyAgent } from 'https-proxy-agent'
import { randomUUID, randomBytes } from 'node:crypto'
import { join } from 'node:path'
import { handleChatCompletions, handleModels } from '../lib/handler.mjs'
import { createSessionStore } from '../lib/sessions.mjs'
import { quickUsage } from '../lib/usage.mjs'
import {
  createSessionStore as createOAuthStore, startOAuthLogin, createDirectBackend,
  keyForSession, defaultStoreDir, takeDiscovery,
} from '../lib/oauth.mjs'

const PORT = Number(process.env.PORT || 3426)
const HOST = process.env.HOST || '127.0.0.1'
const DIR = process.env.GATEWAY_DIR || defaultStoreDir()
const DATA = process.env.DATA_DIR || join(process.cwd(), 'data')
const MAX_FAILOVER = Number(process.env.MAX_FAILOVER || 3)
const QUOTA_STOP = Number(process.env.QUOTA_STOP || 95)   // 额度用满百分比后停用
const QUOTA_TTL = 10 * 60 * 1000

mkdirSync(DATA, { recursive: true })
const KEYS_FILE = join(DATA, 'keys.json')
const CALLS_FILE = join(DATA, 'calls.jsonl')

/* 代理：读项目根的 config.json，proxy.enabled 为真时按账号轮询 */
const CONFIG_CANDIDATES = [
  process.env.CONFIG_FILE,
  new URL('../../../config.json', import.meta.url),
  new URL('../../config.json', import.meta.url),
  join(process.cwd(), 'config.json'),
  join(process.cwd(), '../../config.json'),
].filter(Boolean)
let CONFIG_FILE = null
let proxyAgents = []
function readConfig() {
  for (const c of CONFIG_CANDIDATES) {
    try { const t = readFileSync(c, 'utf8'); CONFIG_FILE = c; return JSON.parse(t) } catch {}
  }
  return null
}
function reloadProxies() {
  try {
    const cfg = readConfig()
    if (!cfg) throw new Error('config.json 未找到')
    const g = cfg.proxy?.gateway || {}
    const list = (g.enabled ? g.list || [] : []).filter(p => p.url && p.enabled !== false)
    proxyAgents = list.map(p => ({ url: p.url, agent: new HttpsProxyAgent(p.url) }))
    console.log(`[proxy] 出站代理 ${proxyAgents.length} 个`)
  } catch { proxyAgents = [] }
}
reloadProxies()
let proxyRR = 0

const oauthStore = createOAuthStore({ dir: DIR })
const sessions = createSessionStore()
let loginState = { running: false, url: null, startedAt: 0, error: null }
let flowRef = null
let rr = 0
const health = new Map()      // key -> {fails, lastUsed, nextTry, dead}
const quotaCache = new Map()  // key -> {at, usage}

/* ---------------- 密钥 ---------------- */
function readKeys() {
  try { return JSON.parse(readFileSync(KEYS_FILE, 'utf8')) } catch { return [] }
}
function writeKeys(list) { writeFileSync(KEYS_FILE, JSON.stringify(list, null, 2)) }
function newKey() { return 'qk_' + randomBytes(24).toString('base64url') }
function findKey(raw) {
  const k = (raw || '').trim()
  if (!k) return null
  return readKeys().find(x => x.key === k) || null
}

/* ---------------- 调用账本 ---------------- */
function logCall(rec) {
  try { appendFileSync(CALLS_FILE, JSON.stringify(rec) + '\n') } catch {}
}
function readCalls(limit = 100, sinceMs = 0) {
  if (!existsSync(CALLS_FILE)) return []
  const lines = readFileSync(CALLS_FILE, 'utf8').trim().split('\n').slice(-5000)
  const out = []
  for (const l of lines) {
    if (!l) continue
    try { const r = JSON.parse(l); if (r.ts >= sinceMs) out.push(r) } catch {}
  }
  return out.slice(-limit).reverse()
}

/* ---------------- 账号 ---------------- */
function accountKeys() { return oauthStore.list() }
function healthOf(k) {
  if (!health.has(k)) health.set(k, { fails: 0, lastUsed: 0, nextTry: 0, dead: false })
  return health.get(k)
}
function backendFor(k) {
  const be = createDirectBackend({
    load: async () => oauthStore.get(k),
    persist: async (s) => oauthStore.set(k, s),
    log: (m) => console.log(`[${k.slice(-6)}]`, m),
  })
  if (proxyAgents.length) {
    const pick = proxyAgents[proxyRR++ % proxyAgents.length]
    be.agent = pick.agent
  }
  return be
}
async function quotaOf(k) {
  const c = quotaCache.get(k)
  if (c && Date.now() - c.at < QUOTA_TTL) return c.usage
  const u = await quickUsage(async () => backendFor(k).session()).catch(e => ({ error: String(e?.message || e) }))
  quotaCache.set(k, { at: Date.now(), usage: u })
  return u
}
function quotaPct(u) {
  const w = (u?.windows || [])[0]
  return w && w.limit ? Math.min(100, (w.used / w.limit) * 100) : 0
}
function pickAccount() {
  const keys = accountKeys()
  if (!keys.length) return null
  const now = Date.now()
  for (let i = 0; i < keys.length; i++) {
    const k = keys[(rr + i) % keys.length]
    const h = healthOf(k)
    if (h.dead) continue
    if (h.nextTry && h.nextTry > now) continue
    rr = (rr + i + 1) % keys.length
    return k
  }
  return null
}

/* ---------------- 工具 ---------------- */
function json(res, status, obj, extra = {}) {
  const b = Buffer.from(JSON.stringify(obj))
  res.writeHead(status, { 'content-type': 'application/json', 'content-length': b.length, ...extra })
  res.end(b)
}
function authed(req, url) {
  const keys = readKeys()
  if (!keys.length) return { ok: true, dev: true }
  const raw = (req.headers.authorization || '').replace(/^Bearer\s+/i, '')
    || req.headers['x-api-key'] || url.searchParams.get('key') || ''
  const hit = findKey(raw)
  if (!hit) return { ok: false }
  hit.last_used = Date.now(); hit.calls = (hit.calls || 0) + 1
  writeKeys(keys)
  return { ok: true, key: hit }
}

async function startLogin() {
  if (loginState.running) return loginState
  loginState = { running: true, url: null, startedAt: Date.now(), error: null }
  const disc = takeDiscovery(defaultStoreDir())
  const flow = await startOAuthLogin({ log: (m) => console.log('[login]', m), discovery: disc, dir: defaultStoreDir() })
  flowRef = flow
  loginState.url = flow.url
  flow.done.then((sess) => {
    const key = keyForSession(sess)
    oauthStore.set(key, sess)
    loginState = { running: false, url: null, startedAt: 0, error: null }
    console.log('[login] ok', sess.email)
  }).catch((e) => {
    loginState = { running: false, url: null, startedAt: 0, error: String(e?.message || e) }
    console.log('[login] failed:', e?.message || e)
  })
  return loginState
}

async function chatWithPool(body, effortHeader, signal) {
  const tried = []
  for (let attempt = 0; attempt < MAX_FAILOVER; attempt++) {
    const key = pickAccount()
    if (!key) {
      const all = accountKeys().map(k => {
        const h = healthOf(k); const a = oauthStore.get(k) || {}
        return { email: a.email || k.slice(-6), fails: h.fails,
                 reason: h.dead ? '已熔断' : (h.nextTry > Date.now() ? `冷却至 ${new Date(h.nextTry).toLocaleTimeString('zh-CN',{hour12:false})}` : '未知') }
      })
      return { response: jsonResponse(503, { error: {
        message: all.length ? '没有可用账号：' + all.map(a => `${a.email}（${a.reason}）`).join('，') : '账号池为空',
        accounts: all,
      } }) }
    }
    const acct = oauthStore.get(key) || {}
    const u = await quotaOf(key)
    if (quotaPct(u) >= QUOTA_STOP) {
      healthOf(key).dead = true
      console.log(`[pool] ${acct.email} 额度用尽，停用`)
      continue
    }
    const t0 = Date.now()
    let out
    try {
      out = await handleChatCompletions({
        body, outpostMgr: backendFor(key), sessions,
        options: { log: console.log }, effortHeader, signal,
      })
    } catch (e) {
      const h = healthOf(key)
      h.fails += 1; h.dead = true; h.nextTry = Date.now() + Math.min(15 * 60000, 30000 * 2 ** (h.fails - 1))
      logCall({ ts: Date.now(), account: acct.email || key.slice(-8), status: 500, ms: Date.now() - t0, error: String(e?.message || e).slice(0, 200) })
      continue
    }
    const ms = Date.now() - t0
    const h = healthOf(key); h.lastUsed = Date.now()
    const retryable = [401, 403, 429].includes(out.status) || out.status >= 500
    if (retryable) {
      h.fails += 1
      h.dead = h.fails >= 2
      h.nextTry = Date.now() + Math.min(15 * 60000, 30000 * 2 ** (h.fails - 1))
      logCall({ ts: Date.now(), account: acct.email || key.slice(-8), status: out.status, ms })
      continue
    }
    h.fails = 0; h.dead = false; h.nextTry = 0
    tried.push(key)
    return { response: out, account: acct.email || key.slice(-8), ms }
  }
  return { response: jsonResponse(502, { error: { message: '所有账号均失败：' + tried.length + ' 次尝试' } }) }
}

function jsonResponse(status, obj) {
  return new Response(JSON.stringify(obj), { status, headers: { 'content-type': 'application/json' } })
}

/* ---------------- 服务 ---------------- */
const server = createServer(async (req, res) => {
  try {
    const url = new URL(req.url, 'http://local')
    const path = url.pathname.replace(/\/+$/, '') || '/'

    if (path === '/reload') { reloadProxies(); return json(res, 200, { ok: true, proxies: proxyAgents.length }) }

    if (path === '/health') return json(res, 200, { ok: true, accounts: accountKeys().length, keys: readKeys().length })

    /* --- 密钥管理（本机管理，不走 key 鉴权）--- */
    if (path === '/keys' && req.method === 'GET') {
      return json(res, 200, { keys: readKeys().map(k => ({
        id: k.id, name: k.name, prefix: k.key.slice(0, 10) + '…',
        created: k.created, last_used: k.last_used || null, calls: k.calls || 0,
      })) })
    }
    if (path === '/keys' && req.method === 'POST') {
      let raw = ''; for await (const c of req) raw += c
      let name = 'default'
      try { name = (JSON.parse(raw || '{}').name || 'default').slice(0, 40) } catch {}
      const list = readKeys()
      const rec = { id: randomUUID(), name, key: newKey(), created: Date.now(), calls: 0 }
      list.push(rec); writeKeys(list)
      return json(res, 200, { ok: true, id: rec.id, name, key: rec.key })   // 只在创建时返回明文
    }
    if (path.startsWith('/keys/') && req.method === 'DELETE') {
      const id = path.split('/').pop()
      const list = readKeys().filter(k => k.id !== id)
      writeKeys(list)
      return json(res, 200, { ok: true })
    }

    /* --- 账号/额度/统计/账本 --- */
    if (path === '/accounts') {
      const list = []
      for (const k of accountKeys()) {
        const a = oauthStore.get(k) || {}
        const h = healthOf(k)
        const u = await quotaOf(k)
        list.push({
          key: `…${k.slice(-8)}`, email: a.email || '', ws_host: a.ws_host || '',
          expires_at: a.usable_until || null, fails: h.fails, dead: h.dead,
          next_try: h.nextTry || null, last_used: h.lastUsed || null,
          quota: u?.windows?.[0] ? { used: u.windows[0].used, limit: u.windows[0].limit } : null,
          plan: u?.plan || null, until: u?.until || null,
        })
      }
      return json(res, 200, { count: list.length, accounts: list })
    }
    if (path === '/usage') {
      const out = []
      for (const k of accountKeys()) {
        const a = oauthStore.get(k) || {}
        out.push({ key: `…${k.slice(-8)}`, email: a.email || '', usage: await quotaOf(k) })
      }
      return json(res, 200, { accounts: out })
    }
    if (path === '/calls') {
      const limit = Math.min(parseInt(url.searchParams.get('limit') || '100', 10), 500)
      return json(res, 200, { calls: readCalls(limit) })
    }
    if (path === '/stats') {
      const calls = readCalls(5000, Date.now() - 7 * 24 * 3600 * 1000)
      const ok = calls.filter(c => c.status === 200)
      const today = new Date(); today.setHours(0, 0, 0, 0)
      const todayCalls = calls.filter(c => c.ts >= today.getTime())
      const sum = a => a.reduce((x, y) => x + (y || 0), 0)
      const byHour = new Array(24).fill(0), costHour = new Array(24).fill(0)
      for (const c of calls) {
        const h = new Date(c.ts).getHours()
        byHour[h]++; costHour[h] += c.cost || 0
      }
      const byModel = {}, byAccount = {}
      for (const c of calls) {
        const m = c.model || '—'
        byModel[m] = byModel[m] || { calls: 0, cost: 0 }
        byModel[m].calls++; byModel[m].cost += c.cost || 0
        const a = c.account || '—'
        byAccount[a] = byAccount[a] || { calls: 0, cost: 0, ms: 0 }
        byAccount[a].calls++; byAccount[a].cost += c.cost || 0; byAccount[a].ms += c.ms || 0
      }
      return json(res, 200, {
        calls: calls.length, ok: ok.length, errors: calls.length - ok.length,
        avg_ms: ok.length ? Math.round(sum(ok.map(c => c.ms)) / ok.length) : 0,
        tokens: sum(calls.map(c => c.total_tokens)),
        cost: Number(sum(calls.map(c => c.cost)).toFixed(4)),
        today: { calls: todayCalls.length, cost: Number(sum(todayCalls.map(c => c.cost)).toFixed(4)) },
        by_hour: byHour, cost_hour: costHour.map(x => Number(x.toFixed(4))),
        by_model: byModel, by_account: byAccount,
        per_account: Object.entries(byAccount).map(([email, v]) => ({
          email, calls: v.calls, cost: Number(v.cost.toFixed(4)),
          avg_ms: v.calls ? Math.round(v.ms / v.calls) : 0,
        })),
      })
    }

    /* --- 登录 --- */
    if (path === '/login') return json(res, 200, await startLogin())
    if (path === '/login/submit' && req.method === 'POST') {
      let raw = ''; for await (const c of req) raw += c
      let pasted = ''
      try { pasted = JSON.parse(raw || '{}').url || '' } catch { pasted = raw.trim() }
      if (!flowRef?.submit) return json(res, 400, { ok: false, error: 'no login in progress' })
      try {
        const sess = await flowRef.submit(pasted)
        const key = keyForSession(sess)
        oauthStore.set(key, sess)
        loginState = { running: false, url: null, startedAt: 0, error: null }
        return json(res, 200, { ok: true, email: sess.email || '' })
      } catch (e) { return json(res, 400, { ok: false, error: String(e?.message || e) }) }
    }
    if (path === '/login/status') {
      return json(res, 200, { logged_in: accountKeys().length > 0, accounts: accountKeys().length, login: loginState })
    }

    /* --- OpenAI 兼容 --- */
    if (req.method === 'GET' && /\/models$/.test(path)) {
      const auth = authed(req, url); if (!auth.ok) return json(res, 401, { error: { message: 'invalid api key' } })
      const out = handleModels()
      res.writeHead(out.status, Object.fromEntries(out.headers))
      if (!out.body) return res.end()
      for await (const c of out.body) res.write(c)
      return res.end()
    }
    if (req.method === 'POST' && /\/chat\/completions$/.test(path)) {
      const auth = authed(req, url); if (!auth.ok) return json(res, 401, { error: { message: 'invalid api key' } })
      let raw = ''; for await (const c of req) raw += c
      let body
      try { body = JSON.parse(raw || '{}') } catch { return json(res, 400, { error: { message: 'body must be JSON' } }) }
      const beh = (() => { try { return JSON.parse(readFileSync(CONFIG_CANDIDATES.find(c => { try { readFileSync(c); return true } catch { return false } }), 'utf8')).gateway_behavior || {} } catch { return {} } })()
      if (!body.model && beh.default_model) body.model = beh.default_model
      const ac = new AbortController()
      res.on('close', () => { if (!res.writableEnded) ac.abort() })
      const { response: out, account, ms } = await chatWithPool(body, req.headers['x-gateway-effort'], ac.signal)
      const headers = Object.fromEntries(out.headers || [])
      if (account) headers['x-quick-account'] = account
      res.writeHead(out.status, headers)
      if (!out.body) return res.end()
      const chunks = []
      for await (const c of out.body) { chunks.push(c); res.write(c) }
      // 记账
      try {
        const text = Buffer.concat(chunks.map(c => Buffer.from(c))).toString('utf8')
        const j = JSON.parse(text.replace(/^data: /gm, '').split('\n').filter(Boolean)[0] || '{}')
        logCall({
          ts: Date.now(), account: account || '', status: out.status, ms,
          model: j.model || body.model || '', prompt_tokens: j.usage?.prompt_tokens || 0,
          completion_tokens: j.usage?.completion_tokens || 0, total_tokens: j.usage?.total_tokens || 0,
          cost: j.usage?.cost || 0, resolved: j.usage?.resolved_model_id || '',
        })
      } catch {}
      return res.end()
    }

    json(res, 404, { error: { message: `not found: ${req.method} ${path}` } })
  } catch (e) {
    if (!res.headersSent) json(res, 500, { error: { message: String(e?.message || e) } })
    else res.end()
  }
})

server.listen(PORT, HOST, () => {
  console.log(`[proxy] http://${HOST}:${PORT}  账号 ${accountKeys().length}  密钥 ${readKeys().length}  (${DIR})`)
})
