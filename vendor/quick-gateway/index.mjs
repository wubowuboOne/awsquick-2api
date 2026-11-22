/**
 * gateway-aws-quick — your Amazon Quick (AWS Quick desktop) login as a gateway / OpenCode provider.
 *
 * gateway sends this provider OpenAI Chat Completions (npm "@ai-sdk/openai-compatible");
 * the loader's fetch answers them by driving the same bidi WebSocket the desktop
 * client uses (see lib/quickws.mjs + lib/handler.mjs).
 *
 * Sign-in methods (all keyless):
 *   1. Browser OAuth, two flavours (no desktop app): the plugin replays the
 *      app's own OIDC+PKCE flow — gateway opens quick.aws.com/sn itself, or
 *      the "copy sign-in link" flavour only shows the URL plus a box for the
 *      browser's final address — the plugin catches the localhost callback,
 *      registers a dynamic OIDC client, exchanges the code, reads user-info —
 *      then chats over the stored token directly with auto-refresh.
 *   2. Local credentials: on Windows reads the desktop app's stored session
 *      straight from Credential Manager (refresh via OIDC, written back so the
 *      app keeps working); otherwise spawns the bundled agent's
 *      `quickwork-agent --outpost-only` and reuses its sign-in via
 *      /auth/sncs-session. Needs the desktop app installed + signed in.
 *
 * Options (gateway plugin options aws-quick '{…}'), all optional:
 *   agentPath    explicit path to quickwork-agent(.exe)
 *   agentZip     explicit path to the app's resources/agent.zip (extracted on demand)
 *   userDataDir  app userData dir to probe for an already-extracted agent/
 *   cacheDir     where to extract agent.zip (default <tmp>/gateway-aws-quick)
 *   port         fixed outpost port (default: a free loopback port)
 *   buildStage   QUICKWORK_BUILD_STAGE for the spawned backend (default prod)
 *   profileStage QUICKWORK_PROFILE_STAGE (default = buildStage)
 *   surfaceType  WS surfaceType (default QUICK_WORK_DESKTOP_WINDOWS)
 *   appVersion   WS appVersion (default 1.2.4660)
 *   channel      WS channel hint (beta|prod)
 *   sessionReuse continue Quick threads across turns (default true)
 *   sessionTtlMs forget a cached thread state after this long unused (default 86400000 = 24h)
 *   sessionMax   at most this many cached thread states (default 200)
 *   sessionDir   where OAuth sessions are stored (default: platform config dir)
 *   oauthPort    fixed localhost callback port for OAuth login (default: free port)
 *   oauthTimeoutMs how long to wait for the browser login (default 600000)
 *   log          function(line) for plugin diagnostics
 *
 * Models are Quick's agent modes, not raw model ids:
 *   fast / balanced / smart / advanced  (X-gateway-Effort maps to thinking_effort)
 */
import path from 'node:path'
import { createOutpostManager } from './lib/outpost.mjs'
import { handleChatCompletions, handleModels } from './lib/handler.mjs'
import { createSessionStore, SESSION_DEFAULTS } from './lib/sessions.mjs'
import { MODELS } from './lib/translate.mjs'
import {
  createSessionStore as createOAuthStore, createDirectBackend,
  startOAuthLogin, takeDiscovery, keyForSession, OAuthError, defaultStoreDir,
} from './lib/oauth.mjs'
import { createCredManBackend, loadCredManSession } from './lib/credman.mjs'
import { quickUsage } from './lib/usage.mjs'

const PROVIDER = 'aws-quick'
// Requests never reach this address: the loader's fetch intercepts them.
const BASE = 'https://quick.aws.com/gateway-aws-quick/v1'

const BASE_MODEL = {
  limit: { context: 1000000, output: 64000 },
  // Quick's own agent tools run server-side; OpenAI-style function calling is
  // layered on top as a <tool_call> text convention (lib/tools.mjs).
  tool_call: true,
  api: { url: BASE, npm: '@ai-sdk/openai-compatible' },
  capabilities: {
    temperature: false,
    reasoning: true,
    attachment: true,
    toolcall: true,
    input: { text: true, image: true, audio: false, video: false, pdf: false },
    output: { text: true },
    interleaved: false,
  },
}

function requestUrl(input) {
  try {
    return input instanceof Request ? new URL(input.url) : new URL(String(input))
  } catch {
    return null
  }
}

async function readBody(input, init) {
  let raw = init?.body
  if (raw == null && input instanceof Request) raw = await input.text()
  if (raw instanceof Uint8Array || raw instanceof ArrayBuffer) raw = new TextDecoder().decode(raw)
  if (typeof raw !== 'string') raw = raw == null ? '' : String(raw)
  return JSON.parse(raw || '{}')
}

