import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

const backend = process.env.SLOTSERVE_DEV_BACKEND ?? 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': backend,
      '/v1': backend,
      '/healthz': backend,
    },
  },
  build: {
    outDir: '../backend/slotserve/static',
    emptyOutDir: true,
    chunkSizeWarningLimit: 1200,
  },
})
