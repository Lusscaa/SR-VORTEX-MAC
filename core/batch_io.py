"""Parsing de planilhas (CSV/XLSX) de contas para enfileirar runs em lote.

Cada linha = uma conta a provisionar. Colunas mínimas (cabeçalho, sem
case-sensitivity, aceita aliases):

    cnpj | email | password | totp_seed

Aliases aceitos:
    email     → email, meta_email, login, email_or_phone, telefone
    password  → password, senha, pass
    totp_seed → totp_seed, totp, seed, 2fa

Retorna ``(specs, errors)``: ``specs`` são dicts prontos pro ``JobSpec``
(cnpj/meta_email_or_phone/meta_password/meta_totp_seed); ``errors`` lista as
linhas inválidas (sem derrubar o lote inteiro por uma linha ruim).

CSV usa só a stdlib; XLSX usa ``openpyxl`` (import preguiçoso — erro claro se
faltar). Sem pandas.
"""
from __future__ import annotations

import csv
import io
import re
from typing import Any, Optional

from utils.helpers import only_digits

# --- combolist (.txt) -------------------------------------------------------
# Contas vendidas vêm em linhas com campos separados por ':' (ou '|', ';',
# tab). Ordem típica: telefone:senha_fb:seed_2fa:email_recup:senha_email.
# Em vez de confiar na POSIÇÃO (que varia entre vendedores), classificamos
# cada token: seed 2FA = base32; telefone = só dígitos (10-15); email = tem
# '@'; senha do FB = token logo após o login. Robusto contra reordenação.
_COMBO_DELIMS = (":", "|", ";", "\t")
# Seed TOTP em base32: A-Z e 2-7, ≥16 chars (16/26/32 são os comuns).
_BASE32_RE = re.compile(r"^[A-Z2-7]{16,}$")
_B32_GROUP_RE = re.compile(r"^[A-Z2-7]{2,8}$", re.IGNORECASE)


def _merge_b32_groups(parts: list[str]) -> list[str]:
    """Junta grupos base32 consecutivos numa única seed.

    Seeds de apps autenticadores vêm agrupadas (ex.: 'E2RO 6NXF YB4C 43IQ').
    Quando a linha é splitada em espaços simples, viram tokens separados.
    Detecta corridas de tokens curtos [A-Z2-7] e, se juntos formam ≥16 chars,
    funde-os num token só para o detector de seed reconhecer.
    """
    merged: list[str] = []
    i = 0
    while i < len(parts):
        if _B32_GROUP_RE.match(parts[i]):
            j = i + 1
            while j < len(parts) and _B32_GROUP_RE.match(parts[j]):
                j += 1
            if j - i >= 2:
                combined = "".join(p.upper() for p in parts[i:j])
                if len(combined) >= 16 and _BASE32_RE.match(combined):
                    merged.append(" ".join(parts[i:j]))
                    i = j
                    continue
        merged.append(parts[i])
        i += 1
    return merged


def _norm_seed(tok: str) -> str:
    """Normaliza um possível seed 2FA: remove espaços/hífens e passa a maiúsculas.

    Seeds costumam vir agrupados (ex.: ``PCBC Y4BF LDA4 ZE24``); sem essa
    limpeza o base32 não casa e a conta seria rejeitada como não reconhecida.
    """
    return re.sub(r"[\s-]", "", tok).upper()


def _detect_delim(line: str) -> Optional[str]:
    return next((d for d in _COMBO_DELIMS if d in line), None)