function headerOf(input, init, name) {
  try {
    const own = init?.headers ? new Headers(init.headers).get(name) : null
    return own ?? (input instanceof Request ? input.headers.get(name) : null) ?? undefined
  } catch {
    return undefined
  }
}

/** Stable per-account identity for thread-reuse scoping (hashed, never stored raw). */
async function identityFor(outpost, fallback) {
  try {
    const s = await outpost.session()
    const who = [s?.provider, s?.aws_account_id, s?.actor_arn, s?.tenant_url].filter(Boolean).join(':')
    if (who) return `${fallback}|${who}`
  } catch {}
  return fallback
}

function makeFetch(outpostMgr, opts, sessions) {
  return async function quickFetch(input, init) {
    const url = requestUrl(input)
    const path = url?.pathname ?? ''
    if (/\/models\/?$/.test(path) && (init?.method ?? 'GET').toUpperCase() === 'GET') {
      return handleModels()
    }
    if (!/\/chat\/completions\/?$/.test(path)) {
      return new Response(JSON.stringify({ error: { message: `aws-quick 插件只支持 chat/completions（收到 ${path}）`, type: 'invalid_request_error' } }), {
        status: 404,
        headers: { 'content-type': 'application/json' },
      })
    }
    let body
    try {
      body = await readBody(input, init)
    } catch {
      return new Response(JSON.stringify({ error: { message: 'request body must be JSON', type: 'invalid_request_error' } }), { status: 400, headers: { 'content-type': 'application/json' } })
    }
    const outpost = await outpostMgr.ensure().catch((e) => ({ __err: e }))
    if (outpost?.__err) {
      const e = outpost.__err
      return new Response(JSON.stringify({ error: { message: e?.message ?? String(e), type: 'api_error', code: 503 } }), { status: 503, headers: { 'content-type': 'application/json' } })
    }
    const identity = await identityFor(outpost, opts.identityKey || 'aws-quick')
    // gateway stamps every request of a chat session with x-gateway-session;
    // other agents (pi) send their own affinity headers. Captured for the
    // session-keyed thread-reuse fallback.
    const sessionKey = ['x-gateway-session', 'session_id', 'x-session-id', 'x-session-affinity', 'x-client-request-id', 'x-thread-id', 'x-conversation-id']
      .map((n) => headerOf(input, init, n))
      .find((v) => typeof v === 'string' && v.trim())
    return handleChatCompletions({
      body,
      outpostMgr,
      sessions,
      options: { ...opts, identity, sessionKey },
      effortHeader: headerOf(input, init, 'x-gateway-effort'),
      signal: init?.signal,
    })
  }
}

function makeSessions(opts) {
  if (opts.sessionReuse === false || opts.sessionReuse === 'false' || opts.sessionReuse === 0) return undefined
  const ttlMs = Number(opts.sessionTtlMs) > 0 ? Number(opts.sessionTtlMs) : SESSION_DEFAULTS.ttlMs
  const max = Number(opts.sessionMax) > 0 ? Math.floor(Number(opts.sessionMax)) : SESSION_DEFAULTS.max
  // Persist to disk: plugin hosts may re-instantiate per request/session, and
  // an in-memory-only store would drop every cached thread each time. Only
  // hashes + threadIds are written — never message content.
  const dir = opts.sessionDir || defaultStoreDir()
  return createSessionStore({ ttlMs, max, file: path.join(dir, 'thread-sessions.json') })
}

