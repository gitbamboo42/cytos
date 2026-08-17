import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

export default defineConfig({
  plugins: [react()],
  // The desktop shell loads `dist/index.html` off the disk, where vite's
  // default absolute "/assets/…" would resolve against the filesystem root
  // and find nothing. Relative paths work under `file://` and `http://`
  // both, so this costs the browser build nothing.
  base: './',
});
