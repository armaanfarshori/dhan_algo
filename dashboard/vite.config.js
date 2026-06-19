/* global process, __dirname */
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'node:path'

// API target for the dev proxy — override with DHAN_API_TARGET to point a local
// dev server at a remote dhan-api (e.g. for design QA against live data).
const API_TARGET = process.env.DHAN_API_TARGET || 'http://localhost:8765'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { '@': path.resolve(__dirname, './src') },
  },
  server: {
    proxy: {
      '/api':      API_TARGET,
      '/postback': API_TARGET,
      '/health':   API_TARGET,
    },
  },
  build: {
    outDir: 'dist',
    rollupOptions: {
      output: {
        // Split the heavy viz libs out of the main chunk for faster first load.
        manualChunks(id) {
          if (id.includes('node_modules/recharts') || id.includes('node_modules/d3')) return 'recharts'
          if (id.includes('react-day-picker') || id.includes('node_modules/date-fns')) return 'calendar'
        },
      },
    },
  },
})
