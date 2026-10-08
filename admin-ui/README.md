# Meta Farm · Admin

Painel admin (React + Vite) para o dono acompanhar **várias instâncias** (sócios)
a partir de um coletor central (Frente 3). Separado do painel de operação (`../frontend`),
com o mesmo espírito visual (neutro, denso, dark mode, lime só p/ sinal).

## Rodar

```bash
npm install
npm run dev          # sobe em http://localhost:5174
```

### Modo mock (sem backend)

```bash
echo "VITE_USE_MOCK=true" > .env.local
npm run dev
```

No mock, **qualquer usuário/senha** loga e há 2 instâncias de exemplo
(`Sócio A — São Paulo`, online; `Sócio B — Curitiba`, offline) com runs, logs e pools.

### Contra o coletor real

```bash
cp .env.example .env.local
# edite VITE_ADMIN_API=https://coletor.suaempresa.com  (ou deixe vazio p/ usar o proxy do vite)
npm run dev
```

## Contrato consumido

| Método | Endpoint | Retorno |
|---|---|---|
| POST | `/admin/login` | `{token}` (guardado em `localStorage`, injetado como `Authorization: Bearer`) |
| GET | `/admin/instances` | `[{instance_id, instance_name, last_seen, counts:{cnpjs,fb_accounts,proxies,runs}, online}]` |
| GET | `/admin/instances/{id}/runs?status=` | linhas de run (`run_id, status, cnpj, account, started_at, finished_at, error`) |
| GET | `/admin/instances/{id}/runs/{run_id}/log` | `{lines:[{ts,level,msg}]}` |
| GET | `/admin/instances/{id}/pools` | `{cnpjs, fb_accounts (com credenciais), proxies}` |

`401` em qualquer chamada → logout automático e volta pro login.

## Telas

1. **Login** — usuário/senha.
2. **Visão geral** (`/instances`) — cards por instância: nome, online/offline, contagens, último contato.
3. **Instância** (`/instances/:id`) — abas:
   - **Runs**: lista filtrável por status (chips), auto-refresh por polling, clique abre detalhe + **visualizador de log com follow/auto-refresh**.
   - **Pools**: CNPJs, contas FB (senha/2FA mascarados com revelar+copiar), proxies.
4. **Seletor de instância** no cabeçalho — troca sem voltar à visão geral.

## Estrutura

```
src/
  main.jsx              entrypoint + tema + AuthProvider
  App.jsx               rotas, layout protegido, top bar
  auth.jsx              AuthContext (token, login/logout, 401)
  api.js                cliente (real + mock), Bearer, trata 401
  mock.js               dados de exemplo (2 instâncias)
  hooks.js              usePoll (polling resiliente, lastOk/error, 401)
  utils.js              timeAgo / fmtTime
  components/
    ui.jsx              Badge, Card, Stat, Table, Freshness, CopyButton, Secret…
    InstanceSelector.jsx
    RunsTab.jsx         filtro + tabela + drawer de detalhe
    LogViewer.jsx       follow + auto-refresh
    PoolsTab.jsx
  pages/
    Login.jsx
    Overview.jsx
    Instance.jsx
```
