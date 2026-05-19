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
    registerRoute('readings', async (root) => {
        // Фаза 2: для начала редиректим на старый портал.
        root.innerHTML = renderRedirectStub(
            'Подача показаний',
            'Этот экран ещё переезжает в новую версию. Пока используем привычную форму.',
            '/?next=readings',
        );
    });
    registerRoute('history', async (root) => {
        root.innerHTML = renderRedirectStub(
            'История квитанций',
            'Этот экран ещё переезжает. Пока используем привычный портал.',
            '/?next=history',
        );
    });
    registerRoute('profile', async (root) => {
        root.innerHTML = renderRedirectStub(
            'Профиль',
            'Этот экран ещё переезжает. Пока пользуемся привычным порталом.',
            '/?next=profile',
        );
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

function renderRedirectStub(title, message, fallbackHref) {
    return `
        <div class="screen">
            <header class="header">
                <div class="header__greeting">
                    <div class="header__hello">Скоро здесь будет</div>
                    <div class="header__name">${escapeHtml(title)}</div>
                </div>
            </header>
            <div class="card card--warning">
                <div style="font-size: 14px; line-height: 1.5; color: var(--text-secondary);">
                    ${escapeHtml(message)}
                </div>
                <a href="${fallbackHref}" style="
                    display: inline-block;
                    margin-top: var(--sp-4);
                    padding: 12px 20px;
                    background: var(--primary);
                    color: white;
                    border-radius: var(--radius-md);
                    text-decoration: none;
                    font-weight: 600;
                ">Открыть старый портал</a>
            </div>
        </div>
    `;
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
