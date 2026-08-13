import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    // Served by FastAPI from web/dist in production.
    outDir: 'dist',
    sourcemap: false,
  },
  server: {
    port: 5173,
  },
});
