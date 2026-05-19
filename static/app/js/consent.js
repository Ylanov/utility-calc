/**
 * Модальный экран согласия на обработку ПД (152-ФЗ).
 *
 * Проверяет /api/me/consent-status сразу после ensureAuthenticated().
 * Если жилец не подписал или подписал устаревшую версию — показывает
 * full-screen модалку с текстом «вы согласны на обработку ПД». Без
 * согласия в личный кабинет не пускает.
 *
 * При нажатии «Согласен» — POST /api/me/consent-pdn, после чего модалка
 * исчезает и продолжается init() main.js.
 */
import { api } from './api.js';

/**
 * @returns {Promise<boolean>} true если согласие получено (можно идти дальше),
 * false если жилец отказался (страница останется на модалке).
 */
export async function ensureConsent() {
    let status;
    try {
        status = await api.get('/me/consent-status');
    } catch (e) {
        console.warn('[consent] status check failed:', e);
        // На случай 500/Network — пускаем дальше (не блокируем). Backend всё
        // равно требует consent для модифицирующих операций (см. middleware
        // в Фазе 3 если будет).
        return true;
    }
    if (status.has_consent) return true;

    return new Promise((resolve) => {
        renderModal(status, () => resolve(true));
    });
}

function renderModal(status, onAccepted) {
    const overlay = document.createElement('div');
    overlay.id = 'consent-overlay';
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');
    overlay.style.cssText = `
        position: fixed;
        inset: 0;
        background: var(--bg-page);
        z-index: 2000;
        display: flex;
        flex-direction: column;
        padding: env(safe-area-inset-top) var(--sp-4) env(safe-area-inset-bottom);
        overflow-y: auto;
    `;
    const isUpdate = !!status.accepted_version && status.accepted_version !== status.current_version;
    overlay.innerHTML = `
        <div style="max-width: var(--max-content-width); margin: 0 auto; padding: var(--sp-5) 0 var(--sp-6); width: 100%;">
            <div style="text-align: center; margin-bottom: var(--sp-5);">
                <div style="width: 64px; height: 64px; margin: 0 auto var(--sp-4); border-radius: 50%; background: var(--primary-bg); color: var(--primary); display: flex; align-items: center; justify-content: center; font-size: 32px;">
                    🔒
                </div>
                <h2 style="font-size: 22px; font-weight: 700; line-height: 1.3;">
                    ${isUpdate ? 'Политика обновлена' : 'Согласие на обработку данных'}
                </h2>
                <p style="font-size: 14px; color: var(--text-secondary); margin-top: var(--sp-2); line-height: 1.5;">
                    ${isUpdate
                        ? 'Мы обновили политику обработки персональных данных. Подтвердите согласие, чтобы продолжить пользоваться личным кабинетом.'
                        : 'Чтобы пользоваться личным кабинетом, подтвердите согласие на обработку ваших персональных данных по 152-ФЗ.'}
                </p>
            </div>

            <div class="card" style="margin-bottom: var(--sp-4);">
                <div style="font-size: 14px; line-height: 1.6; color: var(--text-primary);">
                    <p style="margin-bottom: var(--sp-2);">
                        Мы обрабатываем ваши персональные данные (ФИО, паспорт, адрес, контакты,
                        показания счётчиков, платежи) для оказания услуг ЖКХ и расчёта квитанций.
                    </p>
                    <p style="margin-bottom: var(--sp-2);">
                        Данные хранятся на серверах в РФ, передаются третьим лицам только по
                        закону (суд, прокуратура, ФССП). Сторонние трекинговые системы не используются.
                    </p>
                    <p style="margin-bottom: var(--sp-2);">
                        <strong>Срок действия согласия:</strong> до достижения целей обработки
                        или до отзыва. Отозвать можно в любое время в разделе «Мои данные».
                    </p>
                    <p>
                        Полный текст —
                        <a href="/privacy.html" target="_blank" rel="noopener" style="color: var(--primary); font-weight: 600;">
                            Политика обработки ПД (версия ${status.current_version})
                        </a>.
                    </p>
                </div>
            </div>

            <label style="display: flex; gap: var(--sp-3); align-items: flex-start; padding: var(--sp-3); cursor: pointer; user-select: none;">
                <input type="checkbox" id="consentCheckbox" style="width: 22px; height: 22px; flex-shrink: 0; accent-color: var(--primary); margin-top: 2px;">
                <span style="font-size: 14px; line-height: 1.5;">
                    Я ознакомлен(а) с Политикой обработки персональных данных
                    и даю согласие на обработку моих ПД в указанных целях.
                </span>
            </label>

            <button id="consentAcceptBtn" type="button" disabled style="
                width: 100%;
                min-height: 52px;
                padding: 0 var(--sp-5);
                margin-top: var(--sp-4);
                border-radius: var(--radius-md);
                background: var(--primary);
                color: white;
                font-size: 16px;
                font-weight: 600;
                border: none;
                cursor: pointer;
                opacity: 0.5;
                transition: opacity 150ms ease;
            ">
                Продолжить
            </button>

            <p style="text-align: center; font-size: 12px; color: var(--text-muted); margin-top: var(--sp-4); line-height: 1.5;">
                Без согласия дальнейшее использование сервиса невозможно.
                Если вы не согласны — закройте страницу или
                <a href="mailto:privacy@asy-tk.ru" style="color: var(--text-secondary);">свяжитесь с оператором</a>.
            </p>
        </div>
    `;
    document.body.appendChild(overlay);

    const checkbox = overlay.querySelector('#consentCheckbox');
    const button = overlay.querySelector('#consentAcceptBtn');
    checkbox.addEventListener('change', () => {
        const on = checkbox.checked;
        button.disabled = !on;
        button.style.opacity = on ? '1' : '0.5';
    });

    button.addEventListener('click', async () => {
        if (!checkbox.checked || button.disabled) return;
        button.disabled = true;
        button.textContent = 'Сохраняем…';
        try {
            await api.post('/me/consent-pdn', { version: status.current_version });
            overlay.style.transition = 'opacity 200ms ease';
            overlay.style.opacity = '0';
            setTimeout(() => {
                overlay.remove();
                onAccepted();
            }, 220);
        } catch (e) {
            button.textContent = 'Ошибка — попробуйте ещё раз';
            button.disabled = false;
            console.error('[consent] accept failed:', e);
        }
    });
}
