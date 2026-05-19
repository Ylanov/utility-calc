/**
 * Главный экран — дашборд жильца.
 *
 * Что видит жилец сверху вниз:
 *   1. Приветствие + аватарка (инициалы)
 *   2. Большая «балансовая» карточка — чистый баланс (overpayment − debt)
 *      + к оплате за текущий период + статус (период открыт / показания утв.)
 *   3. Сетка quick actions 2×2 — Подать показания / Скачать квитанцию /
 *      Заказать справку / История
 *   4. (опционально) Anomaly alert если есть нерешённые проблемы
 *
 * Источники данных:
 *   - GET /api/me — имя, room, role
 *   - GET /api/client/finance — debt/overpayment/current_period_total
 *   - GET /api/readings/state — статус периода + last readings
 */
import { api, formatMoney } from '../api.js';
import { getCachedMe, initialsFor } from '../auth.js';
import { toast } from '../toast.js';

export async function renderHome(root) {
    const me = getCachedMe();

    // Сначала рисуем skeleton чтобы не было пустого экрана пока грузится.
    root.innerHTML = renderSkeleton(me);

    // Параллельно подгружаем баланс и состояние периода.
    let finance = null;
    let state = null;
    try {
        [finance, state] = await Promise.all([
            api.get('/client/finance').catch((e) => {
                console.warn('finance error', e);
                return null;
            }),
            api.get('/readings/state').catch((e) => {
                console.warn('state error', e);
                return null;
            }),
        ]);
    } catch (e) {
        toast.error('Не удалось загрузить данные: ' + e.message);
    }

    root.innerHTML = renderContent(me, finance, state);
}

/** Скелетон-загрузчик (полупустые карточки с шиммером). */
function renderSkeleton(me) {
    const initials = initialsFor(me?.username || '?');
    const greeting = greetingFor();
    return `
        <div class="screen">
            <header class="header">
                <div class="header__greeting">
                    <div class="header__hello">${greeting}</div>
                    <div class="header__name">${escapeHtml(me?.username || 'Жилец')}</div>
                </div>
                <div class="header__avatar">${initials}</div>
            </header>
            <div class="skeleton skeleton--card"></div>
            <div class="skeleton skeleton--card" style="height:230px;"></div>
        </div>
    `;
}

/** Полный рендер. */
function renderContent(me, finance, state) {
    const initials = initialsFor(me?.username || '?');
    const greeting = greetingFor();

    return `
        <div class="screen">
            <header class="header">
                <div class="header__greeting">
                    <div class="header__hello">${greeting}</div>
                    <div class="header__name">${escapeHtml(me?.username || 'Жилец')}</div>
                </div>
                <div class="header__avatar">${initials}</div>
            </header>

            ${renderBalanceCard(finance, state)}

            <div class="actions-grid">
                ${renderActionTile({
                    href: '#/readings',
                    title: state?.is_period_open ? 'Подать показания' : 'Показания',
                    sub: actionSubReadings(state),
                    variant: state?.is_period_open && !state?.is_already_approved ? 'success' : '',
                    icon: 'gauge',
                })}
                ${renderActionTile({
                    href: '#/history',
                    title: 'История',
                    sub: 'Квитанции и PDF',
                    icon: 'list',
                })}
                ${renderActionTile({
                    href: '#/profile?action=cert',
                    title: 'Справка',
                    sub: 'Заказать ФЛС',
                    icon: 'doc',
                })}
                ${renderActionTile({
                    href: '#/profile',
                    title: 'Профиль',
                    sub: 'Пароль, 2FA',
                    icon: 'user',
                })}
            </div>

            ${renderRoomCard(me)}
        </div>
    `;
}

