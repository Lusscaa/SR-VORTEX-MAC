#!/usr/bin/env python3
"""
Meta-Farm - Limpar Contas Descartadas e Pendentes de Descarte
Remove todas as contas com status POOL_DISCARDED ou POOL_PENDING_DISCARD
"""

import sys
from pathlib import Path

# Adiciona a pasta current ao path
sys.path.insert(0, str(Path(__file__).parent))

from core.store import (
    init_db,
    _conn,
    _POOL_LOCK,
    delete_fb_accounts,
    POOL_DISCARDED,
    POOL_PENDING_DISCARD,
)
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger(__name__)


def get_discarded_accounts() -> dict:
    """Retorna dicts com contas descartadas e pendentes de descarte."""
    init_db()

    discarded = []
    pending = []

    with _POOL_LOCK, _conn() as conn:
        # Contas DESCARTADAS
        rows = conn.execute(
            "SELECT email, cnpj, status, created_at FROM fb_accounts WHERE status = ?",
            (POOL_DISCARDED,)
        ).fetchall()
        discarded = [dict(r) for r in rows]

        # Contas PENDENTES DE DESCARTE
        rows = conn.execute(
            "SELECT email, cnpj, status, created_at FROM fb_accounts WHERE status = ?",
            (POOL_PENDING_DISCARD,)
        ).fetchall()
        pending = [dict(r) for r in rows]

    return {"discarded": discarded, "pending": pending}


def limpar_contas():
    """Remove todas as contas descartadas e pendentes."""

    print("\n" + "="*70)
    print("  Meta-Farm - Limpar Contas Descartadas")
    print("="*70 + "\n")

    # Busca contas
    contas = get_discarded_accounts()
    discarded = contas["discarded"]
    pending = contas["pending"]

    total = len(discarded) + len(pending)

    if total == 0:
        print("✅ Nenhuma conta descartada ou pendente encontrada!")
        print("   Banco de dados limpo.\n")
        return

    # Mostra resumo
    print(f"📊 RESUMO:")
    print(f"   Descartadas (POOL_DISCARDED):        {len(discarded)}")
    print(f"   Pendentes (POOL_PENDING_DISCARD):   {len(pending)}")
    print(f"   Total:                               {total}\n")

    if discarded:
        print("📋 DESCARTADAS:")
        for acc in discarded[:10]:  # Mostra primeiras 10
            print(f"   • {acc['email']:<40} (CNPJ: {acc['cnpj']})")
        if len(discarded) > 10:
            print(f"   ... e mais {len(discarded) - 10}")
        print()

    if pending:
        print("⏳ PENDENTES DE DESCARTE:")
        for acc in pending[:10]:  # Mostra primeiras 10
            print(f"   • {acc['email']:<40} (CNPJ: {acc['cnpj']})")
        if len(pending) > 10:
            print(f"   ... e mais {len(pending) - 10}")
        print()

    # Confirmação
    print("⚠️  AVISO: Esta ação é IRREVERSÍVEL!")
    print("   Você está prestes a DELETAR permanentemente:")
    print(f"   → {total} conta(s) do banco de dados\n")

    response = input("Deseja continuar? (sim/nao): ").strip().lower()

    if response not in ("sim", "s", "yes", "y"):
        print("\n❌ Operação cancelada.\n")
        return

    # Coleta e-mails
    emails = [acc["email"] for acc in discarded + pending]

    # Deleta
    print(f"\n🗑️  Deletando {len(emails)} conta(s)...\n")
    deleted = delete_fb_accounts(emails)

    print(f"\n✅ SUCESSO!")
    print(f"   Deletadas: {deleted} conta(s)")
    print(f"   Banco de dados limpo.\n")


if __name__ == "__main__":
    try:
        limpar_contas()
    except KeyboardInterrupt:
        print("\n\n❌ Interrompido pelo usuário.\n")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ ERRO: {e}\n")
        logger.exception("Erro ao limpar contas")
        sys.exit(1)
