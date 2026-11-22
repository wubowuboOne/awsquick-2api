// Amazon Quick cloud outpost lifecycle.
//
// The desktop app ships a frozen Python backend (`quickwork-agent`, extracted
// from resources/agent.zip into the app's userData dir on Windows). In cloud
// mode it runs as `--outpost-only`, serves /auth/* and /local/* on a loopback
// port, and holds the user's sign-in state (OS credential store + profile).
//
// This module finds that binary — preferring the copy the app already
// extracted, falling back to extracting agent.zip ourselves — spawns it on a
// free port with OUR OWN instance token piped to stdin (the same contract
// Electron's AgentBridge uses), and exposes the small HTTP surface the plugin
// needs: /health, /auth/status, /auth/sncs-session.
import { spawn, spawnSync } from 'node:child_process'
import { existsSync, mkdirSync, writeFileSync, readFileSync, statSync } from 'node:fs'
import { createServer } from 'node:net'
import os from 'node:os'
import path from 'node:path'

export class OutpostError extends Error {
  constructor(message, { status, kind } = {}) {
    super(message)
    this.name = 'OutpostError'
    this.status = status
    this.kind = kind // 'auth' | 'spawn' | 'http' | 'timeout'
  }
}

const EXE_NAME = process.platform === 'win32' ? 'quickwork-agent.exe' : 'quickwork-agent'

function envPaths(...parts) {
  return parts.filter(Boolean).map((p) => path.join(...p))
}

/** Candidate directories that may already contain an extracted agent/. */
function extractedAgentDirs(options = {}) {
  const dirs = []
  if (options.userDataDir) dirs.push(path.join(options.userDataDir, 'agent'))
  const home = os.homedir()
  if (process.platform === 'win32') {
    const lad = process.env.LOCALAPPDATA
    const ad = process.env.APPDATA
    dirs.push(
      ...envPaths(lad, 'quickwork', 'agent'),
      ...envPaths(ad, 'quickwork', 'agent'),
      ...envPaths(ad, 'Amazon Quick', 'agent'),
    )
  } else if (process.platform === 'darwin') {
    dirs.push(
      path.join(home, 'Library', 'Application Support', 'quickwork', 'agent'),
      path.join(home, 'Library', 'Application Support', 'Amazon Quick', 'agent'),
    )
  } else {
    dirs.push(
      path.join(home, '.config', 'quickwork', 'agent'),
      path.join(home, '.local', 'share', 'quickwork', 'agent'),
    )
  }
  return [...new Set(dirs)]
}

/** Candidate agent.zip files shipped inside the installed app. */
function agentZipCandidates(options = {}) {
  const zips = []
  if (options.agentZip) zips.push(options.agentZip)
  if (process.platform === 'win32') {
    const lad = process.env.LOCALAPPDATA
    const pf = process.env['ProgramFiles'] || 'C:\\Program Files'
    const pfx = process.env['ProgramFiles(x86)'] || ''
    for (const root of [lad && path.join(lad, 'Programs', 'quickwork'), path.join(pf, 'quickwork'), pfx && path.join(pfx, 'quickwork')]) {
      if (root) zips.push(path.join(root, 'resources', 'agent.zip'))
    }
  } else if (process.platform === 'darwin') {
    zips.push('/Applications/Amazon Quick.app/Contents/Resources/agent.zip')
  } else {
    for (const root of ['/opt/quickwork', '/usr/lib/quickwork', '/usr/local/lib/quickwork']) {
      zips.push(path.join(root, 'resources', 'agent.zip'))
    }
  }
  return [...new Set(zips)]
}

/** In-bundle (non-Windows) layouts where the binary sits unpacked in Resources. */
function bundledBinaryCandidates() {
  if (process.platform === 'darwin') {
    return [
      '/Applications/Amazon Quick.app/Contents/Resources/agent/' + EXE_NAME,
      '/Applications/Amazon Quick.app/Contents/Resources/' + EXE_NAME,
    ]
  }
  return []
}

/** Extract a .zip using whatever the platform ships (bsdtar / Expand-Archive / unzip). */
export function extractZip(zipPath, destDir) {
  mkdirSync(destDir, { recursive: true })
  const attempts = []
  if (process.platform === 'win32') {
    attempts.push(['tar', ['-xf', zipPath, '-C', destDir]])
    attempts.push(['powershell', ['-NoProfile', '-Command', `Expand-Archive -LiteralPath '${zipPath.replace(/'/g, "''")}' -DestinationPath '${destDir.replace(/'/g, "''")}' -Force`]])
  } else {
    attempts.push(['tar', ['-xf', zipPath, '-C', destDir]])
    attempts.push(['unzip', ['-o', zipPath, '-d', destDir]])
  }
  let lastErr = null
  for (const [cmd, args] of attempts) {
    try {
      const r = spawnSync(cmd, args, { stdio: 'ignore', timeout: 10 * 60 * 1000 })
      if (r.status === 0 && existsSync(path.join(destDir, EXE_NAME))) return destDir
      lastErr = new Error(`${cmd} exited ${r.status ?? r.error}`)
    } catch (e) {
      lastErr = e
    }
  }
  // zip may nest under _internal only on some layouts; check one level down too
  if (existsSync(path.join(destDir, '_internal', EXE_NAME))) return destDir
  throw new OutpostError(`无法解压 agent.zip 到 ${destDir}: ${lastErr?.message ?? 'unknown'}`, { kind: 'spawn' })
}

