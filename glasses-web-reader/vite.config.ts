import { defineConfig } from 'vite'

export default defineConfig({
  server: {
    host: true,
    port: 5173,
    // School Wi-Fi isolates clients, so the phone reaches us through a
    // Cloudflare quick tunnel (`cloudflared tunnel --url localhost:5173`).
    allowedHosts: ['.trycloudflare.com'],
    // In dev the glasses app calls /api on its own origin; Vite forwards to the
    // condense server so phones scanning the QR code don't hit CORS.
    proxy: { '/api': 'http://localhost:8787' },
  },
  build: { target: 'esnext' },
})
