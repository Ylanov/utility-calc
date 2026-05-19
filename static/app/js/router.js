/**
 * Простейший hash-based роутер. Без зависимостей.
 *
 * Маршруты вида `#/home`, `#/readings`, `#/history`, `#/profile`.
 * Hash выбран сознательно — не требует server-side rewrite (StaticFiles
 * отдал бы 404 при F5 на /app/readings). С хешем всё работает на CDN.
 */

const routes = new Map();
let currentRoute = null;

export function registerRoute(name, handler) {
    routes.set(name, handler);
}

function parseHash() {
    const hash = window.location.hash || '#/home';
    // '#/home' → 'home'; '#/foo/bar' → 'foo' (вложенные не используем пока)
    return hash.replace(/^#\//, '').split('/')[0] || 'home';
}

async function dispatch() {
    const name = parseHash();
    const handler = routes.get(name) || routes.get('home');
    currentRoute = name;
    updateNavActive(name);
    const root = document.getElementById('app');
    if (!root || !handler) return;
    root.setAttribute('aria-busy', 'true');
    try {
        await handler(root);
    } catch (err) {
        console.error('[router] screen error:', err);
        root.innerHTML = `
            <div class="screen">
                <div class="card card--danger">
                    <div class="card__title">Что-то пошло не так</div>
                    <div style="color: var(--text-secondary);">
                        ${err.message || 'Неизвестная ошибка'}
                    </div>
                </div>
            </div>`;
    } finally {
        root.setAttribute('aria-busy', 'false');
    }
}

function updateNavActive(name) {
    document.querySelectorAll('.bottom-nav__item').forEach((el) => {
        if (el.dataset.route === name) {
            el.classList.add('is-active');
        } else {
            el.classList.remove('is-active');
        }
    });
}

export function startRouter() {
    window.addEventListener('hashchange', dispatch);
    // Первая отрисовка
    dispatch();
}

export function getCurrentRoute() {
    return currentRoute;
}
