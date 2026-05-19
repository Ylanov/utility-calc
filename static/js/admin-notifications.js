// static/js/admin-notifications.js
//
// Колокольчик уведомлений в шапке админки.
// Polling /api/admin/notifications каждые 30 секунд, badge с числом
// событий. При клике — dropdown со сгруппированными категориями.
// Click по категории — переход по link (Tools / Audit / Dashboard).

import { api } from './core/api.js';
import { Auth } from './core/auth.js';

const POLL_INTERVAL_MS = 30_000;

// CSS вставляем один раз. Стили локальные, без зависимости от main.css —
// чтобы dropdown работал и на старых страницах без подключения admin-styles.
const CSS = `
.notif-badge {
    position: absolute;
    top: 4px; right: 4px;
    min-width: 18px;
    height: 18px;
    padding: 0 5px;
    border-radius: 9px;
    background: #ef4444;
    color: white;
    font-size: 10px;
    font-weight: 700;
    display: flex;
    align-items: center;
    justify-content: center;
    pointer-events: none;
    animation: notif-pulse 2s ease-in-out infinite;
}
@keyframes notif-pulse {
    0%, 100% { box-shadow: 0 0 0 0 rgba(239,68,68,0.5); }
    50% { box-shadow: 0 0 0 6px rgba(239,68,68,0); }
}
.notif-dropdown {
    position: absolute;
    top: calc(100% + 8px);
    right: 0;
    min-width: 340px;
    max-width: 90vw;
    max-height: 70vh;
    overflow-y: auto;
    background: var(--bg-card, #fff);
    border: 1px solid var(--border-color, #e5e7eb);
    border-radius: 12px;
    box-shadow: 0 12px 32px rgba(0,0,0,0.12);
    z-index: 1000;
    padding: 8px 0;
}
.notif-dropdown__header {
    padding: 10px 16px;
    font-size: 12px;
    font-weight: 700;
    color: var(--text-secondary, #6b7280);
    text-transform: uppercase;
    letter-spacing: 0.3px;
}
.notif-category {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 10px 16px;
    border-bottom: 1px solid var(--border-color, #e5e7eb);
    color: var(--text-main, #111827);
    text-decoration: none;
    cursor: pointer;
}
.notif-category:last-child { border-bottom: none; }
.notif-category:hover { background: var(--bg-page, #f9fafb); }
.notif-category__count {
    min-width: 28px;
    height: 28px;
    border-radius: 14px;
    background: var(--primary-color, #2563eb);
    color: white;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 13px;
    font-weight: 700;
    padding: 0 8px;
    flex-shrink: 0;
}
.notif-category__count--zero {
    background: var(--bg-page, #f3f4f6);
    color: var(--text-muted, #9ca3af);
}
.notif-category__label {
    flex: 1;
    font-size: 14px;
    font-weight: 500;
}
.notif-category__chevron { color: var(--text-muted, #9ca3af); }
.notif-empty {
    padding: 24px 16px;
    text-align: center;
    color: var(--text-secondary, #6b7280);
    font-size: 13px;
}
`;

function injectCss() {
    if (document.getElementById('admin-notifs-css')) return;
    const st = document.createElement('style');
    st.id = 'admin-notifs-css';
    st.textContent = CSS;
    document.head.appendChild(st);
}

let _pollTimer = null;
let _outsideClickHandler = null;

async function fetchAndRender() {
    const btn = document.getElementById('notifBtn');
    const badge = document.getElementById('notifBadge');
    if (!btn || !badge) return;

    try {
        const data = await api.get('/admin/notifications');
        const total = data.total || 0;
        if (total > 0) {
            badge.textContent = total > 99 ? '99+' : String(total);
            badge.hidden = false;
        } else {
            badge.hidden = true;
        }
        // Сохраняем для следующего открытия dropdown.
        btn.dataset.cache = JSON.stringify(data);
    } catch (e) {
        console.warn('[notifs] fetch failed:', e);
    }
}

function renderDropdown(data) {
    const cats = data?.categories || {};
    const items = Object.entries(cats);
    const totalCount = data?.total || 0;

    if (totalCount === 0) {
        return `
            <div class="notif-empty">
                ✓ Сейчас нет событий, требующих внимания
            </div>
        `;
    }

    return `
        <div class="notif-dropdown__header">События</div>
        ${items.map(([key, cat]) => `
            <a class="notif-category" href="${cat.link || '#'}" data-cat="${key}">
                <span class="notif-category__count ${cat.count === 0 ? 'notif-category__count--zero' : ''}">
                    ${cat.count}
                </span>
                <span class="notif-category__label">${cat.label}</span>
                <span class="notif-category__chevron">›</span>
            </a>
        `).join('')}
    `;
}

function toggleDropdown() {
    const dd = document.getElementById('notifDropdown');
    const btn = document.getElementById('notifBtn');
    if (!dd || !btn) return;
    const isOpen = !dd.hidden;
    if (isOpen) {
        dd.hidden = true;
        if (_outsideClickHandler) {
            document.removeEventListener('click', _outsideClickHandler);
            _outsideClickHandler = null;
        }
        return;
    }
    let data = null;
    try { data = JSON.parse(btn.dataset.cache || '{}'); } catch {}
    dd.innerHTML = renderDropdown(data);
    dd.hidden = false;
    // Закрываем при клике вне дропдауна
    _outsideClickHandler = (e) => {
        if (!dd.contains(e.target) && !btn.contains(e.target)) {
            dd.hidden = true;
            document.removeEventListener('click', _outsideClickHandler);
            _outsideClickHandler = null;
        }
    };
    setTimeout(() => document.addEventListener('click', _outsideClickHandler), 0);
}

export function initAdminNotifications() {
    // Только для admin/accountant/financier — у простого user'а endpoint вернёт 403.
    const role = Auth.getRole && Auth.getRole();
    if (!role || role === 'user') return;

    injectCss();
    const btn = document.getElementById('notifBtn');
    if (!btn) return;
    btn.addEventListener('click', (e) => {
        e.stopPropagation();
        toggleDropdown();
    });
    // Первая загрузка — сразу, потом polling.
    fetchAndRender();
    if (_pollTimer) clearInterval(_pollTimer);
    _pollTimer = setInterval(fetchAndRender, POLL_INTERVAL_MS);
}

// Auto-init если в DOM есть колокольчик (admin.html). На других страницах
// модуль может быть подключён, но колокольчика нет — тогда init() выйдет тихо.
if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initAdminNotifications);
    } else {
        initAdminNotifications();
    }
}
