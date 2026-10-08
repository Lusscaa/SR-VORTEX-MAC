import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// App ADMIN, separado do painel de operação (frontend/, porta 5173).
// Roda em :5174. Em dev, /admin é proxiado pro coletor (Frente 3) se você
// não setar VITE_ADMIN_API — ajuste o alvo abaixo conforme onde o coletor sobe.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5174,
    proxy: { '/admin': 'http://localhost:8787' },
  },
})
