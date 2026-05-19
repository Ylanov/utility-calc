/**
 * PWA жильца — entry point.
 *
 * Шаги при загрузке:
 *   1. Регистрируем Service Worker (offline + push потенциально)
 *   2. Проверяем авторизацию → грузим профиль жильца
 *   3. Регистрируем экраны в router
 *   4. Стартуем router (hashchange + первый dispatch)
 *   5. Прячем splash + показываем bottom-nav
 *
 * Все экраны импортируются динамически (`import()`) — браузер тянет JS
 * только когда жилец на них переходит, не блокирует первую отрисовку.
 */
import { ensureAuthenticated } from './auth.js';
import { ensureConsent } from './consent.js';
import { registerRoute, startRouter } from './router.js';

// ─── Service Worker ────────────────────────────────────────────────────
// Регистрируем только в продакшене (или когда HTTPS).
// На localhost тоже работает — SW требует secure context.
if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => {
        navigator.serviceWorker
            .register('/app/sw.js', { scope: '/app/' })
            .catch((err) => console.warn('[SW] register failed:', err));
    });
}

// ─── Init ──────────────────────────────────────────────────────────────
async function init() {
    // Авторизация — если нет токена, ensureAuthenticated сделает редирект
    // на /login.html?next=/app/ и эта функция не вернёт управление.
    try {
        await ensureAuthenticated();
        // После авторизации — проверяем согласие на обработку ПД (152-ФЗ).
        // Если жилец не подписал текущую версию политики — покажется
        // блокирующая модалка; промис разрешится только после подписи.
        await ensureConsent();
    } catch (e) {
        console.error('[auth] failed:', e);
        // Жилец увидит сообщение об ошибке на splash; пусть переходит на логин.
        document.getElementById('splash').innerHTML = `
            <div style="text-align:center; padding: 24px;">
                <div class="splash__logo">⚠️</div>
                <div style="margin-top:16px; color: var(--text-secondary);">
                    Не удалось войти. Попробуйте перезагрузить страницу.
                </div>
                <a href="/login.html?next=/app/" class="action-tile" style="margin-top:24px; display:inline-block; padding: 12px 24px; background: var(--primary); color:white; border-radius: 12px; text-decoration:none;">
                    На страницу входа
                </a>
            </div>`;
        return;
    }

    // ─── Регистрация экранов ──────────────────────────────────────────
    // Lazy: каждый экран отдельный chunk, грузится при первом переходе.
    registerRoute('home', async (root) => {
        const { renderHome } = await import('./screens/home.js');
        await renderHome(root);
    });
    // Фаза 2 — нативные PWA-экраны (вместо stub'ов).
    registerRoute('readings', async (root) => {
        const { renderReadings } = await import('./screens/readings.js');
        await renderReadings(root);
    });
    registerRoute('history', async (root) => {
        const { renderHistory } = await import('./screens/history.js');
        await renderHistory(root);
    });
    registerRoute('profile', async (root) => {
        const { renderProfile } = await import('./screens/profile.js');
        await renderProfile(root);
    });
    registerRoute('my-data', async (root) => {
        // Экран «Мои данные» — реализация прав по 152-ФЗ (ст. 14, 21).
        const { renderMyData } = await import('./screens/my-data.js');
        await renderMyData(root);
    });
    registerRoute('support', async (root) => {
        // Обращения — жилец пишет вопрос, админ отвечает.
        const { renderSupport } = await import('./screens/support.js');
        await renderSupport(root);
    });

    // ─── Старт ────────────────────────────────────────────────────────
    startRouter();

    // Прячем splash + показываем bottom-nav
    const splash = document.getElementById('splash');
    if (splash) {
        splash.setAttribute('aria-hidden', 'true');
        setTimeout(() => splash.remove(), 400);
    }
    const nav = document.getElementById('bottom-nav');
    if (nav) nav.classList.remove('hidden');
}

function escapeHtml(str) {
    if (str == null) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

// Старт сразу после парсинга HTML.
init();
