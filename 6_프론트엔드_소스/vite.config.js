import { defineConfig } from 'vite'
import tailwindcss from '@tailwindcss/vite'
// 백엔드 CSP(img-src 'self')가 data: URL을 막으므로 자산을 인라인하지 않는다.
export default defineConfig({
  plugins: [tailwindcss()],
  build: { assetsInlineLimit: 0 },
  server: { proxy: { '/api': 'http://127.0.0.1:8020' } },
})