export const AwsQuickAuthPlugin = async (_input, options) => {
  const opts = options && typeof options === 'object' ? options : {}
  const outpostMgr = createOutpostManager(opts)
  const sessions = makeSessions(opts)
  // The account's backend by sign-in method: OAuth and Credential Manager chat
  // over the stored token directly (no outpost; OAuth rotates its refresh
  // token in the session file, Credential Manager writes refreshed tokens
  // back to the app's own credential); otherwise the spawned local outpost.
  function backendFor(auth) {
    const key = auth?.type === 'api' ? auth.key : undefined
    if (!key) return null
    if (auth?.metadata?.mode === 'oauth') {
      const store = createOAuthStore({ dir: opts.sessionDir })
      const backend = createDirectBackend({
        load: () => store.get(key),
        persist: (s) => store.set(key, s),
        fetchImpl: opts.fetchImpl,
        log: opts.log,
      })
      return { key, apiKey: 'aws-quick-oauth', mgr: { ensure: () => backend.ensure() } }
    }
    if (auth?.metadata?.mode === 'local-credman') {
      const backend = createCredManBackend({ fetchImpl: opts.fetchImpl, log: opts.log })
      return { key, apiKey: 'aws-quick-local', mgr: { ensure: () => backend.ensure() } }
    }
    return { key, apiKey: 'aws-quick-local', mgr: outpostMgr }
  }
  // Persist a finished OAuth login as an api-type account and return gateway's
  // success result.
  function saveOAuthSession(sess) {
    const store = createOAuthStore({ dir: opts.sessionDir })
    const key = keyForSession(sess)
    store.set(key, sess)
    const who = sess.email || sess.display_name || sess.aws_account_id || ''
    return { type: 'success', key, metadata: { mode: 'oauth', account: who } }
  }
  const startOAuthOpts = (signal, extra = {}) => ({
    stage: opts.buildStage || 'prod',
    port: Number(opts.oauthPort) || undefined,
    timeoutMs: Number(opts.oauthTimeoutMs) || undefined,
    signal,
    fetchImpl: opts.fetchImpl,
    log: opts.log,
    ...extra,
  })
  const wireAbort = (s, cancel) => {
    const onAbort = () => cancel()
    if (s) {
      if (s.aborted) onAbort()
      else s.addEventListener('abort', onAbort, { once: true })
    }
    return () => s?.removeEventListener('abort', onAbort)
  }
  // Browser OAuth sign-in as a gateway "oauth" method: authorize starts the
  // localhost callback server and hands gateway the sign-in URL — gateway opens
  // the browser itself unless the method sets `opened` — and callback waits
  // on the server for the browser flow, then stores the session + refresh
  // token in a private config file. No desktop app, no outpost at request
  // time.
  function oauthSignIn({ label, labels, instructions, opened }) {
    return {
      type: 'oauth',
      label,
      labels,
      authorize: async (_inputs, { signal } = {}) => {
        let started
        try {
          started = await startOAuthLogin(startOAuthOpts(signal))
        } catch (e) {
          return { type: 'failed', error: `Amazon Quick 账号登录失败：${String(e?.message ?? e).slice(0, 300)}` }
        }
        const { url, done, cancel } = started
        return {
          url,
          method: 'auto',
          instructions,
          ...(opened ? { opened: true } : {}),
          callback: async (_code, { signal: s } = {}) => {
            const unwire = wireAbort(s, cancel)
            try {
              const sess = await done
              return saveOAuthSession(sess)
            } catch (e) {
              const msg = String(e?.message ?? e)
              if (e instanceof OAuthError && e.kind === 'abort') return { type: 'failed', error: '登录已取消' }
              return { type: 'failed', error: `Amazon Quick 账号登录失败：${msg.slice(0, 300)}` }
            } finally {
              unwire()
            }
          },
        }
      },
    }
  }
  // The "copy sign-in link" variant: gateway shows a paste field (method
  // 'code') instead of opening anything — the user pastes the browser's
  // address-bar URL after login. A browser on another computer can't reach
  // the 127.0.0.1 callback: the first paste saves the hop-1 discovery fields
  // and asks for a second attempt, whose paste finishes the login.
  function oauthPasteSignIn() {
    return {
      type: 'oauth',
      label: '复制登录地址（不自动打开浏览器）',
      labels: { zh: '复制登录地址（不自动打开浏览器）', 'zh-TW': '複製登錄地址（不自動打開瀏覽器）', en: "Copy sign-in link (don't open a browser)" },
      authorize: async (_inputs, { signal } = {}) => {
        const dir = opts.sessionDir ?? defaultStoreDir()
        const discovery = takeDiscovery(dir)
        let started
        try {
          started = await startOAuthLogin(startOAuthOpts(signal, { discovery, dir }))
        } catch (e) {
          return { type: 'failed', error: `Amazon Quick 账号登录失败：${String(e?.message ?? e).slice(0, 300)}` }
        }
        const { url, submit, cancel } = started
        return {
          url,
          method: 'code',
          opened: true,
          instructions: discovery
            ? '已记住上次的账号信息。复制下方链接到浏览器里登录，完成后把地址栏里的完整地址粘贴到下面。'
            : '复制下方链接，到你要用的浏览器里打开并登录。登录完把浏览器地址栏里的完整地址粘贴到下面（页面打不开也没关系，复制地址就行）。',
          callback: async (code, { signal: s } = {}) => {
            const unwire = wireAbort(s, cancel)
            try {
              const sess = await submit(code)
              return saveOAuthSession(sess)
            } catch (e) {
              const msg = String(e?.message ?? e)
              if (e instanceof OAuthError && e.kind === 'discovery-saved') {
                return { type: 'failed', error: '账号信息已记下。请再点一次「复制登录地址」，登录后把地址栏的地址粘贴回来（15 分钟内有效）。' }
              }
              if (e instanceof OAuthError && e.kind === 'paste') return { type: 'failed', error: msg }
              if (e instanceof OAuthError && e.kind === 'abort') return { type: 'failed', error: '登录已取消' }
              return { type: 'failed', error: `Amazon Quick 账号登录失败：${msg.slice(0, 300)}` }
            } finally {
              unwire()
              // the paste flow is done one way or another: close the listener
              cancel()
            }
          },
        }
      },
    }
  }
  return {
    config: async (cfg) => {
      cfg.provider ??= {}
      cfg.provider[PROVIDER] ??= {
        name: 'AWS Quick',
        npm: '@ai-sdk/openai-compatible',
        api: BASE,
        models: {},
      }
    },

    auth: {
      provider: PROVIDER,
      maxConcurrency: 1,
      methods: [
        oauthSignIn({
          label: '账号登录（浏览器 OAuth，无需桌面端）',
          labels: { zh: '账号登录（浏览器 OAuth，无需桌面端）', 'zh-TW': '帳號登錄（瀏覽器 OAuth，無需桌面端）', en: 'Browser sign-in (OAuth, no desktop app)' },
          instructions: '在打开的页面完成 Amazon 登录；如果打开的不是你要用的浏览器，复制下面的链接到那个浏览器里打开。登录需在本机完成。',
        }),
        oauthPasteSignIn(),
        {
          // No prompts and no key field: gateway calls authorize directly.
          // On Windows we first try reading the app's session straight from
          // Credential Manager — that path needs no agent.exe at all while
          // the stored access token is fresh (OIDC refresh covers expiry).
          // Otherwise fall back to spawning the bundled outpost.
          type: 'api',
          keyless: true,
          label: '本地凭据（Amazon Quick 桌面端登录态）',
          labels: { zh: '本地凭据（Amazon Quick 桌面端登录态）', 'zh-TW': '本機憑據（Amazon Quick 桌面端登錄態）' },
          authorize: async (_inputs, { signal } = {}) => {
            if (process.platform === 'win32') {
              try {
                const sess = await loadCredManSession()
                if (sess?.jwt) {
                  // Exercise the real path once: fresh token returns as-is,
                  // expired token triggers OIDC refresh + credman write-back.
                  const backend = createCredManBackend({ fetchImpl: opts.fetchImpl, log: opts.log })
                  await backend.session()
                  const who = sess.email || sess.display_name || sess.aws_account_id || ''
                  return { type: 'success', key: 'aws-quick-local', metadata: { mode: 'local-credman', account: who } }
                }
              } catch {}
            }
            try {
              const outpost = await outpostMgr.ensure()
              const st = await outpost.status()
              if (!st?.logged_in && !st?.user) {
                return { type: 'failed', error: 'Amazon Quick 桌面端未登录：请先打开 Amazon Quick 登录，再重试' }
              }
              // Force a session mint now so sign-in problems surface here, not
              // on the first chat request.
              await outpost.session({ fresh: true })
              return { type: 'success', key: 'aws-quick-local', metadata: { mode: 'local-outpost' } }
            } catch (e) {
              return {
                type: 'failed',
                error: `无法连接 Amazon Quick 后端：${String(e?.message ?? e).slice(0, 300)}。请确认已安装并登录 Amazon Quick 桌面版。`,
              }
            }
          },
        },
      ],
      async loader(getAuth) {
        const auth = await getAuth()
        const b = backendFor(auth)
        if (!b) return {}
        return { baseURL: BASE, apiKey: b.apiKey, fetch: makeFetch(b.mgr, { ...opts, identityKey: b.key }, sessions) }
      },
      // gateway's own hook (OpenCode ignores it): agent-hour quota for the
      // account card and Usage page.
      usage: async (getAuth, _provider, ctx) => {
        const b = backendFor(await getAuth())
        if (!b) return { error: '未登录 Amazon Quick 账号' }
        return quickUsage(async () => (await b.mgr.ensure()).session(), { fetchImpl: opts.fetchImpl, signal: ctx?.signal })
      },
    },

    provider: {
      id: PROVIDER,
      async models(provider, { auth } = {}) {
        if (auth?.type !== 'api' || !auth.key) return provider.models
        const out = {}
        for (const m of MODELS) out[m.id] = structuredClone(BASE_MODEL)
        return out
      },
    },
  }
}

// Test surface (not a plugin function; the host calls every exported function).
export const _internal = { makeFetch, makeSessions, PROVIDER, BASE, MODELS }
