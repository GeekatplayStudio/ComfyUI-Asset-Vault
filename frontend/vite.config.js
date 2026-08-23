import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev server proxies the v1 API to the local backend on 8127.
// Production builds are served by the backend itself from frontend/dist.
export default defineConfig({
  plugins: [react()],
  build: {
    target: 'es2020',
    assetsInlineLimit: 8192,
    // Three.js is isolated behind React.lazy() and only downloads when a user
    // opens a 3D output. Its 734 kB vendor chunk is intentional, not first paint.
    chunkSizeWarningLimit: 800
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
