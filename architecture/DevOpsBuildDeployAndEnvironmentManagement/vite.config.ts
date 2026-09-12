import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import fs from 'node:fs'
import path from 'node:path'

// Where the application's source is comes from the record, not from this
// file. It used to be spelled here, in _build_chain.py and in hf_helpers.py,
// so moving the application broke the build in three places and the repair
// was an edit to build source. The build must not depend on the business
// layout.
// Where the repository root is, handed in rather than inferred. This
// file is copied into build/ and run from there, so __dirname is not
// the root and counting parents would be a claim about its depth.
const ROOT = process.env.CH_REPO_ROOT ?? __dirname

const buildArchitecture = JSON.parse(
  fs.readFileSync(
    path.resolve(ROOT, 'brain/machine_artifacts/content/build_architecture.json'),
    'utf-8',
  ),
)
const findCareChat = buildArchitecture.react_applications.find(
  (a: { name: string }) => a.name === 'FindCareChat',
)
if (!findCareChat) {
  throw new Error(
    'build_architecture.json declares no react application named FindCareChat',
  )
}

// CANONICAL Vite config for the FindCare React iframe.
//
// Per Skip's directive, this file lives in the DevOps deploy directory.
// At deploy time (local_deploy.py / remote_deploy.py) it is copied to
// the repository root as vite.config.ts, where Vite
// expects it adjacent to the React project's node_modules. The copy is a
// derived artifact (gitignored); change the source here and the next
// deploy picks it up.
//
// The copy sits beside package.json, which is the repository root, so
// `__dirname` resolves there and every path below is stated from the
// root. The application's index.html is not at the root, so `root`
// names where it is.

export default defineConfig({
  // The bundle is served from the website, not from the FindCare Space.
  // The Space then serves no browser-addressable surface at all, which is
  // what lets every route on it require a SharedServices signature -- a
  // bundle route that required one could not be loaded by the iframe that
  // needs it.
  base: `/${findCareChat.serves_at}/`,
  root: path.resolve(ROOT, findCareChat.source_root),
  plugins: [react()],
  resolve: {
    alias: {
      // @providers maps to FindCare/ProviderManagement at the repo root.
      '@providers': path.resolve(ROOT, 'FindCare/ProviderManagement'),
      // @findcare maps to FindCare/ at the repo root.
      '@findcare': path.resolve(ROOT, 'FindCare'),
      // @shared maps to sharedServices/Code/. Twenty-one imports used to
      // climb five or six levels to reach it, so each was a claim about
      // where the importer lived and moving the application broke them all.
      '@shared': path.resolve(ROOT, 'sharedServices/Code'),
    },
  },
  build: {
    outDir: path.resolve(ROOT, findCareChat.dist),
    emptyOutDir: true,
  },
})
