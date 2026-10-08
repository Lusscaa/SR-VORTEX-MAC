"""Checagem PONTUAL de verificação com um token FRESCO (Camada 1 ao vivo).

Prova de certeza: o token capturado no run morre em ~2h, então a consulta de
verificação dias depois precisa de um token FRESCO. Aqui você cola um token
recém-inspecionado da página da conta (o seu método manual) e a ferramenta
consulta os DOIS sinais da Graph API — empresa (verification_status) e WABA
(account_review_status) — provando que o método mostra a validação.

Uso:
    # 1) na página da conta (business.facebook.com) aberta no perfil AdsPower,
    #    rode no Console do DevTools:
    #       copy((document.documentElement.outerHTML.match(/EA[A-Z][A-Za-z0-9_-]{25,}/)||['NAO ACHOU'])[0])
    # 2) salve o token (vai do clipboard p/ arquivo, NÃO aparece no chat):
    #       pbpaste > /tmp/fresh_token.txt
    # 3) consulte:
    #       .venv/bin/python tools/check_verification.py --cnpj 66545133000151

O token é lido de --token-file (default /tmp/fresh_token.txt) ou de
$META_FRESH_TOKEN. NUNCA é impresso.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# raiz do projeto no path quando rodado de tools/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import meta_graph, store  # noqa: E402


def _load_token(args: argparse.Namespace) -> str:
    if os.getenv("META_FRESH_TOKEN"):
        return os.environ["META_FRESH_TOKEN"].strip()
    p = Path(args.token_file)
    if p.exists():
        return p.read_text().strip()
    sys.exit(
        f"Token não encontrado. Cole-o em {args.token_file} (pbpaste > "
        f"{args.token_file}) ou exporte META_FRESH_TOKEN."
    )


def _targets(args: argparse.Namespace) -> list[dict]:
    bms = store.list_bms(success_only=True, limit=200)
    if args.cnpj:
        bms = [b for b in bms if b.get("cnpj") == args.cnpj]
        if not bms:
            sys.exit(f"Nenhuma BM 100% com CNPJ {args.cnpj}.")
    return bms


async def _check(bm: dict, token: str) -> None:
    biz, waba = bm["business_id"], bm.get("waba_id")
    print(f"\n=== CNPJ {bm.get('cnpj')} | biz={biz} | waba={waba} ===")
    bs, be = await meta_graph.fetch_business_verification(biz, token)
    print(
        f"  EMPRESA verification_status = {bs!r} | err={be!r} "
        f"| classify -> {meta_graph.classify_verification(bs)}"
    )
    if waba:
        ws, we = await meta_graph.fetch_waba_review(waba, token)
        print(f"  WABA    account_review_status = {ws!r} | err={we!r}")
    else:
        print("  WABA    (sem waba_id salvo)")
    if be == "TOKEN_EXPIRED" or (waba and we == "TOKEN_EXPIRED"):
        print("  >> TOKEN AINDA EXPIRADO: cole um token RECÉM-inspecionado.")
    elif bs or (waba and ws):
        print("  >> OK: a Graph API respondeu com status REAL (método funciona).")


async def _main_async(args: argparse.Namespace) -> None:
    token = _load_token(args)
    targets = _targets(args)
    print(f"Consultando {len(targets)} BM(s) com token fresco ({len(token)} chars).")
    for bm in targets:
        await _check(bm, token)


def main() -> None:
    ap = argparse.ArgumentParser(description="Checa verificação com token fresco.")
    ap.add_argument("--cnpj", help="so esta conta (senao, todas as BMs 100%%).")
    ap.add_argument("--token-file", default="/tmp/fresh_token.txt")
    asyncio.run(_main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
