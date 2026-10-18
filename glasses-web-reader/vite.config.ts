import { defineConfig } from 'vite'

export default defineConfig({
  server: {
    host: true,
    port: Number(process.env.VITE_PORT ?? 5173),
    // School Wi-Fi isolates clients, so the phone reaches us through a
    // Cloudflare quick tunnel (`cloudflared tunnel --url localhost:5173`).
    allowedHosts: ['.trycloudflare.com'],
    // In dev the glasses app calls /api on its own origin; Vite forwards to the
    // condense server so phones scanning the QR code don't hit CORS.
    proxy: {
      '/api': `http://localhost:${process.env.CONDENSE_PORT ?? 8787}`,
      // Dev only: sim.html reads the Even Hub simulator's automation API
      // (`evenhub-simulator --automation-port 9898`).
      '/sim-api': { target: `http://127.0.0.1:${process.env.SIM_PORT ?? 9898}`, rewrite: p => p.replace(/^\/sim-api/, '/api') },
    },
  },
  build: { target: 'esnext' },
})
