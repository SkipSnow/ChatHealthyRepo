import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'node:path'

// Business-model path aliases. Each alias maps a stable name to a
// FindCare-feature directory in the repo root, so imports survive
// any future directory move — change the alias here in one place,
// every `import ... from '@<alias>/...'` stays unchanged.
//
// Aliases mirror the agile_backlog Epic/Feature hierarchy.
// Offsets are project-root-relative from this config file's directory;
// path.resolve(__dirname, …) anchors them at config-load so vite's
// extension resolution (.ts/.tsx) fires on every @<alias>/X import.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@providers': path.resolve(__dirname, '../../../../FindCare/ProviderManagement'),
    },
  },
  build: {
    outDir: 'dist',
  },
})