def _parse_combo_line(line: str) -> Optional[dict[str, str]]:
    """Extrai uma conta de uma linha de combolist. None se não reconhecer.

    Identifica os campos por NATUREZA (não por ordem):
      * seed 2FA  → base32 (≥16 chars, A-Z2-7);
      * login     → telefone (10-15 dígitos) ou, na falta, o primeiro email;
      * senha FB  → primeiro token após o login que não seja seed/email.
    Precisa dos 3 (login, senha, seed) — senão devolve None.
    """
    line = line.strip()
    delim = _detect_delim(line)
    if delim and delim != "\t":
        parts = [p.strip() for p in line.split(delim) if p.strip()]
    else:
        # TAB e/ou 2+ espaços como separador (planilha, colagem mista).
        # \s{2,} preserva espaços ÚNICOS internos do seed 2FA
        # (ex.: 'R7WL JVRD P25B GJIO') e separa campos colados
        # com espaços múltiplos entre eles.
        parts = [p.strip() for p in re.split(r"\t+|\s{2,}", line) if p.strip()]
        if len(parts) <= 1:
            # Fallback: só espaços simples — splita tudo e junta os
            # grupos base32 consecutivos pra reconstruir a seed.
            raw = [p for p in line.split() if p]
            parts = _merge_b32_groups(raw)
    # Precisa de PELO MENOS login + senha. O 2FA é OPCIONAL (contas sem 2FA).
    if len(parts) < 2:
        return None

    def _is_email(tok: str) -> bool:
        return "@" in tok and "." in tok.split("@")[-1]

    # O seed 2FA é OPCIONAL: base32 ≥16 (pode vir agrupado, ex.: "PCBC Y4BF …").
    # Se não houver, seed fica vazio (conta sem 2FA — antes isso era rejeitado).
    seed_part = next((p for p in parts if _BASE32_RE.match(_norm_seed(p))), "")
    seed = _norm_seed(seed_part) if seed_part else ""

    # Login = telefone (10-15 díg.) OU, na falta, o 1º EMAIL (o login pode SER
    # um email, ex.: devonuchanan@op.pl).
    phone = next((p for p in parts if p.isdigit() and 10 <= len(p) <= 15), "")
    email_login = next((p for p in parts if _is_email(p)), "")
    login = phone or email_login
    if not login:
        return None

    login_idx = parts.index(login)
    password = ""
    for cand in parts[login_idx + 1:]:
        # Pula o seed e EMAILS REAIS (tem '@' E ponto no domínio). NÃO pula
        # qualquer '@': senhas podem conter '@' (ex.: 'Samii@1234', '0786Poiu@$',
        # que são a SENHA, não email) — antes isso as descartava como inválidas.
        if cand == seed_part or _is_email(cand):
            continue
        password = cand
        break
    if not password:
        return None

    # Campos extras (opcionais) p/ as Observações do AdsPower:
    # email da conta, senha do email e (se houver) email de recuperação.
    emails = [p for p in parts if _is_email(p)]
    # O email da conta = 1º email DIFERENTE do login; se o login JÁ é um email
    # (e é o único), ele mesmo é o email da conta.
    email_address = next((e for e in emails if e != login), "")
    if not email_address and login == email_login:
        email_address = email_login
    recovery_email = ""
    email_password = ""
    if email_address:
        others = [e for e in emails if e != email_address]
        recovery_email = others[0] if others else ""
        # senha do email = token logo após a ÚLTIMA ocorrência do email da conta
        # (não pode ser email, seed nem a própria senha do FB).
        idxs = [i for i, p in enumerate(parts) if p == email_address]
        eidx = idxs[-1] if idxs else -1
        nxt = parts[eidx + 1] if 0 <= eidx < len(parts) - 1 else ""
        if nxt and not _is_email(nxt) and nxt != seed_part and nxt != password:
            email_password = nxt

    return {
        "email": login,
        "password": password,
        "totp_seed": seed,
        "email_address": email_address,
        "email_password": email_password,
        "recovery_email": recovery_email,
    }


# --- combolist rotulada (blocos multi-linha) --------------------------------
# Alguns vendedores entregam cada conta como um BLOCO de linhas rotuladas em
# vez de uma linha só, ex.:
#
#     login: 61590975260429
#     senha: EWvwQaW3CtAZ
#     2FA: WXLIWF4IR7PITBCOUDN2GZI524A4QXDA
#     email: desertwindriver5529@mail.com
#     senha email: nnrqrtkad1nNTELO
#
# Contas vêm coladas uma após a outra. Reconhecemos o rótulo antes do ':'
# (sem case, aceitando acentos/variações) e mapeamos pro mesmo dict que o
# parser de linha única devolve. O bloco fecha quando um rótulo já preenchido
# reaparece (ex.: 2º 'login') — assim não dependemos de linha em branco entre
# contas nem da quantidade exata de campos.
_LABEL_FIELDS: dict[str, str] = {
    # login (telefone ou e-mail usado pra logar na Meta)
    "login": "email", "telefone": "email", "phone": "email", "celular": "email",
    "user": "email", "usuario": "email", "usuário": "email", "conta": "email",
    "meta_email": "email", "email_or_phone": "email", "email/telefone": "email",
    # senha do Facebook/Meta
    "senha": "password", "password": "password", "pass": "password",
    "senha fb": "password", "senha_fb": "password", "senha facebook": "password",
    "senha da conta": "password",
    # seed 2FA (TOTP)
    "2fa": "totp_seed", "2 fa": "totp_seed", "totp": "totp_seed",
    "seed": "totp_seed", "totp_seed": "totp_seed", "2fa seed": "totp_seed",
    "seed 2fa": "totp_seed", "codigo 2fa": "totp_seed", "código 2fa": "totp_seed",
    "chave 2fa": "totp_seed", "2fa key": "totp_seed",
    # e-mail da conta (recuperação/verificação)
    "email": "email_address", "e-mail": "email_address", "mail": "email_address",
    "email da conta": "email_address", "email conta": "email_address",
    # senha do e-mail
    "senha email": "email_password", "senha e-mail": "email_password",
    "senha do email": "email_password", "senha do e-mail": "email_password",
    "senha_email": "email_password", "email password": "email_password",
    "email_password": "email_password", "pass email": "email_password",
    # e-mail de recuperação (opcional)
    "email recuperacao": "recovery_email", "email recuperação": "recovery_email",
    "email de recuperacao": "recovery_email", "email de recuperação": "recovery_email",
    "email_recup": "recovery_email", "recovery email": "recovery_email",
    "recovery_email": "recovery_email",
}
# Rótulo = texto antes do 1º ':', normalizado (sem case, espaços colapsados).
_LABEL_RE = re.compile(r"^\s*([^:]{1,40}?)\s*:\s*(.*\S)\s*$")


