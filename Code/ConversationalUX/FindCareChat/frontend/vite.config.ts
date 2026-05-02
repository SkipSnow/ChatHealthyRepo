import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// frontend/src/ux/ holds the UX components (originally hosted at
// Code/Shared/ux/ under an @shared/ux path alias). Single-app reality —
// no second consumer — so the alias is gone; the files live where they
// are imported from. Re-share when an actual second consumer arrives.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: 'dist',
  },
})
