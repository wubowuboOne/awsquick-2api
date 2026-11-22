// Serve one OpenAI Chat Completions request through an Amazon Quick chat turn.
//
// Quick has no OpenAI-shaped HTTP API: the desktop client talks to a bidi
// WebSocket (`session_connect` → `user_message` → text_delta/thinking_delta/…
// → message_complete). This module renders the OpenAI message list into Quick's
// text protocol, runs the turn on a cached thread, and re-emits the event
// stream as chat.completion / chat.completion.chunk.
//
// OpenAI `tools` have no wire channel either, but the model emits a
// caller-defined <tool_call>{json}</tool_call> convention as plain text when
// the instruction is inline and strong (verified live). lib/tools.mjs owns the
// preamble + streaming parser; results return as role:'tool' messages rendered
// into the next turn's text.
import { QuickSocket, QuickWsError } from './quickws.mjs'
import { OutpostError } from './outpost.mjs'
import { uploadImage } from './attach.mjs'
import { normalizeTools, buildToolPreamble, createToolCallParser } from './tools.mjs'
import {
  AGENT_MODES, MODELS, modeFor, effortFor, renderTranscript, renderDelta, textOf,
  byteLen, capText, UPSTREAM_TEXT_LIMIT, canonRole, pickImages,
} from './translate.mjs'
import {
  chainHashes, sessionScope, findContinuation, extendHash, replyMessage,
  replyAnchor, tailAnchor,
} from './sessions.mjs'

const SURFACE = 'QUICK_WORK_DESKTOP_WINDOWS'
const APP_VERSION = '1.2.4660'
// system_prompt shares the user_message frame's ~64 KB budget with `text`;
// cap it so the transcript/tool preamble always keeps real room.
const SYSTEM_PROMPT_CAP = 32 * 1024
// Text room kept free when inline images claim the frame.
const IMAGE_TEXT_RESERVE = 8 * 1024

