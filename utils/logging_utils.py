"""Logging utilities — redaction filter, step context manager, suppress helper.

Tudo aqui é retrocompatível: nenhum handler/log existente quebra se este
módulo não for importado. Os utilitários só agem quando explicitamente
instalados (RedactingFilter) ou usados (step / suppress).
"""
from __future__ import annotations

import json
import logging
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

# ---------------------------------------------------------------------------
# L3 — Redação de logs sensíveis
# ---------------------------------------------------------------------------
# Padrões cobertos:
#   * "Bearer <token>"                  → "Bearer ***"
#   * "Authorization: <anything>"       → "Authorization: ***"
#   * "X-Auth-Email: ..." / "Cookie: ..." (cabeçalhos)
#   * '"access_token": "..."'           (JSON)
#   * '"cookie_secret": "..."'
#   * "password=..."  / "api_key=..."  (querystring / form)
#   * chaves longas hex/base64 (>=24 chars) com prefixo conhecido
_REDACT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+"), r"\1***"),
    (re.compile(r"(?i)(authorization\s*:\s*)\S+"), r"\1***"),
    (re.compile(r"(?i)(cookie\s*:\s*)[^\r\n]+"), r"\1***"),
    (re.compile(r"(?i)(x-auth-(?:email|key|user-token)\s*:\s*)\S+"), r"\1***"),
    (
        re.compile(
            r'(?i)("?(?:access_token|api_token|api_key|cookie_secret|'
            r'client_secret|password|totp_seed)"?\s*[:=]\s*"?)'
            r"[^\s,\"'}{)]+"
        ),
        r"\1***",
    ),
)


class RedactingFilter(logging.Filter):
    """Mascara secrets em msg + args + exc_text antes do log sair do logger."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401
        try:
            # Resolve os args %-style ANTES de redigir: redigir o TEMPLATE
            # ("password=%s" → "password=***") removeria o placeholder e o
            # getMessage() posterior quebraria com TypeError ("not all
            # arguments converted"). Após resolver, args são zerados —
            # idempotente sob múltiplos filtros (logger + handler).
            record.msg = self._redact(record.getMessage())
            record.args = None
            if record.exc_text:
                record.exc_text = self._redact(record.exc_text)
        except Exception:  # noqa: BLE001 — filtro NUNCA pode quebrar o logging
            pass
        return True

    @staticmethod
    def _redact(text: str) -> str:
        for pattern, repl in _REDACT_PATTERNS:
            text = pattern.sub(repl, text)
        return text

    @classmethod
    def _redact_value(cls, value: object) -> object:
        if isinstance(value, str):
            return cls._redact(value)
        return value


def install_redaction(logger: logging.Logger) -> RedactingFilter:
    """Idempotente: adiciona o filtro apenas uma vez."""
    for f in logger.filters:
        if isinstance(f, RedactingFilter):
            return f
    flt = RedactingFilter()
    logger.addFilter(flt)
    return flt


# ---------------------------------------------------------------------------
# PR5 — Logging estruturado (key=value no console, JSONL em arquivo)
# ---------------------------------------------------------------------------
class StructuredFormatter(logging.Formatter):
    """Linha única ``key=value`` — grepável e parseável.

    Formato: ``ts=<iso> level=<LEVEL> logger=<name> msg="<mensagem>"``.
    Mensagens que já usam o padrão ``step=x status=y`` do ``step()``
    compõem naturalmente com o prefixo.
    """

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage().replace('"', "'")
        line = (
            f"ts={self.formatTime(record, '%Y-%m-%dT%H:%M:%S')} "
            f"level={record.levelname} logger={record.name} "
            f'msg="{message}"'
        )
        if record.exc_info:
            exc = self.formatException(record.exc_info).replace('"', "'")
            line += f' exc="{exc}"'
        return line


class JsonLinesFormatter(logging.Formatter):
    """Um objeto JSON por linha — ingestão direta em qualquer ferramenta."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


_STRUCTURED_MARKER = "_meta_automation_structured"


