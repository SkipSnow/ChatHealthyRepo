import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'node:path'

// CANONICAL Vite config for the FindCare React iframe.
//
// Per Skip's directive, this file lives in the DevOps deploy directory.
// At deploy time (local_deploy.py / remote_deploy.py) it is copied to
// Code/ConversationalUX/FindCareChat/frontend/vite.config.ts where Vite
// expects it adjacent to the React project's node_modules. The copy is a
// derived artifact (gitignored); change the source here and the next
// deploy picks it up.
//
// Paths below are relative to the COPY's location (frontend/), so they
// remain unchanged from the original config that lived there. `__dirname`
// at runtime resolves to the frontend directory.

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      // @providers maps to FindCare/ProviderManagement at the repo root.
      '@providers': path.resolve(__dirname, '../../../../FindCare/ProviderManagement'),
    },
  },
  build: {
    outDir: 'dist',
  },
})
