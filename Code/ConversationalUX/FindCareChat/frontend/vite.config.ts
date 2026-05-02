import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

// Path alias replaces the legacy ux symlink (BUG-003 cleanup).
// Resolves @shared/ux/* to Code/Shared/ux/* via a relative path from
// this config file. No filesystem symlink required, no Windows admin /
// Developer Mode dependency, no copytree drift risk.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@shared/ux': path.resolve(__dirname, '../../../Shared/ux'),
    },
  },
  build: {
    outDir: 'dist',
  },
})