/**
 * Locate quickwork-agent. Returns { path, source } or throws OutpostError.
 * Order: explicit option → app's extracted copy → in-bundle binary → extract agent.zip.
 */
export function findAgent(options = {}) {
  if (options.agentPath && existsSync(options.agentPath)) {
    return { path: options.agentPath, source: 'option' }
  }
  for (const dir of extractedAgentDirs(options)) {
    const p = path.join(dir, EXE_NAME)
    if (existsSync(p)) return { path: p, source: `extracted:${dir}` }
  }
  for (const p of bundledBinaryCandidates()) {
    if (existsSync(p)) return { path: p, source: 'bundle' }
  }
  // Explicit zip first, then install-location candidates; extract into a stable cache dir.
  const cacheRoot = options.cacheDir || path.join(os.tmpdir(), 'gateway-aws-quick')
  for (const zip of agentZipCandidates(options)) {
    if (!existsSync(zip)) continue
    const stamp = `${statSync(zip).size}-${Math.floor(statSync(zip).mtimeMs)}`
    const dest = path.join(cacheRoot, 'agent', stamp.replace(/[^\w.-]/g, '_'))
    const exe = path.join(dest, EXE_NAME)
    if (!existsSync(exe)) {
      extractZip(zip, dest)
    }
    if (existsSync(exe)) return { path: exe, source: `zip:${zip}` }
    // some zips nest the exe one level down
    const nested = path.join(dest, '_internal', EXE_NAME)
    if (existsSync(nested)) return { path: nested, source: `zip:${zip}` }
  }
  throw new OutpostError(
    '找不到 Amazon Quick 后端（quickwork-agent）。请先安装并登录 Amazon Quick 桌面端，' +
    '或用插件选项 agentPath / agentZip 指定。',
    { kind: 'spawn', status: 503 },
  )
}

