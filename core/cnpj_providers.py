"""Provedores de consulta de CNPJ na Receita AO VIVO (fallback multi-fonte).

A BrasilAPI usa um dump defasado e devolve o número do endereço como "S/N".
Estes provedores consultam a Receita ao vivo e trazem o número real. Vários,
com fallback: quando um bloqueia/limita (HTTP 429, conta suspensa), o lookup
tenta o próximo — nunca dependemos de um só (lição do bloqueio da Infosimples).

Cada provedor devolve um dict NORMALIZADO com nomes de campo do
:class:`CNPJData` (ou ``None`` se não houver dados). Ordem: grátis sem token
primeiro (poupa cota paga); Infosimples por último.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger("meta_automation.cnpj_providers")

_TIMEOUT_S = 20.0

# Chaves normalizadas = campos do CNPJData (Optional[str], menos numero).
_KEYS = (
    "razao_social", "nome_fantasia", "situacao", "abertura",
    "logradouro", "numero", "complemento", "bairro", "cidade", "uf", "cep",
    "telefone", "email",
)


def _join_tipo_logradouro(tipo: Any, nome: Any) -> Optional[str]:
    """Junta 'RUA' + '608' → 'RUA 608' (provedores que separam o tipo).

    Devolve None quando sobra só um nome NUMÉRICO sem tipo ('608') — isso não
    é um logradouro utilizável e, sozinho, vira 'endereço só com número' no
    formulário da Meta.
    """
    t = str(tipo or "").strip()
    n = str(nome or "").strip()
    if not n:
        return None
    if t and not n.upper().startswith(t.upper()):
        return f"{t} {n}"
    if not t and not any(ch.isalpha() for ch in n):
        return None
    return n


def _clean(d: dict[str, Any]) -> dict[str, Any]:
    """Mantém só as chaves conhecidas, com strings strip()adas e não-vazias."""
    out: dict[str, Any] = {}
    for k in _KEYS:
        v = d.get(k)
        if isinstance(v, str):
            v = v.strip()
        if v:
            out[k] = v
    return out


async def _get_json(url: str, headers: Optional[dict[str, str]] = None) -> Any:
    async with httpx.AsyncClient(timeout=_TIMEOUT_S, follow_redirects=True) as c:
        r = await c.get(url, headers=headers or {})
        r.raise_for_status()
        return r.json()


async def from_receitaws(digits: str) -> Optional[dict[str, Any]]:
    """receitaws.com.br — grátis, sem token (limite ~3/min)."""
    d = await _get_json(f"https://receitaws.com.br/v1/cnpj/{digits}")
    if not isinstance(d, dict) or str(d.get("status", "")).upper() != "OK":
        return None
    return _clean({
        "razao_social": d.get("nome"), "nome_fantasia": d.get("fantasia"),
        "situacao": d.get("situacao"), "abertura": d.get("abertura"),
        "logradouro": d.get("logradouro"), "numero": d.get("numero"),
        "complemento": d.get("complemento"), "bairro": d.get("bairro"),
        "cidade": d.get("municipio"), "uf": d.get("uf"), "cep": d.get("cep"),
        "telefone": d.get("telefone"), "email": d.get("email"),
    })


async def from_cnpja_open(digits: str) -> Optional[dict[str, Any]]:
    """open.cnpja.com — grátis, sem token (limite ~5/min)."""
    d = await _get_json(f"https://open.cnpja.com/office/{digits}")
    if not isinstance(d, dict):
        return None
    addr = d.get("address") or {}
    phones = d.get("phones") or []
    emails = d.get("emails") or []
    tel = None
    if phones:
        p = phones[0]
        tel = f"({p.get('area', '')}) {p.get('number', '')}".strip()
    return _clean({
        "razao_social": (d.get("company") or {}).get("name"),
        "nome_fantasia": d.get("alias"),
        "situacao": (d.get("status") or {}).get("text"),
        "abertura": d.get("founded"),
        "logradouro": addr.get("street"), "numero": addr.get("number"),
        "complemento": addr.get("details"), "bairro": addr.get("district"),
        "cidade": addr.get("city"), "uf": addr.get("state"), "cep": addr.get("zip"),
        "telefone": tel,
        "email": emails[0].get("address") if emails else None,
    })


async def from_cnpjws(digits: str) -> Optional[dict[str, Any]]:
    """publica.cnpj.ws — grátis, sem token (limite ~3/min).

    O ``logradouro`` vem SEM o prefixo do tipo ('608' em vez de 'RUA 608') —
    por isso juntamos ``tipo_logradouro`` + ``logradouro`` aqui (2026-09).
    """
    d = await _get_json(f"https://publica.cnpj.ws/cnpj/{digits}")
    if not isinstance(d, dict):
        return None
    est = d.get("estabelecimento") or {}
    ddd, tel1 = est.get("ddd1"), est.get("telefone1")
    tel = f"({ddd}) {tel1}" if ddd and tel1 else (tel1 or None)
    return _clean({
        "razao_social": d.get("razao_social"),
        "nome_fantasia": est.get("nome_fantasia"),
        "situacao": est.get("situacao_cadastral"),
        "abertura": est.get("data_inicio_atividade"),
        "logradouro": _join_tipo_logradouro(
            est.get("tipo_logradouro"), est.get("logradouro")
        ),
        "numero": est.get("numero"),
        "complemento": est.get("complemento"), "bairro": est.get("bairro"),
        "cidade": (est.get("cidade") or {}).get("nome"),
        "uf": (est.get("estado") or {}).get("sigla"),
        "cep": est.get("cep"),
        "telefone": tel, "email": est.get("email"),
    })


async def from_infosimples(digits: str) -> Optional[dict[str, Any]]:
    """Infosimples (paga, token) — última opção. None se sem token/desabilitada."""
    from core.cnpj_reva import InfosimplesCnpjClient  # noqa: PLC0415
    from config import INFOSIMPLES_BACKFILL_TIMEOUT_S  # noqa: PLC0415

    client = InfosimplesCnpjClient(
        server_timeout_s=INFOSIMPLES_BACKFILL_TIMEOUT_S,
        timeout_s=INFOSIMPLES_BACKFILL_TIMEOUT_S + 10,
    )
    if not client.enabled:
        return None
    d = await client.consultar_dados(digits)
    if not isinstance(d, dict):
        return None
    return _clean({
        "razao_social": d.get("razao_social"), "nome_fantasia": d.get("nome_fantasia"),
        "situacao": d.get("situacao_cadastral"), "abertura": d.get("abertura_data"),
        "logradouro": d.get("endereco_logradouro"), "numero": d.get("endereco_numero"),
        # complemento da Infosimples vem mascarado ("****") p/ MEI — descartado em _clean? não.
        "complemento": (d.get("endereco_complemento") or "").replace("*", "").strip() or None,
        "bairro": d.get("endereco_bairro"), "cidade": d.get("endereco_municipio"),
        "uf": d.get("endereco_uf"), "cep": d.get("endereco_cep"),
        "telefone": d.get("telefone"), "email": d.get("email"),
    })


# Ordem do fallback: grátis (sem token) primeiro; Infosimples por último.
PROVIDERS = (from_receitaws, from_cnpja_open, from_cnpjws, from_infosimples)


async def fetch_live(
    digits: str, *, want: tuple[str, ...] = ("numero",)
) -> Optional[dict[str, Any]]:
    """Tenta cada provedor em ordem e MESCLA o que cada um trouxe.

    Best-effort: erros/limites de um provedor pulam pro próximo.

    ``want`` = chaves que o chamador precisa; a varredura para assim que TODAS
    estiverem preenchidas. Antes a função devolvia o 1º provedor que tivesse
    ``numero`` e parava ali — quando esse provedor vinha SEM logradouro
    (comum em MEI rural), os provedores seguintes, que tinham a rua, nunca
    eram consultados e o endereço ficava só com o número (produção 2026-09,
    print do operador: 'Endereço: 2320'). Agora o merge preenche cada buraco
    com o 1º provedor que souber respondê-lo.
    """
    merged: dict[str, Any] = {}
    used: list[str] = []
    for prov in PROVIDERS:
        try:
            got = await prov(digits)
        except Exception as exc:  # noqa: BLE001 — bloqueio/limite/erro → próximo
            logger.warning("CNPJ %s: provedor %s falhou (%s) — próximo.",
                           digits, prov.__name__, exc)
            continue
        if not got:
            continue
        novo = False
        for k, v in got.items():
            if v and not merged.get(k):
                merged[k] = v
                novo = True
        if novo:
            used.append(prov.__name__)
        if all(merged.get(k) for k in want):
            logger.info(
                "CNPJ %s: dados ao vivo via %s (numero=%s, logradouro=%s).",
                digits, "+".join(used),
                merged.get("numero"), merged.get("logradouro"),
            )
            return merged
    if merged:
        logger.info(
            "CNPJ %s: dados ao vivo PARCIAIS via %s (faltou: %s).",
            digits, "+".join(used) or "-",
            ", ".join(k for k in want if not merged.get(k)) or "-",
        )
    return merged or None
