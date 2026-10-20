"""Motor de execução paralela de runs (fila + pool de workers).

Por que existe: o fluxo single-run rodava em 1 thread daemon que TRAVAVA a
sessão Streamlit. Pra rodar 2–3 contas ao mesmo tempo (meta: colher mais logs
e cobrir mais erros por rodada) sem travar a UI, o trabalho sai da thread do
Streamlit e vai pra um pool. A UI vira monitor: enfileira jobs e consulta o
:mod:`core.store` + os logs por-run.

Decisões (travadas com o usuário 2026-06-14): 2–3 em paralelo, pool
configurável começando baixo; UI Streamlit; store SQLite (já resolve a corrida
de arquivo dos JSON de estado, que também ganharam lock).

Isolamento de log: handlers de logging são GLOBAIS, então 2 workers no mesmo
processo escreveriam um no log do outro. Cada worker anexa um ``FileHandler``
com um filtro por ``thread id`` — só passam os registros emitidos pela própria
thread. Anexa no início do job, remove no fim.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Optional

from config import STATE_DIR
from core import store

logger = logging.getLogger("meta_automation.job_manager")

_MAX_TRANSIENT_RETRIES = 1


def _is_transient_error(exc: Exception) -> bool:
    from core.errors import CdpConnectionError, InfrastructureError, UIError
    return isinstance(exc, (InfrastructureError, CdpConnectionError, UIError))

RUNS_DIR = STATE_DIR / "runs"

# work_fn(run_id, spec) -> result (qualquer objeto com business_id/waba_id/
# meta_token/current_step) | levanta exceção em caso de falha.
WorkFn = Callable[[str, Any], Any]


def run_log_path(run_id: str) -> Path:
    """Caminho do log isolado de um run (``.state/runs/<run_id>/run.log``)."""
    d = RUNS_DIR / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d / "run.log"


def read_run_log(run_id: str, *, tail: int = 400) -> str:
    """Últimas ``tail`` linhas do log de um run (para o monitor da UI)."""
    path = RUNS_DIR / run_id / "run.log"
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-tail:])


# Marcadores de parada explícita ANTES do fim (não são 100% do fluxo).
# ⚠ "boot" foi DELIBERADAMENTE removido daqui: o context manager `step()`
# (utils/logging_utils) restaura `current_step` ao valor anterior no finally ao
# sair de CADA etapa — e como toda etapa top-level tem previous="boot", TODO run
# termina com current_step="boot", inclusive os que fecharam 100%. Com "boot" na
# lista, NENHUM run jamais virava success (DB tinha 0 success, só incomplete) —
# o operador via run completo marcado como incompleto. A completude REAL é
# decidida pelas flags abaixo (all(...)), não pelo current_step.
_INCOMPLETE_STEPS = {
    "account_discarded", "portfolio_limit_reached", "business_restricted",
    "datacrazy_skipped", "whatsapp_blocked_domain_unverified",
    # Passo 4 (conta de anúncios) esgotou as re-tentativas: o pipeline segue
    # para a verificação, mas a run NÃO é 100%.
    "ad_account_failed",
}


def _run_is_complete(result: Any) -> bool:
    """True só quando o run fechou 100% do fluxo de provisionamento.

    "100%" = portfólio criado + detalhes salvos + domínio verificado +
    CONTA DE ANÚNCIOS criada + verificação da empresa iniciada E código SMS
    recebido. Qualquer parada antes disso (conta descartada, SMS timeout…)
    é INCOMPLETE.

    Modalidade ``stopped_at_ad_account``: 100% = portfólio + detalhes +
    domínio + conta de anúncios. Verificação e SMS NÃO são exigidos.

    A completude é ditada pelas flags do resultado — NÃO pelo current_step
    (que o `step()` reverte p/ "boot" no fim de toda run; ver nota acima).
    """
    if getattr(result, "current_step", None) in _INCOMPLETE_STEPS:
        return False
    if getattr(result, "stopped_at_ad_account", False):
        return all((
            getattr(result, "business_id", None),
            getattr(result, "business_details_saved", False),
            getattr(result, "domain_verified", False),
            (
                getattr(result, "ad_account_id", None)
                or getattr(result, "ad_account_created", False)
            ),
        ))
    return all((
        getattr(result, "business_id", None),
        getattr(result, "business_details_saved", False),
        getattr(result, "domain_verified", False),
        (
            getattr(result, "ad_account_id", None)
            or getattr(result, "ad_account_created", False)
        ),
        getattr(result, "business_verification_started", False),
        getattr(result, "sms_verified", False),  # SMS DEVE SER VERIFICADO
    ))


class _ThreadLogFilter(logging.Filter):
    """Só deixa passar logs emitidos pela thread do worker (isola runs)."""

    def __init__(self, thread_id: int) -> None:
        super().__init__()
        self.thread_id = thread_id

    def filter(self, record: logging.LogRecord) -> bool:
        return record.thread == self.thread_id


def _attach_run_logger(path: Path) -> logging.Handler:
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.addFilter(_ThreadLogFilter(threading.get_ident()))
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    pl = logging.getLogger("meta_automation")
    pl.addHandler(handler)
    if pl.level == logging.NOTSET or pl.level > logging.INFO:
        pl.setLevel(logging.INFO)
    return handler


def _detach_run_logger(handler: logging.Handler) -> None:
    try:
        logging.getLogger("meta_automation").removeHandler(handler)
        handler.close()
    except Exception:  # noqa: BLE001
        pass


class JobManager:
    """Pool de workers que roda jobs em paralelo, com limite configurável."""

    def __init__(self, concurrency: int = 2) -> None:
        # ``concurrency`` agora é só informativo (não há fila): cada run sobe
        # NA HORA, numa thread dedicada (pedido do operador — nunca ficar em
        # 'fila'). Mantido p/ exibição no painel.
        self.concurrency = max(1, int(concurrency))
        self._executor = ThreadPoolExecutor(
            max_workers=max(self.concurrency, 32), thread_name_prefix="job"
        )
        self._futures: dict[str, Future] = {}
        self._active: set[str] = set()
        self._lock = threading.Lock()
        # Callback opcional disparado quando a ÚLTIMA run ativa termina (fica
        # ocioso = fim de uma "leva"). Usado pelo Piloto Automático p/ enfileirar
        # a próxima leva. Setado pelo api_server; nunca derruba a run se falhar.
        self.on_all_done: Optional[Callable[[], None]] = None

    # -- submissão ---------------------------------------------------------
    def submit(
        self,
        work_fn: WorkFn,
        *,
        spec: Any = None,
        cnpj: Optional[str] = None,
        label: Optional[str] = None,
        start_delay_s: float = 0.0,
    ) -> str:
        """Dispara um job. Retorna o ``run_id``.

        SEM FILA (pedido do operador): cada run sobe NA HORA numa thread
        dedicada — nunca fica em 'fila' esperando slot, mesmo que muitas rodem
        ao mesmo tempo. ``_run_job`` marca RUN_RUNNING logo no início.

        ``start_delay_s``: ESCALONA o início real da run (fica 'na fila' esse
        tempo antes de começar). Usado p/ não disparar várias runs no MESMO
        instante — evita a corrida no login/2FA/captcha do Facebook que fazia o
        Business Suite dar bounce/timeout (produção 2026-09, 2 runs juntas).
        """
        run_id = store.create_run(cnpj=cnpj, label=label, status=store.RUN_QUEUED)
        t = threading.Thread(
            target=self._run_job,
            args=(run_id, work_fn, spec, cnpj, label, start_delay_s),
            name=f"job-{run_id[:8]}",
            daemon=True,
        )
        t.start()
        return run_id

    # Alias histórico: 'rodar na hora' agora é o comportamento padrão de submit.
    submit_now = submit

    def _run_job(
        self,
        run_id: str,
        work_fn: WorkFn,
        spec: Any,
        cnpj: Optional[str],
        label: Optional[str],
        start_delay_s: float = 0.0,
    ) -> Any:
        # Escalonamento: espera o delay ANTES de começar (fica QUEUED). Evita
        # disparar várias runs no mesmo instante (corrida no FB → bounce/timeout).
        if start_delay_s and start_delay_s > 0:
            logger.info(
                "job %s: início escalonado — aguardando %.0fs antes de começar "
                "(evita corrida com outras runs).", run_id, start_delay_s,
            )
            try:
                time.sleep(start_delay_s)
            except Exception:  # noqa: BLE001
                pass
        with self._lock:
            self._active.add(run_id)
        store.update_run(run_id, status=store.RUN_RUNNING)
        handler = _attach_run_logger(run_log_path(run_id))
        try:
            result = None
            for _attempt in range(_MAX_TRANSIENT_RETRIES + 1):
                try:
                    result = work_fn(run_id, spec)
                    break
                except Exception as exc:
                    if (
                        _attempt < _MAX_TRANSIENT_RETRIES
                        and _is_transient_error(exc)
                    ):
                        logger.warning(
                            "job %s: erro transitório (%s) — auto-retry %d/%d em 15s…",
                            run_id,
                            type(exc).__name__,
                            _attempt + 1,
                            _MAX_TRANSIENT_RETRIES,
                        )
                        store.update_run(
                            run_id,
                            error=f"[retry {_attempt + 1}] {str(exc)[:300]}",
                        )
                        time.sleep(15)
                        store.update_run(run_id, status=store.RUN_RUNNING)
                        continue
                    raise
            if result is None:
                store.update_run(
                    run_id, status=store.RUN_FAILED, error="run sem resultado"
                )
                return None
            # ASSUMIR: o operador assumiu a run — mantém o status 'assumed' que
            # a API gravou (sem isso o painel sobrescrevia com 'incompleto' e o
            # botão "Continuar" deixava de funcionar).
            if getattr(result, "assumed", False):
                store.update_run(
                    run_id,
                    status=store.RUN_ASSUMED,
                    step=getattr(result, "current_step", None) or "assumed",
                )
                logger.info("job %s: ASSUMIDA pelo operador — bot parado.", run_id)
                return result
            # ENCERRAR: o operador já mandou fechar (endpoint /terminate já
            # gravou status=cancelled, descartou conta+CNPJ e apagou o perfil
            # AdsPower). Sem este corte, _persist_success rodava depois e
            # SOBRESCREVIA 'cancelled' com 'incomplete' — a run reaparecia na
            # aba "Incompletos" com o botão "Continuar (mesmo CNPJ)" ativo, que
            # reabre o navegador e loga de novo na MESMA conta que o operador
            # acabou de encerrar. Fica só o log; o status final é o da API.
            if getattr(result, "terminated", False):
                logger.info(
                    "job %s: ENCERRADA pelo operador — status 'cancelled' da "
                    "API preservado (sem re-persistir como incompleto).",
                    run_id,
                )
                return result
            self._persist_success(run_id, result, cnpj, label)
            return result
        except Exception as exc:  # noqa: BLE001
            logger.exception("job %s falhou", run_id)
            store.update_run(run_id, status=store.RUN_FAILED, error=str(exc)[:500])
            return None
        finally:
            _detach_run_logger(handler)
            with self._lock:
                self._active.discard(run_id)
                idle = len(self._active) == 0
            # Fim de leva: se ninguém mais está rodando, avisa o Piloto Automático
            # (best-effort — nunca propaga erro pro término da run).
            if idle and self.on_all_done:
                try:
                    self.on_all_done()
                except Exception:  # noqa: BLE001
                    logger.warning("on_all_done (autopilot) falhou", exc_info=True)

    @staticmethod
    def _persist_success(
        run_id: str, result: Any, cnpj: Optional[str], label: Optional[str]
    ) -> None:
        biz = getattr(result, "business_id", None)
        waba = getattr(result, "waba_id", None)
        ad_account = getattr(result, "ad_account_id", None)
        token = getattr(result, "meta_token", None)
        # "Sucesso" só quando o run fechou 100% do fluxo; do contrário, o run
        # rodou sem erro mas parou no meio → INCOMPLETE (não conta como 2k).
        complete = _run_is_complete(result)
        status = store.RUN_SUCCESS if complete else store.RUN_INCOMPLETE
        # current_step volta a "boot" no fim (step() restaura — ver _INCOMPLETE_STEPS);
        # persistir "boot" num run que fechou 100% confunde o painel → grava um passo
        # terminal claro p/ o sucesso.
        final_step = "success" if complete else getattr(result, "current_step", None)
        store.update_run(
            run_id,
            status=status,
            business_id=biz,
            waba_id=waba,
            ad_account_id=ad_account,
            meta_token=token,
            # Prova ao vivo (_verify_on_capture): o token capturado autenticou na
            # Graph API? Persiste p/ a auditoria saber se a captura é UTILIZÁVEL,
            # não só presente (antes só ia pro log, que rotaciona).
            verify_token_ok=int(bool(getattr(result, "verify_token_ok", False))),
            bm_invite_sent=int(bool(getattr(result, "bm_invite_sent", False))),
            bm_invite_email=getattr(result, "bm_invite_email", None),
            step=final_step,
        )
        if biz:
            store.upsert_bm(
                business_id=biz,
                cnpj=cnpj,
                portfolio_name=label,
                # ``label`` é o e-mail da conta nos dois fluxos (enqueue/manual)
                # → vincula a BM à conta que a criou. Senha/2FA NÃO ficam aqui:
                # são lidas de fb_accounts no read (fonte única), p/ a observação
                # na UI se manter sempre em dia.
                account_email=label,
                waba_id=waba,
                ad_account_id=ad_account,
                meta_token=token,
                # token durável = o meta_token é um System User token (permanente)
                # → o re-check consulta a Graph sem reabrir o browser.
                token_durable=int(bool(getattr(result, "system_user_token", None))),
                last_run_id=run_id,
            )
            # Status inicial de verificação consultado NA HORA (token fresco, via
            # _verify_on_capture) — registra p/ o painel já mostrar 'pending'; o
            # refresh/re-check atualiza quando a Meta aprovar.
            vs = getattr(result, "verification_status", None)
            ws = getattr(result, "waba_review_status", None)
            if vs or ws:
                from core import meta_graph  # noqa: PLC0415

                detail = f"{vs or '?'} · waba:{ws}" if ws else (vs or "?")
                store.set_bm_tier(
                    biz, tier=meta_graph.classify_verification(vs), detail=detail
                )

        # 2 BMs por perfil: grava também as BMs ANTERIORES concluídas na MESMA
        # sessão (result.extra_bms) — cada uma vira uma linha na tabela de BMs.
        for _bm in getattr(result, "extra_bms", None) or []:
            _biz = _bm.get("business_id")
            if not _biz:
                continue
            try:
                store.upsert_bm(
                    business_id=_biz,
                    cnpj=_bm.get("cnpj") or None,
                    portfolio_name=label,
                    account_email=label,
                    waba_id=_bm.get("waba_id"),
                    ad_account_id=_bm.get("ad_account_id"),
                    meta_token=_bm.get("meta_token"),
                    token_durable=int(bool(_bm.get("system_user_token"))),
                    last_run_id=run_id,
                )
            except Exception:  # noqa: BLE001 — persistência best-effort
                logger.debug("2-BMs: falha ao gravar BM extra %s", _biz, exc_info=True)

    # -- estado ------------------------------------------------------------
    def active_count(self) -> int:
        """Quantas runs estão rodando AGORA (sem fila — todas começam na hora)."""
        with self._lock:
            return len(self._active)

    def is_idle(self) -> bool:
        return self.active_count() == 0

    def tracked_run_ids(self) -> list[str]:
        with self._lock:
            return list(self._active)

    def set_concurrency(self, n: int) -> bool:
        """Ajusta o limite de paralelismo.

        Cria um novo executor com o novo limite. Se houver jobs ativos no
        executor antigo, eles continuam rodando nele (shutdown wait=False) —
        não são cancelados nem órfãos. Jobs novos vão para o executor novo.
        Retorna True sempre (a aplicação é imediata).
        """
        n = max(1, int(n))
        if n == self.concurrency:
            return True
        old = self._executor
        self._executor = ThreadPoolExecutor(max_workers=n, thread_name_prefix="job")
        self.concurrency = n
        old.shutdown(wait=False)  # jobs em curso no executor antigo terminam naturalmente
        logger.info("JobManager: concorrência ajustada para %d.", n)
        return True

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False)