function freePort() {
  return new Promise((resolve, reject) => {
    const s = createServer()
    s.once('error', reject)
    s.listen(0, '127.0.0.1', () => {
      const { port } = s.address()
      s.close(() => resolve(port))
    })
  })
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

export class Outpost {
  /**
   * @param {object} options plugin options
   * @param {string} [options.agentPath] explicit quickwork-agent path
   * @param {string} [options.agentZip] explicit agent.zip to extract
   * @param {string} [options.userDataDir] app userData dir to probe for agent/
   * @param {string} [options.cacheDir] extraction cache dir
   * @param {number} [options.port] fixed outpost port (default: free port)
   * @param {string} [options.buildStage] QUICKWORK_BUILD_STAGE (default prod)
   * @param {string} [options.profileStage] QUICKWORK_PROFILE_STAGE
   * @param {object} [options.env] extra env for the child
   * @param {Function} [options.log]
   */
  constructor(options = {}) {
    this.options = options
    this.log = options.log ?? (() => {})
    this.token = options.instanceToken || `gateway-${process.pid}-${Math.random().toString(36).slice(2)}`
    this.child = null
    this.port = null
    this.base = null
    this.sessionCache = null // {data, until}
    this.starting = null
    this.dead = false
  }

  get running() {
    return !!this.child && this.child.exitCode === null && !this.child.killed
  }

  async ensure() {
    if (this.running && this.base) return this
    if (this.starting) return this.starting
    this.starting = this._start().finally(() => { this.starting = null })
    return this.starting
  }

  async _start() {
    if (this.running) return this
    const agent = findAgent(this.options)
    this.port = this.options.port || (await freePort())
    const env = {
      ...process.env,
      ...(this.options.env || {}),
      QUICKWORK_BUILD_STAGE: this.options.buildStage || 'prod',
      QUICKWORK_CLOUD_MODE: '1',
      QUICKWORK_OUTPOST_PORT: String(this.port),
      QUICKWORK_PROFILE_STAGE: this.options.profileStage || this.options.buildStage || 'prod',
      PYTHONUNBUFFERED: '1',
      PYTHONUTF8: '1',
    }
    this.log(`[outpost] spawning ${agent.path} on 127.0.0.1:${this.port} (${agent.source})`)
    const child = spawn(agent.path, ['--outpost-only', '--manifest', 'desktop', '--port', String(this.port)], {
      env,
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
    })
    this.child = child
    child.stdout?.on('data', (d) => this._onStdout(String(d)))
    child.stderr?.on('data', (d) => this._onStderr(String(d)))
    child.on('exit', (code, sig) => {
      this.log(`[outpost] exited code=${code} sig=${sig}`)
      if (this.child === child) { this.child = null; this.base = null; this.sessionCache = null }
    })
    child.on('error', (e) => this.log(`[outpost] spawn error: ${e.message}`))
    // instance token contract: first line on stdin (AgentBridge.pipeToken)
    try { child.stdin?.write(`${this.token}\n`); child.stdin?.end() } catch {}
    this.base = `http://127.0.0.1:${this.port}`
    await this._waitHealthy()
    return this
  }

  _onStdout(chunk) {
    for (const line of chunk.split(/\r?\n/)) {
      if (!line) continue
      const m = line.match(/OUTPOST_PORT:\s*(\d+)/)
      if (m) this.port = parseInt(m[1], 10)
      if (/LEP_PORT:/.test(line)) this.log(`[outpost] ${line.trim()}`)
    }
  }

  _onStderr(chunk) {
    // PyInstaller boot logs are noisy; keep only warnings/errors, redacted length
    for (const line of chunk.split(/\r?\n/)) {
      if (/error|Error|ERROR|Traceback|failed|Failed/.test(line)) this.log(`[outpost:err] ${line.slice(0, 300)}`)
    }
  }

  async _waitHealthy(timeoutMs = 90_000) {
    const deadline = Date.now() + timeoutMs
    let lastErr = null
    while (Date.now() < deadline) {
      if (!this.running) throw new OutpostError('outpost 进程提前退出', { kind: 'spawn' })
      try {
        const r = await this._fetch('/health', { timeoutMs: 3000 })
        if (r.ok) return
      } catch (e) { lastErr = e }
      await sleep(400)
    }
    throw new OutpostError(`outpost 启动超时: ${lastErr?.message ?? 'no response'}`, { kind: 'timeout' })
  }

  async _fetch(pathname, { method = 'GET', body, timeoutMs = 20_000 } = {}) {
    const ctrl = new AbortController()
    const t = setTimeout(() => ctrl.abort(), timeoutMs)
    try {
      const res = await fetch(`${this.base}${pathname}`, {
        method,
        headers: {
          Authorization: `Bearer ${this.token}`,
          ...(body != null ? { 'Content-Type': 'application/json' } : {}),
        },
        body: body != null ? JSON.stringify(body) : undefined,
        signal: ctrl.signal,
      })
      const text = await res.text()
      let data = null
      try { data = text ? JSON.parse(text) : null } catch { data = text }
      return { ok: res.ok, status: res.status, data, headers: res.headers }
    } finally {
      clearTimeout(t)
    }
  }

  /** GET /auth/status → { logged_in, user, provider, … } */
  async status() {
    await this.ensure()
    const r = await this._fetch('/auth/status')
    if (!r.ok) throw new OutpostError(`auth/status ${r.status}`, { status: r.status, kind: 'http' })
    return r.data
  }

  /** GET /local/outpost-id → {outpostId} (device id the chat WS expects). */
  async outpostId() {
    await this.ensure()
    const r = await this._fetch('/local/outpost-id')
    if (!r.ok) throw new OutpostError(`outpost-id ${r.status}`, { status: r.status, kind: 'http' })
    return r.data?.outpostId || ''
  }

  /**
   * GET /auth/sncs-session → { id_token, ws_host, tenant_url, actor_arn, provider,
   * aws_account_id, embed_host, usable_until, … }. Cached until shortly before
   * usable_until; pass {fresh:true} to force a re-mint.
   */
  async session({ fresh = false } = {}) {
    await this.ensure()
    const now = Date.now() / 1000
    if (!fresh && this.sessionCache && this.sessionCache.until > now + 30) return this.sessionCache.data
    const r = await this._fetch('/auth/sncs-session')
    if (r.status === 401) throw new OutpostError('会话已失效，请在 Amazon Quick 桌面端重新登录', { status: 401, kind: 'auth' })
    if (!r.ok) throw new OutpostError(`sncs-session ${r.status}: ${JSON.stringify(r.data)?.slice(0, 200)}`, { status: r.status, kind: 'http' })
    const until = Number(r.data?.usable_until) || now + 300
    this.sessionCache = { data: r.data, until }
    return r.data
  }

  async stop() {
    const c = this.child
    this.child = null
    this.base = null
    this.sessionCache = null
    if (c) {
      try { c.stdin?.end() } catch {}
      try { c.kill() } catch {}
      // give it a moment then force
      setTimeout(() => { try { c.kill('SIGKILL') } catch {} }, 3000).unref?.()
    }
  }
}

/** Shared per-plugin-instance outpost (created lazily by the loader). */
export function createOutpostManager(options = {}) {
  let current = null
  let starting = null
  return {
    options,
    get() {
      if (!current) current = new Outpost(options)
      return current
    },
    async ensure() {
      if (!current) current = new Outpost(options)
      if (!current.running) {
        if (!starting) starting = current.ensure().finally(() => { starting = null })
        await starting
      }
      return current
    },
    async stop() { if (current) await current.stop() },
  }
}