def _norm_label(raw: str) -> str:
    return re.sub(r"\s+", " ", (raw or "").strip().lower())


def _label_field(raw_label: str) -> Optional[str]:
    return _LABEL_FIELDS.get(_norm_label(raw_label))


def _looks_labeled(lines: list[str]) -> bool:
    """True se o texto parece o formato de blocos rotulados (>=2 rótulos
    conhecidos). Uma combolist de linha única não bate: o texto antes do 1º
    ':' é o telefone/senha, não um rótulo conhecido."""
    hits = 0
    for line in lines:
        m = _LABEL_RE.match(line)
        if m and _label_field(m.group(1)) is not None:
            hits += 1
            if hits >= 2:
                return True
    return False


def _finalize_block(cur: dict[str, str]) -> Optional[dict[str, str]]:
    """Valida um bloco acumulado. Precisa de login+senha+seed 2FA; devolve o
    dict no mesmo formato do parser de linha única, ou None se incompleto."""
    login = (cur.get("email") or "").strip()
    password = (cur.get("password") or "").strip()
    seed = (cur.get("totp_seed") or "").strip()
    if not (login and password and seed):
        return None
    return {
        "email": login,
        "password": password,
        "totp_seed": seed,
        "email_address": (cur.get("email_address") or "").strip(),
        "email_password": (cur.get("email_password") or "").strip(),
        "recovery_email": (cur.get("recovery_email") or "").strip(),
    }


def _parse_labeled_blocks(
    lines: list[str],
) -> tuple[list[dict[str, str]], list[str]]:
    """Lê contas em formato de blocos rotulados. Fecha um bloco quando um
    campo já preenchido reaparece. Retorna ``(contas, erros)``."""
    accounts: list[dict[str, str]] = []
    errors: list[str] = []
    cur: dict[str, str] = {}

    def flush() -> None:
        if not cur:
            return
        acc = _finalize_block(cur)
        if acc is None:
            faltando = [
                nome for nome, key in
                (("login", "email"), ("senha", "password"), ("seed 2FA", "totp_seed"))
                if not (cur.get(key) or "").strip()
            ]
            errors.append(
                "conta " + str(len(accounts) + len(errors) + 1)
                + ": bloco incompleto (faltando: " + ", ".join(faltando) + ")"
            )
        else:
            accounts.append(acc)

    for line in lines:
        if not line.strip():
            continue
        m = _LABEL_RE.match(line)
        if not m:
            continue  # linha sem rótulo reconhecível — ignora
        field = _label_field(m.group(1))
        if field is None:
            continue
        value = m.group(2).strip()
        if not value:
            continue
        # Rótulo repetido dentro do bloco → começa uma conta nova.
        if field in cur:
            flush()
            cur = {}
        cur[field] = value

    flush()
    return accounts, errors


def _read_lines(raw: bytes) -> list[str]:
    return raw.decode("utf-8-sig", errors="replace").splitlines()


