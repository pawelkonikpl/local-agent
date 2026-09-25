import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

const apiProxyTarget = process.env.VITE_API_PROXY_TARGET ?? 'http://localhost:8000'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/auth': apiProxyTarget,
      '/admin': apiProxyTarget,
      // Exact match only: "/sessions/{id}" itself is a GUI page route (no such bare API
      // endpoint exists), so a browser refresh there must fall through to the SPA, not the API.
      '^/sessions$': apiProxyTarget,
      '^/sessions/[^/]+/messages$': apiProxyTarget,
    },
  },
})
