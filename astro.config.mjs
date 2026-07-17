import { defineConfig } from 'astro/config';
import mdx from '@astrojs/mdx';
import react from '@astrojs/react';

// WowRepo builds a fully static site. No runtime AI, no server, no database.
// See docs/architecture.md for the content -> plan -> composition -> render pipeline.
export default defineConfig({
  site: 'https://wowrepo.pages.dev',
  // MDX is available for rich long-form authoring; React powers the few
  // genuinely interactive islands. Neither is required for a page to render.
  integrations: [mdx(), react()],
  build: {
    format: 'directory',
  },
  vite: {
    build: {
      // Keep the client bundle small — this is an editorial site, not an app.
      assetsInlineLimit: 4096,
    },
  },
});
