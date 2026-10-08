"""Dialog interativo que aparece quando há erro — oferece voltar 1 passo antes de fechar."""
from __future__ import annotations

import asyncio
import logging
from playwright.async_api import Page, Error as PlaywrightError

logger = logging.getLogger(__name__)


async def show_error_recovery_dialog(
    page: Page,
    error_msg: str,
    step_name: str = "desconhecido"
) -> bool:
    """Mostra um diálogo interativo quando há erro.

    Pergunta ao operador: "Deseja voltar 1 passo e tentar de novo?"

    Retorna:
        True = operador clicou "Tentar de novo" (volta 1 passo)
        False = operador clicou "Fechar" (encerra mesmo assim)
    """

    # HTML do diálogo em tela cheia
    dialog_html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <style>
            * {{
                margin: 0;
                padding: 0;
                box-sizing: border-box;
            }}
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%);
                display: flex;
                align-items: center;
                justify-content: center;
                height: 100vh;
                width: 100vw;
                margin: 0;
                padding: 0;
            }}
            .container {{
                background: white;
                border-radius: 16px;
                box-shadow: 0 20px 60px rgba(0,0,0,0.3);
                padding: 40px;
                max-width: 500px;
                text-align: center;
                animation: slideUp 0.3s ease-out;
            }}
            @keyframes slideUp {{
                from {{
                    opacity: 0;
                    transform: translateY(20px);
                }}
                to {{
                    opacity: 1;
                    transform: translateY(0);
                }}
            }}
            .icon {{
                font-size: 64px;
                margin-bottom: 20px;
            }}
            h1 {{
                color: #dc2626;
                font-size: 28px;
                margin-bottom: 16px;
                font-weight: 600;
            }}
            .step {{
                color: #6b7280;
                font-size: 14px;
                margin-bottom: 20px;
                background: #f3f4f6;
                padding: 12px;
                border-radius: 8px;
                font-family: monospace;
            }}
            .error-msg {{
                color: #374151;
                font-size: 14px;
                margin-bottom: 30px;
                line-height: 1.6;
                background: #fef2f2;
                padding: 16px;
                border-radius: 8px;
                border-left: 4px solid #dc2626;
                text-align: left;
                max-height: 120px;
                overflow-y: auto;
            }}
            .buttons {{
                display: flex;
                gap: 12px;
                margin-top: 30px;
            }}
            button {{
                flex: 1;
                padding: 12px 24px;
                font-size: 16px;
                font-weight: 600;
                border: none;
                border-radius: 8px;
                cursor: pointer;
                transition: all 0.2s;
            }}
            .retry-btn {{
                background: #3b82f6;
                color: white;
            }}
            .retry-btn:hover {{
                background: #2563eb;
                transform: translateY(-2px);
                box-shadow: 0 10px 20px rgba(59, 130, 246, 0.3);
            }}
            .close-btn {{
                background: #e5e7eb;
                color: #374151;
            }}
            .close-btn:hover {{
                background: #d1d5db;
            }}
            .info {{
                font-size: 12px;
                color: #9ca3af;
                margin-top: 16px;
                font-style: italic;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="icon">⚠️</div>
            <h1>Erro na Automação</h1>
            <div class="step">Passo: {step_name}</div>
            <div class="error-msg">{error_msg}</div>
            <p style="color: #6b7280; margin-bottom: 24px;">O que você deseja fazer?</p>
            <div class="buttons">
                <button class="retry-btn" id="retry-btn">🔄 Tentar de Novo</button>
                <button class="close-btn" id="close-btn">✕ Fechar</button>
            </div>
            <div class="info">
                ℹ️ "Tentar de novo" volta 1 passo e tenta novamente no mesmo navegador.
            </div>
        </div>

        <script>
            window.dialogResult = null;
            const retryBtn = document.getElementById('retry-btn');
            const closeBtn = document.getElementById('close-btn');

            retryBtn.addEventListener('click', () => {{
                retryBtn.disabled = true;
                closeBtn.disabled = true;
                retryBtn.style.opacity = '0.6';
                retryBtn.textContent = '⏳ Reiniciando...';
                window.dialogResult = 'retry';
                console.log('DIALOG_RESULT: retry');
            }});

            closeBtn.addEventListener('click', () => {{
                retryBtn.disabled = true;
                closeBtn.disabled = true;
                closeBtn.style.opacity = '0.6';
                closeBtn.textContent = '⏳ Fechando...';
                window.dialogResult = 'close';
                console.log('DIALOG_RESULT: close');
            }});

            // Feedback visual — ambos os botões têm hover mais visível
            [retryBtn, closeBtn].forEach(btn => {{
                btn.addEventListener('mousedown', (e) => {{
                    e.target.style.transform = 'scale(0.95)';
                }});
                btn.addEventListener('mouseup', (e) => {{
                    e.target.style.transform = 'scale(1)';
                }});
            }});
        </script>
    </body>
    </html>
    """

    try:
        # Abre uma nova aba com o diálogo
        dialog_page = await page.context.new_page()
        await dialog_page.set_content(dialog_html)

        # Aguarda o resultado (até 60 segundos)
        start_time = asyncio.get_event_loop().time()
        # Janela para o operador responder. Era 60s FIXO: em lote, toda run
        # que falha parava 1 minuto esperando alguém que não está olhando.
        from config import ERROR_DIALOG_TIMEOUT_S  # noqa: PLC0415

        timeout = ERROR_DIALOG_TIMEOUT_S

        while True:
            try:
                result = await dialog_page.evaluate("() => window.dialogResult")
                if result:
                    logger.info(f"Dialog resultado: {result}")
                    await dialog_page.close()
                    return result == "retry"
            except Exception as e:
                logger.debug(f"Polling error: {e}")

            # Timeout
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > timeout:
                logger.warning(f"Dialog timeout após {elapsed:.1f}s — assumindo 'close'")
                try:
                    await dialog_page.close()
                except:
                    pass
                return False

            await asyncio.sleep(0.2)

    except Exception as e:
        logger.error(f"Erro ao mostrar dialog: {e}")
        return False


async def show_error_dialog_before_close(
    page: Page,
    error_msg: str,
    step_name: str = "desconhecido"
) -> bool:
    """Wrapper que mostra o diálogo em tela cheia.

    Retorna True se operador quer voltar/tentar de novo.
    Retorna False se quer fechar mesmo assim.
    """
    try:
        logger.info(f"Mostrando dialog de erro: {step_name}")
        return await show_error_recovery_dialog(page, error_msg, step_name)
    except Exception as e:
        logger.error(f"Erro ao exibir dialog: {e}")
        return False
