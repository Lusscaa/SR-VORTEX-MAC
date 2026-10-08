import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// O backend FastAPI roda em :8000; o dev server do Vite proxia /api pra ele.
// IMPORTANTE: a chave do proxy é '/api/' (COM barra), não '/api'. O Vite casa
// por prefixo — '/api' pegaria também a ROTA do app '/apis' (SPA) e mandaria pro
// backend, que devolve {"detail":"Not Found"} ao dar F5/entrar direto em /apis.
// Como toda chamada real é /api/<algo> (ver api.js: fetch(`/api${path}`)), '/api/'
// cobre tudo e deixa /apis ser servido pelo index.html (fallback SPA do Vite).
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: { '/api/': 'http://localhost:8000' },
  },
})
