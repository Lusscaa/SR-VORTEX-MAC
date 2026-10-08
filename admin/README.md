# Coletor Admin

Serviço **independente** do app principal. Recebe telemetria empurrada pelas
instâncias dos operadores e expõe uma API protegida onde o dono vê todas as
instâncias, runs, logs e pools (com credenciais). SQLite próprio em
`admin/admin.db`. Não depende de AdsPower/Meta.

## Subir

```bash
cd admin
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export ADMIN_USERNAME=dono
export ADMIN_PASSWORD=senha-forte
export ADMIN_AUTH_SECRET=$(openssl rand -hex 32)
export INSTANCE_TOKENS="tok_socio_a:Sócio A,tok_socio_b:Sócio B"
export ADMIN_CORS_ORIGIN=http://localhost:5173   # origem do painel admin

# rode a partir da raiz do repo (o app usa imports relativos do pacote admin/)
cd ..
uvicorn admin.server:app --host 0.0.0.0 --port 8900
```

## Variáveis de ambiente

| Var | Default (dev) | Descrição |
|-----|---------------|-----------|
| `ADMIN_USERNAME` | `admin` | usuário do login admin |
| `ADMIN_PASSWORD` | `admin` | senha do login admin |
| `ADMIN_AUTH_SECRET` | `dev-secret-troque-me` | segredo HS256 do JWT |
| `ADMIN_CORS_ORIGIN` | `*` | origem liberada no CORS p/ o painel |
| `INSTANCE_TOKENS` | _vazio_ | `"tokenA:NomeA,tokenB:NomeB"` — tokens aceitos na ingestão |

## Contrato de ingestão (instâncias → coletor)

`POST /api/ingest` com header `X-Instance-Token: <token>`.

```json
{ "instance_id": "...", "instance_name": "...", "event_type": "...", "ts": 1718000000, "payload": {} }
```

`event_type ∈ {heartbeat, run_upsert, run_log, pool_snapshot}`:

- **heartbeat** — só marca a instância como viva (`last_seen`).
- **run_upsert** — UPSERT por `(instance_id, run_id)`. `payload` é a linha do run (precisa de `run_id` e `status`).
- **run_log** — acumula log. `payload`: `{run_id, ts, level, msg}` (linha única) ou `{run_id, lines:[{ts,level,msg}]}` (lote).
- **pool_snapshot** — substitui o snapshot inteiro da instância. `payload`: `{cnpjs:[], fb_accounts:[], proxies:[]}`.

## API de admin (consumida pelo painel)

- `POST /admin/login {username,password}` → `{token}` (JWT HS256, exp 12h).
- Demais `/admin/*` exigem `Authorization: Bearer <token>` (401 sem).
- `GET /admin/instances`
- `GET /admin/instances/{id}/runs?status=`
- `GET /admin/instances/{id}/runs/{run_id}/log`
- `GET /admin/instances/{id}/pools`
- `GET /api/health` — público.

## Teste rápido com curl

```bash
TOK=tok_socio_a
BASE=http://localhost:8900

curl -s $BASE/api/health

# ingestão dos 4 eventos
curl -s -XPOST $BASE/api/ingest -H "X-Instance-Token: $TOK" -H 'Content-Type: application/json' \
  -d '{"instance_id":"i1","instance_name":"Sócio A","event_type":"heartbeat","ts":1718000000,"payload":{}}'
curl -s -XPOST $BASE/api/ingest -H "X-Instance-Token: $TOK" -H 'Content-Type: application/json' \
  -d '{"instance_id":"i1","event_type":"run_upsert","payload":{"run_id":"r1","status":"running","cnpj":"123"}}'
curl -s -XPOST $BASE/api/ingest -H "X-Instance-Token: $TOK" -H 'Content-Type: application/json' \
  -d '{"instance_id":"i1","event_type":"run_log","payload":{"run_id":"r1","level":"info","msg":"oi"}}'
curl -s -XPOST $BASE/api/ingest -H "X-Instance-Token: $TOK" -H 'Content-Type: application/json' \
  -d '{"instance_id":"i1","event_type":"pool_snapshot","payload":{"cnpjs":["1"],"fb_accounts":[{"email":"a@b.c","password":"x"}],"proxies":["p1"]}}'

# login + leitura protegida
TOKEN=$(curl -s -XPOST $BASE/admin/login -H 'Content-Type: application/json' \
  -d '{"username":"dono","password":"senha-forte"}' | python -c 'import sys,json;print(json.load(sys.stdin)["token"])')

curl -s $BASE/admin/instances -H "Authorization: Bearer $TOKEN"
curl -s $BASE/admin/instances/i1/runs -H "Authorization: Bearer $TOKEN"
curl -s $BASE/admin/instances/i1/pools -H "Authorization: Bearer $TOKEN"

# sem token → 401
curl -s -o /dev/null -w '%{http_code}\n' $BASE/admin/instances
```
