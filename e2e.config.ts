import { defineConfig } from '@tester-army/e2e';

export default defineConfig({
  tests: 'tests/**/*.e2e.ts',
  baseUrl: "https://shah-consultancy.netlify.app/claims/file3",
  timeout: 30000,
});