# --- proxies (combolist) ----------------------------------------------------
def _parse_proxy_line(raw: str, default_scheme: str = "http") -> Optional[dict[str, Any]]:
    """Normaliza 1 linha de proxy. Aceita os formatos de venda mais comuns:

        scheme://user:pass@host:port   |   user:pass@host:port
        host:port:user:pass            |   host:port

    ``default_scheme`` é usado quando a linha não traz "scheme://" explícito
    (ex.: tipo escolhido pelo usuário na tela de cadastro).

    Devolve ``{label, scheme, host, port, username, password}`` ou ``None``."""
    s = (raw or "").strip()
    if not s:
        return None

    scheme = (default_scheme or "http").lower()
    if scheme not in {"http", "https", "socks5"}:
        scheme = "http"

    user = pwd = None

    # Tira scheme:// se tiver
    if "://" in s:
        _, s = s.split("://", 1)

    # Tira user:pass@ se tiver
    if "@" in s:
        cred, s = s.rsplit("@", 1)
        if ":" in cred:
            user, pwd = cred.split(":", 1)
        else:
            user = cred

    # Agora é host:port ou host:port:user:pass
    parts = s.split(":")

    if len(parts) < 2:
        return None

    host = parts[0].strip()
    port_str = parts[1].strip()

    # Se tiver 4+ partes e user ainda é None, pega como host:port:user:pass
    if len(parts) >= 4 and user is None:
        user = parts[2].strip() or None
        pwd = parts[3].strip() or None

    if not host or not port_str:
        return None

    try:
        port_int = int(port_str)
    except ValueError:
        return None

    return {
        "label": f"{host}:{port_str}",
        "scheme": scheme,
        "host": host,
        "port": port_int,
        "username": user,
        "password": pwd,
    }


def parse_proxies(
    raw: bytes, filename: str, default_scheme: str = "http"
) -> tuple[list[dict[str, Any]], list[str]]:
    """Lê proxies de .txt (1 por linha) ou .csv/.xlsx (coluna única/'proxy').

    ``default_scheme`` (http/https/socks5) é o tipo escolhido pelo usuário na
    tela de cadastro; só vale para linhas sem "scheme://" explícito.

    Retorna ``(proxies_normalizados, erros)``."""
    default_scheme = (default_scheme or "http").strip().lower()
    if default_scheme not in {"http", "https", "socks5"}:
        default_scheme = "http"
    if (filename or "").lower().endswith(".txt"):
        lines = _read_lines(raw)
    else:
        lines = []
        for row in _read_rows(raw, filename):
            val = (row.get("proxy") or next(iter(row.values()), "") or "").strip()
            if val:
                lines.append(val)
    proxies: list[dict[str, Any]] = []
    errors: list[str] = []
    for i, line in enumerate(lines, start=1):
        s = line.strip()
        if not s:
            continue
        p = _parse_proxy_line(s, default_scheme=default_scheme)
        if not p:
            errors.append(f"linha {i}: proxy inválido ({s!r})")
            continue
        proxies.append(p)
    return proxies, errors

_ALIASES: dict[str, tuple[str, ...]] = {
    "cnpj": ("cnpj",),
    "email": ("email", "meta_email", "login", "email_or_phone", "telefone"),
    "password": ("password", "senha", "pass"),
    "totp_seed": ("totp_seed", "totp", "seed", "2fa"),
}


def _pick(row: dict[str, str], field: str) -> str:
    for alias in _ALIASES[field]:
        if alias in row and (row[alias] or "").strip():
            return row[alias].strip()
    return ""


def _to_spec_dict(row: dict[str, str]) -> dict[str, str]:
    cnpj = only_digits(_pick(row, "cnpj"))
    if len(cnpj) != 14:
        raise ValueError(f"CNPJ inválido ({_pick(row, 'cnpj')!r})")
    email = _pick(row, "email")
    password = _pick(row, "password")
    totp = _pick(row, "totp_seed")
    faltando = [
        nome
        for nome, val in (("email", email), ("password", password), ("totp_seed", totp))
        if not val
    ]
    if faltando:
        raise ValueError(f"campos vazios: {', '.join(faltando)}")
    return {
        "cnpj": cnpj,
        "meta_email_or_phone": email,
        "meta_password": password,
        "meta_totp_seed": totp,
    }


def _read_csv(raw: bytes) -> list[dict[str, str]]:
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    rows: list[dict[str, str]] = []
    for r in reader:
        rows.append({(k or "").strip().lower(): (v or "") for k, v in r.items()})
    return rows


def _read_xlsx(raw: bytes) -> list[dict[str, str]]:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # noqa: BLE001
        raise ValueError(
            "leitura de .xlsx exige o pacote 'openpyxl' (pip install openpyxl) "
            "— ou exporte como .csv"
        ) from exc
    wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    ws = wb.active
    rows_iter = ws.iter_rows(values_only=True)
    try:
        header = [str(c).strip().lower() if c is not None else "" for c in next(rows_iter)]
    except StopIteration:
        return []
    rows: list[dict[str, str]] = []
    for values in rows_iter:
        if values is None or all(v is None for v in values):
            continue
        row = {
            header[i]: ("" if v is None else str(v))
            for i, v in enumerate(values)
            if i < len(header)
        }
        rows.append(row)
    return rows


