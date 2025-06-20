import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  root: '.',
  plugins: [react()],
  // Expose both `VITE_` and `NEXT_PUBLIC_` environment variables to the client
  // so deployments configured for Next.js continue to work without changes.
  envPrefix: ['VITE_', 'NEXT_PUBLIC_'],
  optimizeDeps: {
    include: ['./src/main.tsx']
  },
  build: {
    outDir: 'dist'
  }
});
