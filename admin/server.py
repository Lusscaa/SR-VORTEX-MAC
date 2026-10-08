"""Coletor central + backend de admin.

Serviço FastAPI INDEPENDENTE do app principal. Recebe telemetria empurrada
pelas instâncias dos operadores (sócios) e expõe uma API protegida onde o
dono enxerga TODAS as instâncias, runs, logs e pools (com credenciais).

Subir:  uvicorn admin.server:app --port 8900   (ver admin/README.md)
"""

import os
import time
from pathlib import Path

import jwt
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import store

# Carrega admin/.env se existir (silencioso se python-dotenv não estiver instalado).
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent / ".env", override=False)
except ImportError:
    pass

# --------------------------------------------------------------------------- #
# Config (tudo por env; defaults só p/ dev local)                              #
# --------------------------------------------------------------------------- #
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")
ADMIN_AUTH_SECRET = os.environ.get("ADMIN_AUTH_SECRET", "dev-secret-troque-me")
ADMIN_CORS_ORIGIN = os.environ.get("ADMIN_CORS_ORIGIN", "*")
JWT_ALG = "HS256"
TOKEN_TTL_SECONDS = 12 * 60 * 60  # 12h


def _load_instance_tokens():
    """INSTANCE_TOKENS = "tokenA:NomeA,tokenB:NomeB" → {token: nome}."""
    raw = os.environ.get("INSTANCE_TOKENS", "")
    tokens = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        token, _, name = part.partition(":")
        token = token.strip()
        if token:
            tokens[token] = name.strip() or token
    return tokens


INSTANCE_TOKENS = _load_instance_tokens()

app = FastAPI(title="Meta Automation — Coletor Admin")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[ADMIN_CORS_ORIGIN] if ADMIN_CORS_ORIGIN != "*" else ["*"],
    allow_credentials=ADMIN_CORS_ORIGIN != "*",
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup():
    store.init_db()


# --------------------------------------------------------------------------- #
# Modelos                                                                      #
# --------------------------------------------------------------------------- #
class IngestEvent(BaseModel):
    instance_id: str
    instance_name: str | None = None
    event_type: str
    ts: float | None = None
    payload: dict | None = None


class LoginBody(BaseModel):
    username: str
    password: str


# --- MASTER (estoque central + funcionários) ------------------------------- #
class WorkerBody(BaseModel):
    name: str
    token: str | None = None          # se ausente, o servidor gera um
    config: dict | None = None        # overrides de .env desta pessoa


class ConfigBody(BaseModel):
    config: dict


class CnpjsBody(BaseModel):
    cnpjs: list[str]


class FbAccount(BaseModel):
    email: str
    password: str | None = ""
    twofa: str | None = ""
    proxy: str | None = ""


class FbAccountsBody(BaseModel):
    accounts: list[FbAccount]


# --------------------------------------------------------------------------- #
# Saúde (público)                                                              #
# --------------------------------------------------------------------------- #
@app.get("/api/health")
def health():
    return {"ok": True, "service": "admin-collector", "ts": time.time()}


# --------------------------------------------------------------------------- #
# Ingestão (instâncias → coletor)                                              #
# --------------------------------------------------------------------------- #
@app.post("/api/ingest")
def ingest(event: IngestEvent, x_instance_token: str | None = Header(default=None)):
    if not x_instance_token or x_instance_token not in INSTANCE_TOKENS:
        raise HTTPException(status_code=401, detail="token de instância inválido")

    # Nome confiável vem do mapeamento do token; cai pro body se ausente.
    instance_name = INSTANCE_TOKENS[x_instance_token] or event.instance_name

    valid = {"heartbeat", "run_upsert", "run_log", "pool_snapshot"}
    if event.event_type not in valid:
        raise HTTPException(status_code=422, detail=f"event_type inválido: {event.event_type}")

    result = store.ingest(
        instance_id=event.instance_id,
        instance_name=instance_name,
        event_type=event.event_type,
        ts=event.ts,
        payload=event.payload or {},
    )
    if not result.get("ok"):
        raise HTTPException(status_code=422, detail=result.get("error", "falha na ingestão"))
    return result


# --------------------------------------------------------------------------- #
# Auth de admin (JWT HS256, 12h)                                               #
# --------------------------------------------------------------------------- #
@app.post("/admin/login")
def admin_login(body: LoginBody):
    if body.username != ADMIN_USERNAME or body.password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="credenciais inválidas")
    now = int(time.time())
    token = jwt.encode(
        {"sub": body.username, "iat": now, "exp": now + TOKEN_TTL_SECONDS},
        ADMIN_AUTH_SECRET,
        algorithm=JWT_ALG,
    )
    return {"token": token}


def require_admin(authorization: str | None = Header(default=None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="token ausente")
    token = authorization.split(" ", 1)[1].strip()
    try:
        jwt.decode(token, ADMIN_AUTH_SECRET, algorithms=[JWT_ALG])
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="token inválido ou expirado")
    return True


# --------------------------------------------------------------------------- #
# API de admin (protegida)                                                     #
# --------------------------------------------------------------------------- #
@app.get("/admin/instances")
def admin_instances(_=Depends(require_admin)):
    return store.list_instances()


@app.get("/admin/instances/{instance_id}/runs")
def admin_instance_runs(instance_id: str, status: str | None = None, _=Depends(require_admin)):
    return store.list_runs(instance_id, status=status)


