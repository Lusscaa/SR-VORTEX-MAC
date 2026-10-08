# meta-farm — regras para Claude Code

## SMS24h — número virtual

O número é virtual (numero-virtual.com) — aceitar **qualquer DDD** que a API
devolver, sem filtro. O operador quer usar exatamente o número gerado.

## Telefone no comprovante CNPJ (PDF do BV)

O PDF deve usar o **número SMS24h da run** (mesmo número que recebe o código
de verificação na Meta).

### Prioridade do telefone no PDF

1. `preallocated_sms_phone` (SMS24h desta run)
2. `COMPROVANTE_TELEFONE` no `.env` — **somente** se não houver SMS alocado
3. Telefone da Receita (fallback; evitar)

### Arquivos principais

- `core/comprovante_phone.py` — formata e resolve prioridade
- `core/api_clients.py` → `acquire_number()` — aquisição do número
- `core/automation.py` → `_bv_prepare_comprovante()` / `_bv_complete_verification()`
- `core/cnpj_reva.py` → `render_comprovante_local()` — invalida PDF se telefone mudou
