"""Controle cooperativo de parada de runs (botões 'Encerrar', 'Assumir' e 'Continuar').

Uma thread de worker do ``JobManager`` NÃO é matável de fato em Python — então
a parada é COOPERATIVA: a API registra um pedido por ``run_id`` aqui, e o
``AutomationRunner`` checa em pontos seguros (topo do loop do AuthFSM, antes de
cada passo de negócio, no retry de passo) via :func:`check` e levanta
:class:`RunStopRequested`, que desenrola o pipeline e cai no ``finally`` de
limpeza.

Três modos:

- ``terminate`` — operador encerrou a run de vez. O ``finally`` FECHA o perfil
  AdsPower; a API já descartou conta+CNPJ.
- ``assume``    — operador vai assumir manualmente. O ``finally`` MANTÉM o
  perfil aberto; conta+CNPJ ficam como estão (o operador está usando).
- ``resume``    — operador terminou intervenção manual, quer continuar. A thread
  acorda e prossegue (não para).

Thread-safe (lock simples). O registro é em memória do processo do backend —
some no restart, o que é o comportamento desejado (não persistir pedidos).
"""
from __future__ import annotations

import threading
from typing import Optional

TERMINATE = "terminate"
ASSUME = "assume"
RESUME = "resume"
RELEASE_ACCOUNT = "release_account"  # Libera conta sem descartar, descarta CNPJ

_LOCK = threading.Lock()
_REQUESTS: dict[str, str] = {}
_RESUME_REQUESTS: set[str] = set()  # Runs que foram solicitadas para continuar
# (era `{}` — um DICT — e qualquer .add()/.discard() estourava AttributeError,
#  quebrando 'Continuar' e o próprio registro do 'Assumir'.)
# Runs ASSUMIDAS pelo operador. Diferente de ``_REQUESTS`` (que é consumido
# pelo checkpoint), este flag é PEGAJOSO: dura até o fim da run. É ele que
# garante que NENHUM caminho de erro/limpeza feche o navegador, apague o
# perfil AdsPower, descarte a conta/CNPJ ou re-tente algo depois do clique
# em "Assumir".
_ASSUMED: set[str] = set()
# Runs que o operador mandou FECHAR ("Encerrar" / "Voltar Conta"). Também
# pegajoso: é o ÚNICO gatilho que fecha o navegador e apaga o perfil AdsPower.
# Sem ele, qualquer erro do bot fechava/apagava sozinho, e um erro de login
# logo depois do clique fazia o loop de retry ABRIR TUDO DE NOVO e re-logar.
_CLOSE_REQUESTED: set[str] = set()


class RunStopRequested(Exception):
    """Levantada pelo runner quando há pedido de parada para a run atual."""

    def __init__(self, run_id: str, mode: str) -> None:
        self.run_id = run_id
        self.mode = mode
        super().__init__(f"run {run_id}: parada solicitada pelo operador ({mode})")


def request_stop(run_id: Optional[str], mode: str) -> bool:
    """Registra um pedido de parada (``terminate``, ``assume`` ou ``release_account``) para ``run_id``.

    Devolve ``True`` se registrou. No-op (``False``) se ``run_id`` vazio ou modo
    inválido."""
    if not run_id or mode not in (TERMINATE, ASSUME, RELEASE_ACCOUNT):
        return False
    with _LOCK:
        _REQUESTS[run_id] = mode
        if mode == ASSUME:
            # Flag pegajoso — ver comentário em _ASSUMED.
            _ASSUMED.add(run_id)
            _RESUME_REQUESTS.discard(run_id)
        if mode in (TERMINATE, RELEASE_ACCOUNT):
            _CLOSE_REQUESTED.add(run_id)
            _RESUME_REQUESTS.discard(run_id)
    return True


def get_stop(run_id: Optional[str]) -> Optional[str]:
    """Modo de parada pedido para ``run_id`` (ou ``None`` se não há pedido).

    Se a run foi marcada para RESUME, retorna None (como se não houvesse parada)."""
    if not run_id:
        return None
    with _LOCK:
        # Se foi solicitado resume, ignora o pedido de parada anterior
        if run_id in _RESUME_REQUESTS:
            _RESUME_REQUESTS.discard(run_id)
            _REQUESTS.pop(run_id, None)
            return None
        return _REQUESTS.get(run_id)


def request_resume(run_id: Optional[str]) -> bool:
    """Marca uma run para CONTINUAR após ter sido assumida.

    A próxima vez que _check_stop() for chamada, ao invés de parar,
    ela retorna None (continua normalmente)."""
    if not run_id:
        return False
    with _LOCK:
        _RESUME_REQUESTS.add(run_id)
        # Operador quer o bot de volta — "assumir"/"encerrar" deixam de valer.
        _ASSUMED.discard(run_id)
        _CLOSE_REQUESTED.discard(run_id)
    return True


def is_assume_requested(run_id: Optional[str]) -> bool:
    """True se o operador clicou "Assumir" nesta run (flag PEGAJOSO).

    Ao contrário de :func:`get_stop`, não é consumido: continua True até a run
    terminar (ou até um "Continuar"). Todo caminho de erro/limpeza consulta
    isto antes de fechar navegador, apagar perfil ou re-tentar qualquer coisa."""
    if not run_id:
        return False
    with _LOCK:
        return run_id in _ASSUMED


def is_close_requested(run_id: Optional[str]) -> bool:
    """True se o operador clicou "Encerrar" (ou "Voltar Conta") nesta run.

    Pegajoso, igual ao de "assumir": vale até o fim da run. O runner usa isto
    para (a) NÃO re-tentar/rotacionar nada depois do clique e (b) fechar o
    navegador e apagar o perfil SOMENTE nesse caso."""
    if not run_id:
        return False
    with _LOCK:
        return run_id in _CLOSE_REQUESTED


def is_resume_requested(run_id: Optional[str]) -> bool:
    """Verifica se há um RESUME pedido (reiniciar sem fechar browser/AdsPower).

    Útil para detectar restart e reutilizar o perfil AdsPower existente
    ao invés de criar um novo."""
    if not run_id:
        return False
    with _LOCK:
        return run_id in _RESUME_REQUESTS


def clear(run_id: Optional[str]) -> None:
    """Remove o pedido (chamado no fim da run, pelo finalizador)."""
    if not run_id:
        return
    with _LOCK:
        _REQUESTS.pop(run_id, None)
        _RESUME_REQUESTS.discard(run_id)
        _ASSUMED.discard(run_id)
        _CLOSE_REQUESTED.discard(run_id)