def parse_accounts(raw: bytes, filename: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Lê a planilha e devolve (specs válidos, erros por linha)."""
    name = (filename or "").lower()
    if name.endswith(".csv"):
        records = _read_csv(raw)
    elif name.endswith((".xlsx", ".xlsm")):
        records = _read_xlsx(raw)
    else:
        raise ValueError("formato não suportado — use .csv ou .xlsx")

    specs: list[dict[str, Any]] = []
    errors: list[str] = []
    for i, row in enumerate(records, start=2):  # linha 1 = cabeçalho
        try:
            specs.append(_to_spec_dict(row))
        except ValueError as exc:
            errors.append(f"linha {i}: {exc}")
    return specs, errors


# ---------------------------------------------------------------------------
# Parsers de POOL (contas e CNPJs como listas separadas, pareadas na fila)
# ---------------------------------------------------------------------------
def _read_rows(raw: bytes, filename: str) -> list[dict[str, str]]:
    name = (filename or "").lower()
    if name.endswith(".csv"):
        return _read_csv(raw)
    if name.endswith((".xlsx", ".xlsm")):
        return _read_xlsx(raw)
    raise ValueError("formato não suportado — use .csv ou .xlsx")


def parse_cnpjs(raw: bytes, filename: str) -> tuple[list[str], list[str]]:
    """Lê CNPJs de .csv/.xlsx (coluna 'cnpj'/única) ou .txt (1 por linha).

    Retorna ``(cnpjs_normalizados, erros)``."""
    if (filename or "").lower().endswith(".txt"):
        cnpjs: list[str] = []
        errors: list[str] = []
        for i, line in enumerate(_read_lines(raw), start=1):
            val = line.strip()
            if not val:
                continue
            digits = only_digits(val)
            if len(digits) != 14:
                errors.append(f"linha {i}: CNPJ inválido ({val!r})")
                continue
            cnpjs.append(digits)
        return cnpjs, errors

    records = _read_rows(raw, filename)
    cnpjs: list[str] = []
    errors: list[str] = []
    for i, row in enumerate(records, start=2):
        val = (row.get("cnpj") or "").strip()
        if not val and len(row) == 1:  # coluna única sem header 'cnpj'
            val = (next(iter(row.values()), "") or "").strip()
        digits = only_digits(val)
        if len(digits) != 14:
            errors.append(f"linha {i}: CNPJ inválido ({val!r})")
            continue
        cnpjs.append(digits)
    return cnpjs, errors


def parse_fb_accounts(raw: bytes, filename: str) -> tuple[list[dict[str, str]], list[str]]:
    """Lê contas FB de .csv/.xlsx (email/password/totp_seed + aliases) ou de
    .txt. O .txt aceita dois formatos, detectados automaticamente:

      * combolist: 1 conta por linha, campos separados por ':'/'|'/';'/tab;
      * blocos rotulados: cada conta é um bloco de linhas ``rótulo: valor``
        (login/senha/2FA/email/senha email), contas coladas em sequência.

    Retorna ``(contas, erros)`` — contas são dicts com email/password/totp_seed
    (mais email_address/email_password/recovery_email quando presentes)."""
    if (filename or "").lower().endswith(".txt"):
        lines = _read_lines(raw)
        # Formato de blocos rotulados (login:/senha:/2FA:/email:/senha email:)
        # tem prioridade quando detectado — senão, combolist de 1 linha/conta.
        if _looks_labeled(lines):
            return _parse_labeled_blocks(lines)
        accounts: list[dict[str, str]] = []
        errors: list[str] = []
        for i, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            acc = _parse_combo_line(line)
            if acc is None:
                errors.append(f"linha {i}: não reconheci login/senha/seed 2FA")
                continue
            accounts.append(acc)
        return accounts, errors

    records = _read_rows(raw, filename)
    accounts: list[dict[str, str]] = []
    errors: list[str] = []
    for i, row in enumerate(records, start=2):
        email = _pick(row, "email")
        password = _pick(row, "password")
        totp = _pick(row, "totp_seed")
        faltando = [
            nome for nome, val in
            (("email", email), ("password", password), ("totp_seed", totp))
            if not val
        ]
        if faltando:
            errors.append(f"linha {i}: campos vazios: {', '.join(faltando)}")
            continue
        accounts.append({"email": email, "password": password, "totp_seed": totp})
    return accounts, errors
