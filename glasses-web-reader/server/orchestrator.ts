import { timingSafeEqual } from 'node:crypto'
import { Hono } from 'hono'
import { bodyLimit } from 'hono/body-limit'
import type { Context } from 'hono'
import { getConnInfo } from '@hono/node-server/conninfo'
import { execFile } from 'node:child_process'
import { LAPTOP_URLS } from '../shared/routes'

interface Options {
  base: string; transport?: typeof fetch; origins?: string[]
  /** Local development only: an orchestrator DEMO_AUTH_MODE token (demo-*) the gateway attaches
   *  for requests made on this machine. Tunnel and LAN requests never get it. */
  devToken?: string
  /** Optional per-run pairing secret (at least 24 characters) from the QR code the phone
   *  scans. A request carrying it gets the dev token from anywhere, so it never lives
   *  longer than one demo run and is only shared through that QR. */
  pairSecret?: string
  /** The TCP peer address; defaults to the Node socket. Injectable for tests. */
  peerAddress?: (c: Context) => string | undefined
  /** Opens a URL on this laptop (macOS `open`). Injectable for tests. */
  openOnLaptop?: (url: string) => Promise<void>
}

const macOpen = (url: string) => new Promise<void>((resolve, reject) =>
  execFile('open', [url], { timeout: 10_000 }, err => err ? reject(err) : resolve()))

const LOOPBACK = /^(127\.\d+\.\d+\.\d+|::1|::ffff:127\.\d+\.\d+\.\d+)$/
// A tunnel or other proxy in front of us: never local.
const TUNNEL = ['forwarded', 'x-real-ip', 'cf-connecting-ip', 'cf-ray']
const nodePeer = (c: Context) => { try { return getConnInfo(c).remote.address } catch { return undefined } }
/** The client's stand-in for "use the gateway's local dev token". */
export const LOCAL_DEV_BEARER = 'Bearer local-dev'
/** The client's stand-in for "I scanned the pairing QR": `Bearer pair-<secret>`. */
export const PAIR_PREFIX = 'pair-'