/** Главная карточка — баланс + к оплате + статус. */
function renderBalanceCard(finance, state) {
    if (!finance) {
        return `
            <div class="card card--primary">
                <div class="card__label">Баланс</div>
                <div class="balance">
                    <div class="balance__amount">— <span class="balance__currency">₽</span></div>
                    <div class="balance__sub">Загружаем данные…</div>
                </div>
            </div>
        `;
    }
    // Чистый баланс = overpayment − debt. Положительный = вам должны, отрицательный = вы должны.
    const totalDebt = Number(finance.total_debt || 0);
    const totalOverpay = Number(finance.total_overpayment || 0);
    const net = totalOverpay - totalDebt;
    const currentTotal = Number(finance.current_period_total || 0);

    // Подсказки для жильца
    let netLabel = 'Баланс';
    let netClass = '';
    if (net > 0) {
        netLabel = 'Переплата';
        netClass = 'is-positive';
    } else if (net < 0) {
        netLabel = 'Долг';
        netClass = 'is-negative';
    }

    const statusBadge = renderStatusBadge(state);
    const periodName = finance.current_period_name || state?.period_name || 'Текущий период';

    return `
        <div class="card card--primary">
            <div class="card__label">${netLabel}</div>
            <div class="balance">
                <div class="balance__amount">
                    ${net < 0 ? '−' : ''}${formatMoney(Math.abs(net))}
                    <span class="balance__currency">₽</span>
                </div>
                <div class="balance__sub">
                    <span>${escapeHtml(periodName)}</span>
                    <span class="dot"></span>
                    <span>К оплате: ${formatMoney(currentTotal)} ₽</span>
                </div>
            </div>
            ${statusBadge ? `<div style="margin-top:var(--sp-4);">${statusBadge}</div>` : ''}
        </div>
    `;
}

function renderStatusBadge(state) {
    if (!state) return '';
    if (state.is_already_approved) {
        return '<span class="status status--approved">✓ Показания утверждены</span>';
    }
    if (state.is_draft) {
        return '<span class="status status--draft">✏️ Черновик сохранён</span>';
    }
    if (state.is_period_open) {
        return '<span class="status status--open">🟢 Период открыт</span>';
    }
    return '<span class="status status--closed">🔒 Приём закрыт</span>';
}

function actionSubReadings(state) {
    if (!state) return 'Загружаем…';
    if (state.is_already_approved) return 'Уже утверждены ✓';
    if (state.is_draft) return 'Черновик ✏️';
    if (state.is_period_open) return 'Приём открыт';
    return 'Приём закрыт';
}

const ICONS = {
    gauge: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>',
    list: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9h10M7 13h7M7 17h4"/></svg>',
    doc: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><path d="M8 13h8M8 17h5"/></svg>',
    user: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="8" r="4"/><path d="M4 20c0-4 4-6 8-6s8 2 8 6"/></svg>',
};

function renderActionTile({ href, title, sub, variant = '', icon = 'gauge' }) {
    const cls = variant ? `action-tile action-tile--${variant}` : 'action-tile';
    return `
        <a href="${href}" class="${cls}">
            <div class="action-tile__icon">${ICONS[icon] || ICONS.gauge}</div>
            <div class="action-tile__title">${escapeHtml(title)}</div>
            <div class="action-tile__sub">${escapeHtml(sub)}</div>
        </a>
    `;
}

function renderRoomCard(me) {
    const room = me?.room;
    if (!room) return '';
    const area = room.apartment_area ? Number(room.apartment_area).toFixed(1) : '—';
    return `
        <h3 class="section-title">Помещение</h3>
        <div class="list">
            <div class="list__item">
                <div class="list__item-content">
                    <div class="list__item-title">${escapeHtml(room.dormitory_name)}, ком. ${escapeHtml(room.room_number)}</div>
                    <div class="list__item-sub">Площадь: ${area} м² · Жильцов: ${me.residents_count || 1}</div>
                </div>
            </div>
        </div>
    `;
}

function greetingFor() {
    const h = new Date().getHours();
    if (h < 6) return 'Доброй ночи';
    if (h < 12) return 'Доброе утро';
    if (h < 18) return 'Добрый день';
    return 'Добрый вечер';
}

function escapeHtml(str) {
    if (str == null) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}
