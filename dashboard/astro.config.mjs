import { defineConfig } from 'astro/config';

// Static build served by FastAPI. Nothing is inlined so the CSP can stay
// `script-src 'self'` (no 'unsafe-inline', no hashes to maintain).
export default defineConfig({
  output: 'static',
  build: { format: 'directory', inlineStylesheets: 'never', assets: 'assets' },
  vite: { build: { assetsInlineLimit: 0 } },
  devToolbar: { enabled: false },
});