def install_structured_logging(
    name: str = "meta_automation",
    *,
    level: int = logging.INFO,
    jsonl_dir: Optional[Path] = None,
) -> logging.Logger:
    """Configura o logger-pai do core com saída estruturada. Idempotente.

    * Console: ``StructuredFormatter`` (key=value).
    * Arquivo (opcional): ``<jsonl_dir>/meta_<YYYYMMDD>.jsonl``.
    * Redação L3 instalada NOS HANDLERS — diferente do filtro por logger,
      o filtro em handler cobre também registros de loggers FILHOS
      (auth_fsm, saga, pages…), que não passam pelos filtros do pai.
    """
    parent = logging.getLogger(name)
    if getattr(parent, _STRUCTURED_MARKER, False):
        return parent

    console = logging.StreamHandler()
    console.setFormatter(StructuredFormatter())
    console.addFilter(RedactingFilter())
    parent.addHandler(console)

    if jsonl_dir is not None:
        try:
            jsonl_dir.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(
                jsonl_dir / f"meta_{time.strftime('%Y%m%d')}.jsonl",
                encoding="utf-8",
            )
            file_handler.setFormatter(JsonLinesFormatter())
            file_handler.addFilter(RedactingFilter())
            parent.addHandler(file_handler)
        except Exception as exc:  # noqa: BLE001 — log em arquivo é best-effort
            parent.warning("Logging JSONL indisponível: %s", exc)

    parent.setLevel(level)
    setattr(parent, _STRUCTURED_MARKER, True)
    return parent


# ---------------------------------------------------------------------------
# H1 — Step tracking
# ---------------------------------------------------------------------------
@contextmanager
def step(
    logger: logging.Logger,
    name: str,
    *,
    result: Optional[object] = None,
    log_start: bool = True,
    saga: Optional[object] = None,
) -> Iterator[None]:
    """Marca início/fim de uma etapa da esteira.

    * Loga start/ok/fail no formato estruturado
      ``step=<name> status=<state> duration_ms=<n>``.
    * Se ``result`` tiver atributo ``current_step`` (caso de ``AutomationResult``),
      atualiza enquanto o bloco está ativo — assim, qualquer falha externa
      consegue inspecionar em qual etapa estávamos.
    * PR4 — se ``saga`` for passada (PipelineSaga), registra a transição do
      passo (running/done/failed) no snapshot transacional persistido.
    * NÃO suprime a exceção — apenas anota e re-propaga.
    """
    previous = getattr(result, "current_step", None) if result is not None else None
    if result is not None and hasattr(result, "current_step"):
        try:
            result.current_step = name  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
    saga_entry = None
    if saga is not None:
        try:
            saga_entry = saga.step_started(name)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            saga_entry = None
    started = time.monotonic()
    if log_start:
        logger.info("→ step=%s status=start", name)
    try:
        yield
    except BaseException as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.error(
            "✗ step=%s status=fail duration_ms=%d err=%s: %s",
            name, elapsed_ms, type(exc).__name__, exc,
        )
        if saga_entry is not None:
            try:
                saga.step_finished(saga_entry, error=exc)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
        raise
    else:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.info("✓ step=%s status=ok duration_ms=%d", name, elapsed_ms)
        if saga_entry is not None:
            try:
                saga.step_finished(saga_entry)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
    finally:
        if result is not None and hasattr(result, "current_step") and previous is not None:
            try:
                result.current_step = previous  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# H3 — suppress com log
# ---------------------------------------------------------------------------
@contextmanager
def suppress_and_log(
    logger: logging.Logger,
    *exc_types: type[BaseException],
    reason: str,
    level: int = logging.DEBUG,
    step_name: Optional[str] = None,
) -> Iterator[None]:
    """Substitui ``try: ... except Foo: pass`` por uma supressão rastreável.

    Idêntico em comportamento (engole a exceção) mas registra ``reason`` +
    tipo/mensagem em DEBUG. Quando ``step_name`` é informado, vai pro log
    com o mesmo formato estruturado de ``step``.
    """
    if not exc_types:
        exc_types = (Exception,)
    try:
        yield
    except exc_types as exc:  # type: ignore[misc]
        prefix = f"step={step_name} " if step_name else ""
        logger.log(
            level,
            "%ssuppressed reason=%s err=%s: %s",
            prefix, reason, type(exc).__name__, exc,
        )
