// Cloud image upload for oversized inline images (verified live, no device
// needed): the raw bytes go to upload-attachment, a DLP job scans them, then
// an invokeAgent qbs-event delivers the file into the thread's cloud
// workspace and hands back a `path` the user_message attachment can point at.
//
//   1. POST {base}upload-attachment?name&contentType&contentLength → {attachmentId}
//   2. GET  {base}attachment-dlp-status?attachmentId=… until the DLP job ends
//   3. POST {base}qbs-event  invokeAgent files/upload/<threadId> → workspace path
//   4. user_message.attachments[] = {type:'image', name, size, mimeType, path,
//      attachmentId, contentSha256} — no inline `data`.
import crypto from 'node:crypto'

const DLP_DONE = new Set(['COMPLETED', 'SKIPPED', 'NOT_APPLICABLE'])
const DLP_FAIL = new Set(['FAILED', 'ERROR'])

function fail(reason, message, status) {
  const e = new Error(message)
  e.reason = reason
  if (status) e.status = status
  return e
}

const isAbort = (e, signal) => signal?.aborted || e?.name === 'AbortError'

const sleep = (ms, signal) => new Promise((resolve, reject) => {
  if (signal?.aborted) return reject(Object.assign(new Error('aborted'), { name: 'AbortError' }))
  const onAbort = () => { clearTimeout(t); reject(Object.assign(new Error('aborted'), { name: 'AbortError' })) }
  const t = setTimeout(() => { signal?.removeEventListener('abort', onAbort); resolve() }, ms)
  signal?.addEventListener('abort', onAbort, { once: true })
})

const json = async (r) => { try { return await r.json() } catch { return {} } }

/** {tenant}/integration/quick-work/, or derived from ws_host when unset. */
function attachBase(session) {
  const tenant = String(session?.tenant_url ?? '').trim().replace(/\/+$/, '')
  if (tenant) return `${tenant}/integration/quick-work/`
  const ws = String(session?.ws_host ?? '').trim().replace(/\/+$/, '')
  if (!ws) return ''
  const http = /^wss:\/\//i.test(ws) ? `https://${ws.slice(6)}`
    : /^ws:\/\//i.test(ws) ? `http://${ws.slice(5)}`
      : `https://${ws}`
  return `${http}/integration/quick-work/`
}

/**
 * Upload one inline image through Quick's cloud attachment flow.
 * @param {object} p
 * @param {object} p.session   sncs-shaped session ({id_token, tenant_url, ws_host})
 * @param {object} p.image     {mimeType, data(base64)}
 * @param {string} p.name      attachment file name (image-N.ext)
 * @param {string} p.threadId  Quick thread the file is delivered into
 * @param {Function} [p.fetchImpl]
 * @param {AbortSignal} [p.signal]
 * @param {number} [p.pollMs]  DLP poll interval (default 1500)
 * @param {number} [p.maxPolls] DLP poll budget (default 40)
 * @returns {Promise<object>} user_message attachment entry (no `data`)
 * @throws {Error} with .reason ∈ 'upload'|'dlp'|'deliver'|'timeout'|'auth'
 */