/** Narrow gateway to the existing Supabase orchestrator. No service credentials. */
export function orchestratorGateway(options: Options) {
  const base = new URL(options.base)
  if (base.username || base.password || base.pathname !== '/' || base.search || base.hash ||
      (base.protocol !== 'https:' && !(base.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(base.hostname)))) {
    throw new Error('ORCHESTRATOR_URL must be an HTTPS origin (HTTP is allowed on loopback).')
  }
  if (options.pairSecret && (!options.devToken || !/^[A-Za-z0-9_-]{24,128}$/.test(options.pairSecret))) {
    throw new Error('GATEWAY_PAIR_SECRET needs GATEWAY_DEV_TOKEN and 24-128 URL-safe characters.')
  }
  const pairBearer = options.pairSecret ? Buffer.from(`Bearer ${PAIR_PREFIX}${options.pairSecret}`) : null
  const paired = (auth: string) => {
    if (!pairBearer) return false
    const given = Buffer.from(auth)
    return given.length === pairBearer.length && timingSafeEqual(given, pairBearer)
  }
  if (options.devToken && !/^demo-[a-z]+$/.test(options.devToken)) {
    throw new Error('GATEWAY_DEV_TOKEN must be an orchestrator demo token such as demo-all.')
  }
  const transport = options.transport ?? fetch
  // A request made on this machine: the socket peer is loopback, and every hop the dev proxy
  // reports (Vite appends the real client to X-Forwarded-For) is loopback too. The Host header
  // proves nothing: it is client-controlled and Vite rewrites it.
  const peer = options.peerAddress ?? nodePeer
  const isLocal = (c: Context) => {
    if (!LOOPBACK.test(peer(c) ?? '') || TUNNEL.some(h => c.req.header(h))) return false
    const hops = (c.req.header('x-forwarded-for') ?? '').split(',').map(h => h.trim()).filter(Boolean)
    return hops.every(h => LOOPBACK.test(h))
  }
  const gateway = new Hono()
  const id = '[A-Za-z0-9_.:-]{1,160}'
  const routes: [string, RegExp][] = [
    ['GET', /^\/v1\/(preferences|tasks)$/], ['PUT', /^\/v1\/preferences$/],
    ['POST', /^\/v1\/(sessions|contexts|chat|messages\/send)$/],
    ['GET', new RegExp(`^/v1/tasks/${id}$`)], ['POST', new RegExp(`^/v1/tasks/${id}/dispatch$`)],
    ['GET', new RegExp(`^/v1/sessions/${id}/display$`)],
    ['PUT', new RegExp(`^/v1/sessions/${id}/page$`)],
    ['POST', new RegExp(`^/v1/sessions/${id}/actions/${id}/(approve|reject)$`)],
  ]
  gateway.use('*', bodyLimit({ maxSize: 100_000 }))
  // Tells the app whether it can connect without a token (local dev token on this machine).
  gateway.get('/dev', c => c.json({ local: !!options.devToken && isLocal(c), pairing: !!pairBearer }, 200, { 'Cache-Control': 'no-store' }))
  // Opens one of the hardcoded routes (YouTube, Spotify, the AirPods page) on this laptop,
  // for this machine or a paired phone only. Any other URL is refused.
  gateway.post('/laptop/open', async c => {
    const origin = c.req.header('origin')
    if (origin && ![new URL(c.req.url).origin, ...(options.origins ?? [])].includes(origin)) return c.json({ error: 'Origin not allowed' }, 403)
    const auth = c.req.header('authorization') ?? ''
    if (!((auth === LOCAL_DEV_BEARER && !!options.devToken && isLocal(c)) || paired(auth))) return c.json({ error: 'Pair the glasses to open things on the laptop.' }, 401)
    const { url } = await c.req.json<{ url?: unknown }>().catch(() => ({ url: undefined }))
    if (typeof url !== 'string' || !LAPTOP_URLS.has(url)) return c.json({ error: 'Not a known page.' }, 400)
    try {
      await (options.openOnLaptop ?? macOpen)(url)
      console.log(`[laptop] opened ${url}`)
      return c.json({ opened: url }, 200, { 'Cache-Control': 'no-store' })
    } catch {
      return c.json({ error: "Couldn't open it on the laptop." }, 502)
    }
  })
  gateway.all('*', async c => {
    const path = c.req.path.replace(/^\/api\/backend/, '')
    if (!routes.some(([method, pattern]) => method === c.req.method && pattern.test(path))) return c.json({ error: 'Route unavailable' }, 404)
    const origin = c.req.header('origin')
    const allowed = [new URL(c.req.url).origin, 'http://localhost:5173', 'http://127.0.0.1:5173', ...(options.origins ?? [])]
    if (origin && !allowed.includes(origin)) return c.json({ error: 'Origin not allowed' }, 403)
    let auth = c.req.header('authorization') ?? ''
    // Local dev sign-in (this machine), or a phone that scanned this run's pairing QR.
    const devLocal = (auth === LOCAL_DEV_BEARER && !!options.devToken && isLocal(c)) || paired(auth)
    if (devLocal) auth = `Bearer ${options.devToken}`
    else if (!/^Bearer [^\s]{1,8192}$/i.test(auth) || /^Bearer (demo-|local-dev$|pair-)/i.test(auth)) return c.json({ error: 'Connect with a signed access token.' }, 401)
    const target = new URL(path, base)
    target.search = new URL(c.req.url).search
    try {
      const response = await transport(target, {
        method: c.req.method,
        // The orchestrator must never mistake a proxied call for a local fixture caller,
        // except the local dev token, which only DEMO_AUTH_MODE on loopback accepts.
        headers: { Authorization: auth, 'Content-Type': 'application/json',
          ...(devLocal ? {} : { 'X-Forwarded-For': 'web-reader-gateway' }) },
        body: ['POST', 'PUT'].includes(c.req.method) ? await c.req.text() : undefined,
        redirect: 'error', signal: AbortSignal.timeout(path === '/v1/chat' ? 90_000 : path === '/v1/messages/send' ? 65_000 : path.endsWith('/dispatch') ? 60_000 : 30_000),
      })
      return new Response(response.body, { status: response.status, headers: {
        'Content-Type': response.headers.get('content-type') ?? 'application/json', 'Cache-Control': 'no-store',
      } })
    } catch (e) {
      // A timeout means the request reached the orchestrator, which may have acted; the
      // orchestrator's own /v1/chat deadline (80 s) normally answers before this one.
      if ((e as Error)?.name === 'TimeoutError') return c.json({ error: 'No answer yet. Check the laptop.' }, 504)
      return c.json({ error: 'Orchestrator unavailable. Check its connection and configuration.' }, 502)
    }
  })
  return gateway
}
