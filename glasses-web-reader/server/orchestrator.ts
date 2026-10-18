import { Hono } from 'hono'
import { bodyLimit } from 'hono/body-limit'

interface Options { base: string; transport?: typeof fetch; origins?: string[] }

/** Narrow gateway to the existing Supabase orchestrator. No service credentials. */
export function orchestratorGateway(options: Options) {
  const base = new URL(options.base)
  if (base.username || base.password || base.pathname !== '/' || base.search || base.hash ||
      (base.protocol !== 'https:' && !(base.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(base.hostname)))) {
    throw new Error('ORCHESTRATOR_URL must be an HTTPS origin (HTTP is allowed on loopback).')
  }
  const transport = options.transport ?? fetch
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
  gateway.all('*', async c => {
    const path = c.req.path.replace(/^\/api\/backend/, '')
    if (!routes.some(([method, pattern]) => method === c.req.method && pattern.test(path))) return c.json({ error: 'Route unavailable' }, 404)
    const origin = c.req.header('origin')
    const allowed = [new URL(c.req.url).origin, 'http://localhost:5173', 'http://127.0.0.1:5173', ...(options.origins ?? [])]
    if (origin && !allowed.includes(origin)) return c.json({ error: 'Origin not allowed' }, 403)
    const auth = c.req.header('authorization') ?? ''
    if (!/^Bearer [^\s]{1,8192}$/i.test(auth) || /^Bearer demo-/i.test(auth)) return c.json({ error: 'Connect with a signed access token.' }, 401)
    const target = new URL(path, base)
    target.search = new URL(c.req.url).search
    try {
      const response = await transport(target, {
        method: c.req.method,
        headers: { Authorization: auth, 'Content-Type': 'application/json',
          // The orchestrator must never mistake a proxied call for a local fixture caller.
          'X-Forwarded-For': 'web-reader-gateway' },
        body: ['POST', 'PUT'].includes(c.req.method) ? await c.req.text() : undefined,
        redirect: 'error', signal: AbortSignal.timeout(path === '/v1/chat' ? 90_000 : path === '/v1/messages/send' ? 65_000 : path.endsWith('/dispatch') ? 60_000 : 30_000),
      })
      return new Response(response.body, { status: response.status, headers: {
        'Content-Type': response.headers.get('content-type') ?? 'application/json', 'Cache-Control': 'no-store',
      } })
    } catch {
      return c.json({ error: 'Orchestrator unavailable. Check its connection and configuration.' }, 502)
    }
  })
  return gateway
}