export async function uploadImage({ session, image, name, threadId, fetchImpl = globalThis.fetch, signal, pollMs = 1500, maxPolls = 40 }) {
  const token = session?.id_token
  if (!token) throw fail('auth', 'session has no id_token')
  const base = attachBase(session)
  if (!base) throw fail('auth', 'session has no tenant_url or ws_host')
  const bytes = Buffer.from(String(image?.data ?? ''), 'base64')
  const mimeType = image?.mimeType || 'application/octet-stream'
  const headers = {
    Authorization: `Bearer ${token}`,
    'x-amzn-qs-internal-trace-id': crypto.randomUUID(),
  }
  const auth = (r, what) => {
    if (r.status === 401 || r.status === 403) throw fail('auth', `${what} rejected: HTTP ${r.status}`, r.status)
  }

  // 1. raw bytes → attachmentId
  let r
  try {
    r = await fetchImpl(`${base}upload-attachment?` + new URLSearchParams({
      name: String(name || 'image'), contentType: mimeType, contentLength: String(bytes.length),
    }), {
      method: 'POST',
      headers: { ...headers, 'Content-Type': 'application/octet-stream' },
      body: bytes,
      signal,
    })
  } catch (e) {
    if (isAbort(e, signal)) throw e
    throw fail('upload', `upload request failed: ${e?.message ?? e}`)
  }
  auth(r, 'upload')
  if (!r.ok) throw fail('upload', `upload failed: HTTP ${r.status}`, r.status)
  const up = await json(r)
  const attachmentId = up.attachmentId || up.attachment_id || ''
  if (!attachmentId) throw fail('upload', 'upload returned no attachmentId')

  // 2. wait for the DLP scan; only an explicit ALLOW lets the file through
  let cleared = false
  for (let i = 0; i < maxPolls; i++) {
    try {
      r = await fetchImpl(`${base}attachment-dlp-status?attachmentId=${encodeURIComponent(attachmentId)}`, { headers, signal })
    } catch (e) {
      if (isAbort(e, signal)) throw e
      throw fail('dlp', `dlp status check failed: ${e?.message ?? e}`)
    }
    auth(r, 'dlp-status')
    if (r.ok) {
      const st = await json(r)
      const job = st?.dlpJobStatus || st?.dlp_job_status || ''
      if (DLP_DONE.has(job)) {
        if (st?.policyAction !== 'ALLOW') throw fail('dlp', `dlp policy ${st?.policyAction ?? 'unknown'} did not allow the image`)
        cleared = true
        break
      }
      if (DLP_FAIL.has(job)) throw fail('dlp', `dlp job ${job}`)
    }
    // pending or inconclusive response: keep polling within the budget
    if (i + 1 < maxPolls) await sleep(pollMs, signal)
  }
  if (!cleared) throw fail('timeout', `dlp did not finish within ${maxPolls} polls`)

  // 3. deliver into the thread's cloud workspace; outpostId must be a
  // non-empty UUID (empty → HTTP 500) but need not match the socket's
  const evt = {
    eventType: 'invokeAgent',
    eventBody: { invokeAgent: {
      path: `files/upload/${threadId}`,
      queryParams: { _method: 'POST' },
      body: { attachmentId },
      workingSessionId: crypto.randomUUID(),
      outpostId: crypto.randomUUID(),
    } },
  }
  try {
    r = await fetchImpl(`${base}qbs-event`, {
      method: 'POST',
      headers: { ...headers, 'Content-Type': 'application/json' },
      body: JSON.stringify(evt),
      signal,
    })
  } catch (e) {
    if (isAbort(e, signal)) throw e
    throw fail('deliver', `deliver request failed: ${e?.message ?? e}`)
  }
  auth(r, 'deliver')
  if (!r.ok) throw fail('deliver', `deliver failed: HTTP ${r.status}`, r.status)
  const dj = await json(r)
  let res = dj?.eventResponse?.invokeAgentResponse
  if (typeof res === 'string') { try { res = JSON.parse(res) } catch { res = null } }
  res = res?.api_response ?? res
  if (typeof res === 'string') { try { res = JSON.parse(res) } catch { res = null } }
  if (res?.status != null) {
    const st = Number(res.status)
    if (!(st >= 200 && st < 300)) throw fail('deliver', `deliver returned status ${res.status}`)
  }
  let rb = res?.body
  if (typeof rb === 'string') { try { rb = JSON.parse(rb) } catch { rb = null } }
  const path = rb?.path || rb?.files?.[0]?.path || ''
  if (!path) throw fail('deliver', 'deliver returned no file path')

  return {
    type: 'image',
    name: String(name || 'image'),
    size: bytes.length,
    mimeType,
    path,
    attachmentId,
    contentSha256: crypto.createHash('sha256').update(bytes).digest('hex'),
  }
}
