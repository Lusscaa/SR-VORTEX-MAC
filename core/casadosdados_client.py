"""Cliente da API oficial do Casa dos Dados — PESQUISA avançada de CNPJ.

Diferente das APIs de CONSULTA (BrasilAPI/ReceitaWS/CNPJá/CNPJ.ws, que dão os
dados de UM CNPJ), esta faz DESCOBERTA: recebe filtros e devolve uma LISTA de
CNPJs. Usada pelo gerador de lista no painel ("Disparar").

Endpoint: POST https://api.casadosdados.com.br/v5/cnpj/pesquisa
Auth:     header ``api-key`` (paga — ~R$0,01/CNPJ; trial 7d/200).
Doc:      https://docs.casadosdados.com.br/pesquisa-avançada-de-empresas
"""
from __future__ import annotations

import os
from typing import Any, Optional

import httpx

SEARCH_URL = "https://api.casadosdados.com.br/v5/cnpj/pesquisa"


class CasaDosDadosError(Exception):
    """Erro amigável da pesquisa (chave, saldo, HTTP)."""


def _api_key() -> str:
    return (os.getenv("CASADOSDADOS_API_KEY") or "").strip()


def build_body(
    *,
    nome: Optional[str] = None,
    somente_mei: bool = False,
    somente_matriz: bool = False,
    situacao_ativa: bool = True,
    capital_min: Optional[float] = None,
    capital_max: Optional[float] = None,
    abertura_de: Optional[str] = None,   # "YYYY-MM-DD"
    abertura_ate: Optional[str] = None,  # "YYYY-MM-DD"
    uf: Optional[str] = None,
    limite: int = 20,
    pagina: int = 1,
) -> dict[str, Any]:
    """Monta o corpo do POST /v5/cnpj/pesquisa só com os filtros preenchidos."""
    body: dict[str, Any] = {
        "limite": max(1, min(1000, int(limite or 20))),
        "pagina": max(1, int(pagina or 1)),
    }
    if nome and nome.strip():
        body["busca_textual"] = [{
            "texto": [nome.strip()],
            "tipo_busca": "radical",
            "razao_social": True,
            "nome_fantasia": True,
            "nome_socio": False,
        }]
    if situacao_ativa:
        body["situacao_cadastral"] = ["ATIVA"]
    if somente_matriz:
        body["matriz_filial"] = "MATRIZ"
    if somente_mei:
        body["mei"] = {"optante": True}
    cap: dict[str, Any] = {}
    # ATENÇÃO: a API exige INTEIRO em capital_social — float (ex.: 200.0) → HTTP 400.
    if capital_min not in (None, ""):
        cap["minimo"] = int(float(capital_min))
    if capital_max not in (None, ""):
        cap["maximo"] = int(float(capital_max))
    if cap:
        body["capital_social"] = cap
    ab: dict[str, Any] = {}
    if abertura_de:
        ab["inicio"] = abertura_de
    if abertura_ate:
        ab["fim"] = abertura_ate
    if ab:
        body["data_abertura"] = ab
    if uf and uf.strip():
        body["uf"] = [uf.strip().lower()]
    return body


async def search(body: dict[str, Any], *, tipo_resultado: str = "simples", timeout: float = 30.0) -> dict[str, Any]:
    """Chama a pesquisa e devolve o JSON cru ({total, cnpjs:[...]}). Levanta
    CasaDosDadosError com mensagem amigável em erro de chave/saldo/HTTP."""
    key = _api_key()
    if not key:
        raise CasaDosDadosError("Chave do Casa dos Dados não configurada — cole em APIs → Casa dos Dados.")
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(
                SEARCH_URL,
                params={"tipo_resultado": tipo_resultado},
                headers={"api-key": key, "Content-Type": "application/json"},
                json=body,
            )
    except Exception as exc:  # noqa: BLE001 — rede
        raise CasaDosDadosError(f"Falha de conexão com o Casa dos Dados: {exc}")
    if r.status_code in (401, 403):
        raise CasaDosDadosError("Chave inválida ou sem permissão (api-key).")
    if r.status_code == 402:
        raise CasaDosDadosError("Sem saldo/créditos no Casa dos Dados.")
    if r.status_code == 429:
        raise CasaDosDadosError("Muitas requisições — aguarde um pouco e tente de novo.")
    if r.status_code == 400:
        body_txt = (r.text or "").strip()
        raise CasaDosDadosError(
            f"Filtros recusados pela API (HTTP 400){': ' + body_txt[:200] if body_txt else ' — revise capital social e datas.'}"
        )
    if r.status_code != 200:
        raise CasaDosDadosError(f"HTTP {r.status_code}: {(r.text or '')[:200]}")
    try:
        return r.json() or {}
    except Exception:  # noqa: BLE001
        raise CasaDosDadosError("Resposta inválida do Casa dos Dados.")