function errorResponse(status, message, type = 'api_error') {
  return new Response(JSON.stringify({ error: { message, type, code: status } }), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

function errorType(err) {
  if (err?.kind === 'auth') return 'authentication_error'
  if (err?.status === 429 || err?.kind === 'quota') return 'rate_limit_error'
  return 'api_error'
}

function httpStatus(err) {
  if (err instanceof OutpostError || err instanceof QuickWsError) {
    if (err.kind === 'auth') return 401
    if (err.kind === 'abort') return 499
    if (err.kind === 'spawn' || err.kind === 'timeout') return 503
  }
  return err?.status && err.status >= 400 && err.status < 600 ? err.status : 502
}

const sockets = new WeakMap() // Outpost → QuickSocket

async function socketFor(outpost, options) {
  let s = sockets.get(outpost)
  if (!s) {
    const outpostId = await outpost.outpostId()
    // OAuth-provisioned sessions (direct backend) have no local outpost; the
    // server accepts an empty outpostId. A *local* outpost returning none is a
    // real auth failure though.
    if (!outpostId && !outpost.direct) throw new OutpostError('outpost 未返回设备 id（/local/outpost-id），请确认已登录 Amazon Quick', { kind: 'auth', status: 401 })
    s = new QuickSocket({
      getSession: () => outpost.session(),
      outpostId,
      extra: {
        surfaceType: options?.surfaceType || SURFACE,
        appVersion: options?.appVersion || APP_VERSION,
        channel: options?.channel,
      },
      log: options?.log,
      agent: outpost.agent || null,
    })
    sockets.set(outpost, s)
  }
  return s
}

/** Usage object from a turn_usage event payload (snake_case wire fields). */
function usageFrom(u) {
  if (!u || typeof u !== 'object') return null
  const input = Number(u.input_tokens ?? u.inputTokens) || 0
  const cached = Number(u.cache_read_tokens ?? u.cached_input_tokens ?? u.cachedInputTokens) || 0
  const written = Number(u.cache_write_tokens ?? u.cacheWriteTokens) || 0
  const completion = Number(u.output_tokens ?? u.outputTokens) || 0
  // Quick reports non-cached input, cache reads and cache writes separately;
  // OpenAI prompt_tokens is the whole prompt side.
  const prompt = input + cached + written
  const total = Number(u.total_tokens ?? u.totalTokens) || prompt + completion
  const cost = Number(u.cost_usd ?? u.costUsd)
  const ctx = Number(u.context_window ?? u.contextWindow)
  const resolved = u.resolved_model_id ?? u.served_model_id ?? u.resolvedModelId
  return {
    prompt_tokens: prompt,
    completion_tokens: completion,
    total_tokens: total || prompt + completion,
    ...(cached ? { prompt_tokens_details: { cached_tokens: cached } } : {}),
    // Quick-reported extras; OpenAI clients ignore unknown usage fields.
    ...(Number.isFinite(cost) && cost > 0 ? { cost } : {}),
    ...(Number.isFinite(ctx) && ctx > 0 ? { context_window: ctx } : {}),
    ...(typeof resolved === 'string' && resolved ? { resolved_model_id: resolved } : {}),
  }
}

function addUsage(acc, u) {
  if (!u) return acc
  acc = acc ?? { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 }
  // Each agentic step re-reads the whole thread, so summing prompts counted
  // the same context once per step — clients size their context window from
  // prompt_tokens (Claude Code auto-compacted on it). The prompt is the
  // latest step's; only completions accumulate.
  if (u.prompt_tokens) {
    acc.prompt_tokens = u.prompt_tokens
    if (u.prompt_tokens_details?.cached_tokens) acc.prompt_tokens_details = { cached_tokens: u.prompt_tokens_details.cached_tokens }
    else delete acc.prompt_tokens_details
  }
  acc.completion_tokens += u.completion_tokens || 0
  acc.total_tokens = acc.prompt_tokens + acc.completion_tokens
  if (u.cost) acc.cost = (acc.cost || 0) + u.cost
  // Window and model are per-turn facts, not sums: keep the latest.
  if (u.context_window) acc.context_window = u.context_window
  if (u.resolved_model_id) acc.resolved_model_id = u.resolved_model_id
  return acc
}

const newCallId = () => `call_${crypto.randomUUID().replace(/-/g, '').slice(0, 24)}`

/**
 * Normalized turn output:
 *   {type:'meta', threadId, reused, mode}
 *   {type:'text', text}                 visible answer delta
 *   {type:'reasoning', text}            thinking_delta
 *   {type:'tool_call', id, name, arguments}   parsed <tool_call> block (arguments = JSON string)
 *   {type:'usage', usage}               turn_usage (per message)
 *   {type:'note', text}                 non-fatal oddities worth showing
 *   {type:'done', …}                    terminal bookkeeping
 */
async function* runTurn({ outpost, socket, body, sessions, options, signal }) {
  const messages = Array.isArray(body.messages) ? body.messages : []
  const convo = messages.filter((m) => m.role !== 'system' && m.role !== 'developer')
  const systemText = messages
    .filter((m) => m.role === 'system' || m.role === 'developer')
    .map((m) => textOf(m.content))
    .join('\n\n')
  const mode = modeFor(body.model)
  const effort = effortFor(options?.effortHeader, body)
  const tcNone = body.tool_choice === 'none' || (typeof body.tool_choice === 'object' && body.tool_choice?.type === 'none')
  const tools = tcNone ? [] : normalizeTools(body.tools)
  // The client's system prompt rides the dedicated user_message.system_prompt
  // field (verified live: the server honors it as a per-turn system override,
  // even at ~54 KB). It shares the same ~64 KB frame budget as `text`, so both
  // are capped together — the transcript budget shrinks by whatever the system
  // prompt occupies.
  const systemPrompt = systemText ? capText(systemText, SYSTEM_PROMPT_CAP) : ''
  const stats = {}
  if (systemText && byteLen(systemText) > SYSTEM_PROMPT_CAP) stats.truncated = true
  const sessionKey = typeof options?.sessionKey === 'string' ? options.sessionKey.slice(0, 128) : ''
  // custom_prompt is a real per-turn instruction channel (verified live). Use
  // it to tell the model this session is app-driven: with client tools, steer
  // local-environment work to the <tool_call> protocol; on direct backends
  // (OAuth / Credential Manager — no outpost, hence no connected device) also
  // say built-in device tools cannot run, so the model doesn't burn agentic
  // turns on guaranteed failures. The local-outpost path may genuinely have a
  // device attached, so the claim stays off there.
  const directBackend = !!outpost?.direct
  const customPrompt = tools.length
    ? directBackend
      ? 'Calling context: this chat is driven by an external application. The client tools listed in the message are executed by that application on the user\'s machine; this session has no connected device, so built-in device tools (local files, apps) cannot run here. For questions about the user\'s project, files or code, call the client tools to read current contents — earlier tool results may be partial, and a fresh read beats guessing.'
      : 'Calling context: this chat is driven by an external application. The client tools listed in the message are executed by that application on the user\'s machine — for questions about the user\'s project, files or code, call them to read current contents; earlier tool results may be partial, and a fresh read beats guessing.'
    : directBackend
      ? 'Calling context: this chat is driven by an external application and has no connected device. Built-in tools that need the user\'s device (local files, apps) cannot run in this session — say so plainly rather than attempting them.'
      : ''
  const textCap = Math.max(8 * 1024, UPSTREAM_TEXT_LIMIT - byteLen(systemPrompt) - byteLen(customPrompt) - 1024)

  // Tool specs are part of thread identity: changing them must not resume a
  // thread whose history lacks the convention.
  const identity = options?.identity || 'local'
  const scope = sessionScope(identity, `${mode}|${effort ?? ''}`, systemText, tools.map((t) => t.name))
  const hashes = chainHashes(scope, convo)
  const cont = sessions ? findContinuation(sessions, hashes, convo) : undefined
  // Fuzzy resume: context extensions (pi-magic-context, rewind hooks, …) can
  // prune or rewrite old messages, breaking the strict prefix chain. Anchor on
  // our last reply echo + its 3 predecessors — content hashes survive the
  // position shifts such edits cause. Only the LAST assistant can yield an
  // appendable delta anyway.
  let fuzzy
  if (!cont && sessions) {
    let a = -1
    for (let i = convo.length - 1; i >= 0; i--) {
      if (canonRole(convo[i]?.role) === 'assistant') { a = i; break }
    }
    if (a >= 3) {
      const rk = replyAnchor(scope, convo[a])
      const tk = tailAnchor(scope, convo.slice(a - 3, a))
      const hit = sessions.findEntry((e) => e.replyKey === rk && e.tailKey === tk)
      if (hit) {
        const delta = convo.slice(a + 1)
        if (delta.length && delta.every((m) => ['user', 'tool', 'function'].includes(canonRole(m.role)))) {
          sessions.take(hit.key)
          fuzzy = { entry: hit.entry, delta }
        }
      }
    }
  }
  // Session-keyed fallback: gateways like gateway stamp every request of a
  // chat with a stable session id (x-gateway-session). When content matching
  // fails entirely — heavy client-side compaction rewrites even our reply
  // echo — the session boundary still identifies the conversation, so trust
  // it and append whatever follows the last assistant message. Verified
  // matches (strict/fuzzy) always win; this is the last resort.
  let sessHit
  if (!cont && !fuzzy && sessions && sessionKey) {
    const sk = `sess:${scope}:${sessionKey}`
    if (sessions.has(sk)) {
      let a = -1
      for (let i = convo.length - 1; i >= 0; i--) {
        if (canonRole(convo[i]?.role) === 'assistant') { a = i; break }
      }
      if (a >= 0) {
        const delta = convo.slice(a + 1)
        if (delta.length && delta.every((m) => ['user', 'tool', 'function'].includes(canonRole(m.role)))) {
          const entry = sessions.take(sk)
          if (entry) sessHit = { entry, delta }
        }
      }
    }
  }
  const reuse = cont ?? fuzzy ?? sessHit
  // A tool preamble that would leave under ~8 KB for the actual message
  // degrades to names+descriptions first — otherwise the frame hard-cap
  // (a tail cut) would eat the user's question and the model would see
  // only the tool list. Names-only is the last resort: verbose descriptions
  // can keep even the lite preamble over budget, and a shed preamble means
  // zero tool instructions and guaranteed plain-text answers.
  const fitPreamble = () => {
    let pre = buildToolPreamble(tools, body.tool_choice)
    if (byteLen(pre) > textCap - 8192) {
      const lite = buildToolPreamble(tools, body.tool_choice, { lite: true })
      if (byteLen(lite) < byteLen(pre)) { pre = lite; stats.truncated = true }
    }
    if (byteLen(pre) > textCap - 4096) {
      const names = buildToolPreamble(tools, body.tool_choice, { namesOnly: true })
      if (byteLen(names) < byteLen(pre)) { pre = names; stats.truncated = true }
    }
    return pre
  }
  // A reused thread already holds the full spec from its first turn (tool
  // names are part of the scope, so a changed toolset never reuses), and
  // whatever rides in `text` stays in Quick's history — so later turns only
  // get a short reminder. Fresh threads, including the stale-thread retry
  // below, get the full spec.
  const preamble = !tools.length ? '' : reuse ? buildToolPreamble(tools, body.tool_choice, { reminder: true }) : fitPreamble()
  const textBudget = Math.max(4 * 1024, textCap - byteLen(preamble))

  // Images ride user_message.attachments: small ones inline as base64 inside
  // the frame; on direct (device-less) backends larger ones go through
  // Quick's cloud attachment upload instead (lib/attach.mjs) — the local
  // outpost delivery path is unverified, so it stays inline-only. Only this
  // turn's images are sent; inline bytes shrink the text budget, uploaded
  // ones don't (they carry no base64 on the wire).
  const lastAsst = convo.findLastIndex((m) => canonRole(m.role) === 'assistant')
  const imageSlice = reuse ? reuse.delta : convo.slice(lastAsst + 1)
  const images = pickImages(imageSlice, Math.max(0, textBudget - IMAGE_TEXT_RESERVE), { upload: directBackend })
  const turnBudget = textBudget - images.bytes
  const frameCap = textCap - images.bytes

  let threadId = reuse ? reuse.entry.threadId : crypto.randomUUID()
  const imageNoteFor = (uploadFailed) => {
    const n = images.skipped + uploadFailed
    return n ? `\n\n[${n} image(s) not delivered: too large, upload failed, or not inline data]` : ''
  }
  // Deliver the queued uploads into tid's cloud workspace, in parallel.
  // Failures degrade to the not-delivered note; aborts propagate.
  const uploadAll = async (tid) => {
    if (!images.toUpload.length) return { entries: [], failed: 0 }
    const sess = await outpost.session()
    const results = await Promise.allSettled(images.toUpload.map((img) =>
      uploadImage({
        session: sess, image: img, name: img.name, threadId: tid,
        fetchImpl: options?.fetchImpl ?? globalThis.fetch, signal,
      })
    ))
    const entries = []
    let failed = 0
    for (const r of results) {
      if (r.status === 'fulfilled') { entries.push(r.value); continue }
      if (signal?.aborted || r.reason?.name === 'AbortError') throw r.reason
      failed++
      options?.log?.(`[aws-quick] image upload failed: ${String(r.reason?.message ?? r.reason).slice(0, 200)}`)
    }
    return { entries, failed }
  }
  const uploaded = await uploadAll(threadId)
  const imageNote = imageNoteFor(uploaded.failed)
  const attachments = images.attachments.concat(uploaded.entries)

  let text
  let reused = false
  if (reuse) {
    text = renderDelta(reuse.delta, { maxBytes: turnBudget, stats }) + imageNote
    reused = true
  } else {
    text = renderTranscript(convo, '', { maxBytes: turnBudget, stats }) + imageNote
  }
  // The tool convention rides inside the user_message text (there is no
  // client-tool channel); on reused threads only the short reminder rides,
  // since every byte of text stays in the thread and counts toward context.
  let upstreamText = preamble ? `${preamble}\n\n${text}` : text
  if (byteLen(upstreamText) > frameCap) {
    // Last resort before the tail-cutting hard cap: shed the preamble so the
    // user's message survives intact (tool calling degrades, the answer doesn't).
    upstreamText = byteLen(text) <= frameCap ? text : capText(text, frameCap)
    stats.truncated = true
  }
  if (!upstreamText.trim()) upstreamText = '(empty)'

  const state = { text: '', reasoning: '', usage: null, calls: [] }
  const parser = tools.length ? createToolCallParser() : null
  let fedText = false
  let sawOutput = false

  const payload = {
    text: upstreamText,
    message_id: crypto.randomUUID(),
    agent_mode: mode,
    user_timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC',
    user_locale: (Intl.DateTimeFormat().resolvedOptions().locale || 'en-US').replace('_', '-'),
  }
  if (effort) payload.thinking_effort = effort
  if (systemPrompt) payload.system_prompt = systemPrompt
  if (customPrompt) payload.custom_prompt = customPrompt
  // Client-supplied tools own the tool loop; keep Quick's web search out of it.
  if (tools.length) payload.web_search_enabled = false
  if (options?.chatAgentId) payload.chat_agent_id = options.chatAgentId
  if (attachments.length) payload.attachments = attachments

  function emitParsed(r) {
    // r: {text, calls:[{name,arguments}]} — yields normalized items, mutates state.
    const out = []
    if (r.text) { state.text += r.text; out.push({ type: 'text', text: r.text }) }
    for (const c of r.calls) {
      const id = newCallId()
      state.calls.push({ id, name: c.name })
      out.push({ type: 'tool_call', id, name: c.name, arguments: JSON.stringify(c.arguments ?? {}) })
    }
    if (out.length) sawOutput = true
    return out
  }

  async function* consume(turnIt) {
    for await (const evt of turnIt) {
      const t = evt.type
      const p = evt.payload || {}
      if (t === 'text_delta' && typeof p.delta === 'string') {
        fedText = true
        const r = parser ? parser.push(p.delta) : { text: p.delta, calls: [] }
        yield* emitParsed(r)
      } else if (t === 'thinking_delta' && typeof p.delta === 'string') {
        sawOutput = true
        state.reasoning += p.delta
        yield { type: 'reasoning', text: p.delta }
      } else if (t === 'turn_usage') {
        const u = usageFrom(p)
        if (u) { state.usage = addUsage(state.usage, u); yield { type: 'usage', usage: u } }
      } else if (t === 'message_complete') {
        // Fallback: deltas can be dropped; the complete payload carries full text.
        // Only when NO delta arrived — parser buffer would double-count otherwise.
        if (!fedText && typeof p.text === 'string' && p.text) {
          const r = parser ? parser.push(p.text) : { text: p.text, calls: [] }
          yield* emitParsed(r)
        }
      } else if (t === 'message_start' || t === 'connected' || t === 'session_established' || t === 'keepalive') {
        // noise
      } else if (t === 'status') {
        const s = p.status || p.message
        if (s && !/pong|ok/i.test(String(s))) yield { type: 'note', text: `[quick] ${s}` }
      } else if (t === 'guardrail_blocked' || t === 'error') {
        // Error details may live in payload OR at the event's top level;
        // dump the whole event (truncated) when nothing readable is found.
        const detail = p.message || p.reason || p.error || evt.message || evt.reason || evt.error || evt.code
        const extra = detail ? '' : ` ${JSON.stringify(evt).slice(0, 400)}`
        throw new QuickWsError(`${detail || `quick ${t}`}${extra}`, { kind: 'stream' })
      }
      // tool_start/tool_arguments/tool_progress/tool_result, runs_updated,
      // task_list_update, title_generated, session_sync … are agent-internal;
      // Quick executes its own tools, so they don't map to OpenAI tool_calls.
    }
    if (parser) {
      const tail = parser.end()
      if (tail.text) yield* emitParsed({ text: tail.text, calls: [] })
      for (const c of tail.calls ?? []) {
        const id = newCallId()
        state.calls.push({ id, name: c.name })
        yield { type: 'tool_call', id, name: c.name, arguments: JSON.stringify(c.arguments ?? {}) }
      }
      if (tail.dropped) yield { type: 'note', text: '[quick] tool call 输出被截断，已忽略' }
    }
  }

  yield { type: 'meta', threadId, reused, mode }
  if (stats.truncated) yield { type: 'note', text: '上下文超过 Quick 64KB 上限，已截断较早内容' }

  const produced = () => state.text.length > 0 || state.calls.length > 0 || state.reasoning.length > 0
  try {
    for await (const o of consume(socket.turn(threadId, payload, { signal }))) yield o
  } catch (e) {
    if (!(reuse && sessions) || produced() || e?.kind === 'auth' || signal?.aborted) throw e
    // Cached thread may be gone server-side: retry once on a fresh thread with
    // the full transcript (same policy as gateway-comate). Uploaded files live
    // in the dead thread's workspace, so they are delivered again for `fresh`.
    const fresh = crypto.randomUUID()
    const up2 = await uploadAll(fresh)
    const retryAttachments = images.attachments.concat(up2.entries)
    const full = renderTranscript(convo, '', { maxBytes: turnBudget, stats }) + imageNoteFor(up2.failed)
    const fullPreamble = tools.length ? fitPreamble() : ''
    const fullText = fullPreamble ? `${fullPreamble}\n\n${full}` : full
    const capped = byteLen(fullText) > frameCap ? capText(fullText, frameCap) : fullText
    state.text = ''
    state.reasoning = ''
    state.usage = null
    state.calls = []
    fedText = false
    sawOutput = false
    threadId = fresh
    reused = false
    yield { type: 'meta', threadId: fresh, reused: false, mode }
    payload.text = capped.trim() || '(empty)'
    payload.message_id = crypto.randomUUID()
    if (retryAttachments.length) payload.attachments = retryAttachments
    else delete payload.attachments
    for await (const o of consume(socket.turn(fresh, payload, { signal }))) yield o
  }

  // Remember where this conversation stands for the next request.
  if (sessions && (state.text || state.calls.length)) {
    const reply = replyMessage(state.text, state.calls)
    const key = extendHash(hashes[convo.length], reply)
    sessions.set(key, {
      threadId,
      replyKey: replyAnchor(scope, reply),
      tailKey: tailAnchor(scope, convo.slice(-3)),
    })
    // Session-keyed entry self-perpetuates: rewritten every turn so the
    // fallback survives however much the client rewrites its history.
    if (sessionKey) sessions.set(`sess:${scope}:${sessionKey}`, { threadId })
  }
  yield { type: 'done', threadId, text: state.text, reasoning: state.reasoning, usage: state.usage }
}

function makeId(threadId) {
  return `chatcmpl-quick-${threadId?.slice(0, 8) ?? 'x'}-${Date.now().toString(36)}`
}

/**
 * @param {object} p
 * @param {object} p.body       parsed Chat Completions request
 * @param {object} p.outpostMgr createOutpostManager() result
 * @param {object} [p.sessions] session store (lib/sessions.mjs)
 * @param {object} [p.options]  plugin options
 * @param {string} [p.effortHeader] X-gateway-Effort style header value
 * @param {AbortSignal} [p.signal]
 */
export async function handleChatCompletions({ body, outpostMgr, sessions, options, effortHeader, signal }) {
  if (!body || typeof body !== 'object') return errorResponse(400, 'request body must be JSON', 'invalid_request_error')
  if (!Array.isArray(body.messages) || !body.messages.length) {
    return errorResponse(400, 'messages is required', 'invalid_request_error')
  }
  let outpost
  try {
    outpost = await outpostMgr.ensure()
  } catch (e) {
    return errorResponse(httpStatus(e), e?.message ?? String(e), errorType(e))
  }
  let socket
  try {
    socket = await socketFor(outpost, { ...options, effortHeader, log: options?.log })
  } catch (e) {
    return errorResponse(httpStatus(e), e?.message ?? String(e), errorType(e))
  }

  const model = typeof body.model === 'string' && body.model.trim() ? body.model.trim() : 'smart'
  const it = runTurn({ outpost, socket, body: { ...body, model }, sessions, options: { ...options, effortHeader }, signal })

  // Pull up to the first real output before answering so auth/session failures
  // surface as real HTTP statuses (gateway marks the account on 401, fails over
  // on 429/5xx) instead of a 200 stream that dies immediately.
  const pre = []
  let done = false
  let meta
  try {
    for (;;) {
      const r = await it.next()
      if (r.done) { done = true; break }
      if (r.value.type === 'meta') { meta = r.value; continue }
      if (r.value.type === 'note') continue // notes ride inside the stream
      pre.push(r.value)
      break
    }
  } catch (e) {
    return errorResponse(httpStatus(e), e?.message ?? String(e), errorType(e))
  }
  const items = (async function* () {
    yield* pre
    if (done) return
    for (;;) {
      const r = await it.next()
      if (r.done) return
      yield r.value
    }
  })()

  const created = Math.floor(Date.now() / 1000)
  const id = makeId(meta?.threadId)

  if (!body.stream) {
    let text = ''
    let reasoning = ''
    const calls = []
    let usage
    try {
      for await (const o of items) {
        if (o.type === 'text') text += o.text
        else if (o.type === 'reasoning') reasoning += o.text
        else if (o.type === 'tool_call') calls.push({ id: o.id, type: 'function', function: { name: o.name, arguments: o.arguments } })
        else if (o.type === 'usage') usage = addUsage(usage, o.usage)
      }
    } catch (e) {
      return errorResponse(httpStatus(e), e?.message ?? String(e), errorType(e))
    }
    const message = { role: 'assistant', content: text }
    if (reasoning) message.reasoning_content = reasoning
    if (calls.length) message.tool_calls = calls
    return new Response(
      JSON.stringify({
        id, object: 'chat.completion', created, model,
        choices: [{ index: 0, message, finish_reason: calls.length ? 'tool_calls' : 'stop' }],
        usage: usage ?? { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
      }),
      { status: 200, headers: { 'content-type': 'application/json' } },
    )
  }

  const enc = new TextEncoder()
  const includeUsage = !!body.stream_options?.include_usage
  const stream = new ReadableStream({
    async start(controller) {
      const send = (obj) => controller.enqueue(enc.encode(`data: ${JSON.stringify(obj)}\n\n`))
      const chunk = (delta, finish = null) => send({ id, object: 'chat.completion.chunk', created, model, choices: [{ index: 0, delta, finish_reason: finish }] })
      let sentRole = false
      const role = () => {
        if (!sentRole) {
          sentRole = true
          chunk({ role: 'assistant', content: '' })
        }
      }
      let toolIndex = 0
      let usage
      try {
        for await (const o of items) {
          if (o.type === 'text') {
            role()
            chunk({ content: o.text })
          } else if (o.type === 'reasoning') {
            role()
            chunk({ reasoning_content: o.text })
          } else if (o.type === 'tool_call') {
            role()
            chunk({ tool_calls: [{ index: toolIndex++, id: o.id, type: 'function', function: { name: o.name, arguments: o.arguments } }] })
          } else if (o.type === 'usage') usage = addUsage(usage, o.usage)
          else if (o.type === 'note') {
            role()
            chunk({ content: `\n\n> ${o.text}` })
          }
        }
        role()
        chunk({}, toolIndex > 0 ? 'tool_calls' : 'stop')
        if (includeUsage) send({ id, object: 'chat.completion.chunk', created, model, choices: [], usage: usage ?? { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 } })
        controller.enqueue(enc.encode('data: [DONE]\n\n'))
      } catch (e) {
        send({ error: { message: e?.message ?? String(e), type: errorType(e), code: httpStatus(e) } })
        controller.enqueue(enc.encode('data: [DONE]\n\n'))
      } finally {
        controller.close()
      }
    },
    cancel() {
      items.return?.()
      it.return?.()
    },
  })
  return new Response(stream, {
    status: 200,
    headers: { 'content-type': 'text/event-stream; charset=utf-8', 'cache-control': 'no-cache' },
  })
}

/** /v1/models in OpenAI shape. */
export function dropSocket(outpost) {
  const s = sockets.get(outpost)
  if (s) {
    try { s.close() } catch {}
    sockets.delete(outpost)
  }
}

export function handleModels() {
  return new Response(JSON.stringify({ object: 'list', data: MODELS }), {
    headers: { 'content-type': 'application/json' },
  })
}

export { AGENT_MODES }