@app.get("/admin/instances/{instance_id}/runs/{run_id}/log")
def admin_run_log(instance_id: str, run_id: str, _=Depends(require_admin)):
    return store.get_run_log(instance_id, run_id)


@app.get("/admin/instances/{instance_id}/pools")
def admin_instance_pools(instance_id: str, _=Depends(require_admin)):
    return store.get_pools(instance_id)


# --------------------------------------------------------------------------- #
# MASTER — ADMIN gerencia funcionários + estoque (contas/CNPJs)               #
# (o dono é o único que cadastra; os clientes só PUXAM — ver /worker/* abaixo)#
# --------------------------------------------------------------------------- #
import secrets  # noqa: E402


@app.post("/admin/workers")
def admin_create_worker(body: WorkerBody, _=Depends(require_admin)):
    """Cria/atualiza um funcionário. Se não mandar token, gera um seguro."""
    token = (body.token or "").strip() or ("tok_" + secrets.token_urlsafe(16))
    store.create_worker(token, body.name, body.config or {})
    return {"ok": True, "token": token, "name": body.name}


@app.get("/admin/workers")
def admin_list_workers(_=Depends(require_admin)):
    return store.list_workers()


@app.post("/admin/workers/{token}/config")
def admin_set_worker_config(token: str, body: ConfigBody, _=Depends(require_admin)):
    res = store.set_worker_config(token, body.config)
    if not res.get("ok"):
        raise HTTPException(status_code=404, detail=res.get("error", "worker não encontrado"))
    return res


@app.post("/admin/workers/{token}/active/{state}")
def admin_set_worker_active(token: str, state: int, _=Depends(require_admin)):
    return store.set_worker_active(token, bool(state))


@app.post("/admin/cnpjs")
def admin_add_cnpjs(body: CnpjsBody, _=Depends(require_admin)):
    return store.add_cnpjs(body.cnpjs)


@app.get("/admin/cnpjs")
def admin_list_cnpjs(status: str | None = None, _=Depends(require_admin)):
    return store.list_master_cnpjs(status=status)


@app.post("/admin/fb-accounts")
def admin_add_fb_accounts(body: FbAccountsBody, _=Depends(require_admin)):
    return store.add_fb_accounts([a.model_dump() for a in body.accounts])


@app.get("/admin/fb-accounts")
def admin_list_fb_accounts(status: str | None = None, _=Depends(require_admin)):
    return store.list_master_fb_accounts(status=status)


# --------------------------------------------------------------------------- #
# WORKER — o cliente (app do funcionário) autentica com o TOKEN e PUXA        #
# --------------------------------------------------------------------------- #
def require_worker(x_worker_token: str | None = Header(default=None)):
    """Valida o token do funcionário contra a tabela ``workers``.

    Aceita também o header antigo ``X-Instance-Token`` para compatibilidade.
    Retorna o dict do worker (com config).
    """
    token = (x_worker_token or "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="token do funcionário ausente")
    worker = store.get_worker(token)
    if not worker or not worker.get("active"):
        raise HTTPException(status_code=401, detail="token inválido ou desativado")
    return worker


@app.get("/worker/bootstrap")
def worker_bootstrap(worker=Depends(require_worker)):
    """O app chama ISTO logo após o funcionário digitar o token: devolve o
    nome, a config (.env) individual e quanto tem disponível no estoque."""
    cnpjs = store.list_master_cnpjs(status="available", limit=100000)
    accts = store.list_master_fb_accounts(status="available", limit=100000)
    return {
        "ok": True,
        "name": worker["name"],
        "config": worker["config"],
        "available": {"cnpjs": len(cnpjs), "fb_accounts": len(accts)},
    }


@app.get("/worker/config")
def worker_config(worker=Depends(require_worker)):
    """Só a config (.env) do funcionário — usado para (re)gravar o .env local."""
    return {"ok": True, "name": worker["name"], "config": worker["config"]}


@app.post("/worker/claim/cnpj")
def worker_claim_cnpj(worker=Depends(require_worker)):
    got = store.claim_cnpj(worker["token"])
    if not got:
        raise HTTPException(status_code=409, detail="sem CNPJ disponível no estoque")
    return {"ok": True, **got}


@app.post("/worker/claim/account")
def worker_claim_account(worker=Depends(require_worker)):
    got = store.claim_fb_account(worker["token"])
    if not got:
        raise HTTPException(status_code=409, detail="sem conta disponível no estoque")
    return {"ok": True, **got}


# --------------------------------------------------------------------------- #
# Painel admin (SPA) servido pelo próprio coletor — uma URL só p/ API + UI.    #
# Buildar antes:  (cd admin-ui && VITE_ADMIN_API= npm run build)               #
# Se admin-ui/dist não existir, o coletor segue como API-only.                 #
# --------------------------------------------------------------------------- #
_UI_DIST = Path(__file__).resolve().parent.parent / "admin-ui" / "dist"
if _UI_DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=_UI_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}")
    def _spa(full_path: str):
        # /api e /admin já foram resolvidos pelas rotas acima; aqui é só a UI.
        if full_path.startswith(("api/", "admin/", "worker/")):
            raise HTTPException(status_code=404)
        candidate = _UI_DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(_UI_DIST / "index.html")
