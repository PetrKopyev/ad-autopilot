import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: { include: ['src/**/*.test.ts', 'src/**/*.test.tsx'], setupFiles: ['src/testSetup.ts'], testTimeout: 15000 },
});
