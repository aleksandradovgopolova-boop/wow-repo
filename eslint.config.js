// Flat ESLint config. Intentionally light: it guards against obvious mistakes
// without fighting Astro's own conventions. Type-level checking is handled by
// `astro check`, not ESLint.
import js from '@eslint/js';
import tseslint from 'typescript-eslint';
import astro from 'eslint-plugin-astro';

export default [
  {
    ignores: [
      'dist/**',
      'node_modules/**',
      '.astro/**',
      'src/env.d.ts',
      'test-results/**',
      'playwright-report/**',
      'tests/visual/screenshots/**',
    ],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  ...astro.configs.recommended,
  {
    rules: {
      '@typescript-eslint/no-explicit-any': 'off',
      '@typescript-eslint/no-unused-vars': [
        'warn',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_' },
      ],
    },
  },
  {
    // Node/build-time engine + config files use Node globals.
    files: ['src/engine/**/*.ts', '*.config.*', 'tests/**/*.ts'],
    languageOptions: {
      globals: { process: 'readonly', console: 'readonly' },
    },
  },
];
