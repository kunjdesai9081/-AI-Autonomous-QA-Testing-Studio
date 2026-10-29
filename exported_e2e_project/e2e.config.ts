import { defineConfig } from '@tester-army/e2e';

export default defineConfig({
  tests: 'tests/**/*.e2e.ts',
  baseUrl: "https://example.com",
  timeout: 30000,
});
