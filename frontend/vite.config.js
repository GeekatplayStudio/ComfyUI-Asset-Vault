import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev server proxies the v1 API to the local backend on 8127.
// Production builds are served by the backend itself from frontend/dist.
export default defineConfig({
  plugins: [react()],
  build: {
    target: 'es2020',
    assetsInlineLimit: 8192,
    chunkSizeWarningLimit: 400
  },
  server: {
    port: 3000,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8127',
        changeOrigin: true
      }
    }
  }
})
