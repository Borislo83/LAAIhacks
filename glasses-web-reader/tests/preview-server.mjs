// Controlled local browser fixture: no model, database, credential or external agent calls.
import { createServer } from 'vite'
import { fileURLToPath } from 'node:url'

let revision = 1, currentPage = null, actions = []
let preferences = { display_density: 'comfortable', shopping_priority: 'balanced', proactive_enabled: true, notes: 'Private fixture note' }
const tasks = []
let contexts = 0, dispatches = 0
const page = { url: 'https://example.com/headphones', title: 'Model A headphones', site: 'Example', wordCount: 120,
  sections: [{ heading: 'Overview', blocks: [{ type: 'p', text: 'Wireless headphones with clear sound and a comfortable fit.' }] }],
  links: [{ text: 'More details', url: 'https://example.com/details' }] }
const screens = [{ layout: 'product', title: 'Model A headphones', subtitle: 'Wireless audio', price: '$100', rating: '4/5', stock: 'In stock', description: 'Clear sound and a comfortable fit.' },
  { layout: 'points', heading: 'Key details', points: ['Comfortable fit', 'Wireless connection'] }]
const server = await createServer({ configFile: false, root: fileURLToPath(new URL('../', import.meta.url)),
  resolve: { alias: { '@evenrealities/even_hub_sdk': fileURLToPath(new URL('./bridge-fixture.ts', import.meta.url)) } },
  server: { host: '127.0.0.1', port: 5187, strictPort: true }, plugins: [{ name: 'merge-fixture', configureServer(server) {
    server.middlewares.use(async (req, res, next) => {
      const url = new URL(req.url, 'http://localhost')
      if (!url.pathname.startsWith('/api/') && url.pathname !== '/fixture-status') return next()
      res.setHeader('Content-Type', 'application/json')
      const send = (value, status = 200) => { res.statusCode = status; res.end(JSON.stringify(value)) }
      if (url.pathname === '/fixture-status') return send({ contexts, dispatches, revision })
      if (url.pathname === '/api/condense') {
        res.setHeader('Content-Type', 'application/x-ndjson')
        return res.end([{ page: { ...page, url: url.searchParams.get('url') || page.url } }, { kind: 'shop' }, { screens }, { done: true }].map(e => JSON.stringify(e)).join('\n') + '\n')
      }
      if (url.pathname === '/api/telemetry') { res.statusCode = 204; return res.end() }
      if (url.pathname === '/api/recommendations') {
        let raw = ''; for await (const chunk of req) raw += chunk
        const context = JSON.parse(raw)
        return send({ source: 'claude', page: { url: context.url, title: context.title }, actions: [
          { id: 'preview-prices', title: 'Compare prices', description: 'Find the best offer across trusted stores.', instruction: 'Compare current offers for Model A headphones.' },
          { id: 'preview-alternatives', title: 'Explore similar headphones', description: 'See how other wireless options compare.', instruction: 'Find similar wireless headphones around $100.' },
          { id: 'preview-reviews', title: 'Check real-world comfort', description: 'Get the highlights from owner reviews.', instruction: 'Summarize owner reviews of Model A headphones, focusing on comfort.' },
        ] })
      }
      if (req.headers.authorization !== 'Bearer browser-fixture') return send({ error: 'Fixture token must be browser-fixture' }, 401)
      let raw = ''; for await (const chunk of req) raw += chunk
      const body = raw ? JSON.parse(raw) : null
      const path = url.pathname.replace('/api/backend', '')
      if (path === '/v1/preferences') { if (req.method === 'PUT') preferences = body; return send(preferences) }
      if (path === '/v1/sessions') return send({ session_id: 'fixture-session', revision })
      if (path.endsWith('/page')) {
        currentPage = body; revision++
        actions = [{ action_id: `external-${revision}`, label: 'Compare offers', description: 'External agent proposal for this page.', status: 'proposed', requires_approval: true }]
        return send({ session_id: 'fixture-session', revision })
      }
      if (path.endsWith('/display')) return send({ schema_version: '1.0', session_id: 'fixture-session', revision, page: currentPage, actions, mode: 'summary', notice: null })
      if (path === '/v1/contexts') {
        contexts++
        const task = { id: `task-${contexts}`, context_id: `context-${contexts}`, kind: 'reviews', status: 'predicted', payload: { product: body.product || body.title },
          result: null, error: null, created_at: new Date().toISOString() }
        tasks.unshift(task); return send({ context_id: task.context_id, prediction_source: 'rules', tasks: [task] })
      }
      if (path === '/v1/tasks') return send(tasks)
      const task = tasks.find(t => path.includes(`/tasks/${t.id}`))
      if (task && path.endsWith('/dispatch')) { dispatches++; task.status = 'succeeded'; task.result = { summary: 'Fixture reviews: comfortable fit; clear audio.' }; return send(task) }
      if (task) return send(task)
      if (/\/(approve|reject)$/.test(path)) {
        if (body.expected_revision !== revision) return send({ error: { message: 'Display changed' } }, 409)
        const action = actions.find(a => path.includes(a.action_id))
        if (!action) return send({ error: 'No action' }, 404)
        action.status = path.endsWith('/approve') ? 'approved' : 'rejected'; revision++
        // An approved action finishes a moment later, like a real agent run.
        if (action.status === 'approved') setTimeout(() => {
          action.status = 'completed'; action.result = { message: 'Fixture: best offer $89 at Example Store.' }; revision++
        }, 3000)
        return send({ action_id: action.action_id, status: action.status, event_id: action.status === 'approved' ? 'event-fixture' : null })
      }
      send({ error: 'Unknown fixture route' }, 404)
    })
  } }] })
await server.listen()
console.log('Isolated merge fixture: http://127.0.0.1:5187 — token browser-fixture')
