"""Telefone exibido no comprovante CNPJ (PDF do passo BV).

Prioridade (maior → menor):
  1. Número pré-alocado via SMS24h (``preallocated_sms_phone``)
  2. ``COMPROVANTE_TELEFONE`` no ``.env`` — só se não houver SMS desta run
  3. Telefone da Receita Federal (sem override no render)

O SMS24h da run é a fonte principal: o mesmo número vai no PDF, no cadastro
Meta e na verificação por código.
"""
from __future__ import annotations

import os
import re
from typing import Optional

_ENV_KEY = "COMPROVANTE_TELEFONE"


def _digits(raw: str) -> str:
    return re.sub(r"\D+", "", raw or "")


def format_brazil_phone(raw: Optional[str]) -> Optional[str]:
    """Formata para ``(XX) XXXXX-XXXX`` (celular) ou ``(XX) XXXX-XXXX``."""
    digits = _digits(raw or "")
    if len(digits) >= 11:
        return f"({digits[-11:-9]}) {digits[-9:-4]}-{digits[-4:]}"
    if len(digits) >= 10:
        return f"({digits[-10:-8]}) {digits[-8:-4]}-{digits[-4:]}"
    stripped = (raw or "").strip()
    return stripped or None


def format_receita_phone(raw: Optional[str]) -> Optional[str]:
    """Formato Receita Federal: ``(DD) XXXX-XXXX`` (sempre 8 dígitos no assinante).

    Números com 11+ dígitos nacionais (DD + 9 dígitos) perdem o PRIMEIRO
    dígito do assinante — independente de qual seja (9, 5, 2, 8…). O
    prefixo ``55`` (DDI Brasil) é ignorado quando presente.

    Entrada aceita: ``77920057223``, ``5577920057223``, ``+5519982120535``.
    """
    digits = _digits(raw or "")
    if len(digits) >= 11:
        ddd = digits[-11:-9]
        return f"({ddd}) {digits[-8:-4]}-{digits[-4:]}"
    if len(digits) >= 10:
        return f"({digits[-10:-8]}) {digits[-8:-4]}-{digits[-4:]}"
    stripped = (raw or "").strip()
    return stripped or None


def get_manual_comprovante_phone() -> Optional[str]:
    """Override manual via ``COMPROVANTE_TELEFONE`` (somente dígitos ou formatado)."""
    raw = (os.getenv(_ENV_KEY) or "").strip()
    if not raw:
        return None
    return format_brazil_phone(raw)


def should_skip_sms_acquire_for_comprovante() -> bool:
    """True quando operador fixou telefone no .env — não gastar SMS24h só no PDF."""
    return get_manual_comprovante_phone() is not None


def resolve_comprovante_telefone(*, sms_phone_raw: Optional[str] = None) -> Optional[str]:
    """Telefone final para ``telefone_override`` do render do comprovante.

    Usa o formato Receita Federal — celulares perdem o 9º dígito:
    ``(DD) 9XXXX-XXXX`` → ``(DD) XXXX-XXXX``. O PDF do BV imita o
    documento oficial da Receita, que registra telefones no formato antigo.
    """
    sms = format_receita_phone(sms_phone_raw)
    if sms:
        return sms
    manual = get_manual_comprovante_phone()
    if manual:
        return format_receita_phone(manual) or manual
    return None
