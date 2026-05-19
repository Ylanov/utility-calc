/**
 * Экран профиля жильца.
 *
 * Содержит:
 *   - Карточку с данными помещения (адрес, площадь, состав семьи)
 *   - Список настроек (смена пароля, 2FA, договоры найма, справки)
 *   - Юр. блок: «Мои данные» (право доступа по 152-ФЗ), политика
 *   - Кнопка «Выйти»
 *
 * Большинство экшенов в Фазе 2 — это редиректы на старый портал
 * с anchor'ом (например `/?tab=profile&action=password`). PWA-нативные
 * формы появятся в Фазе 3.
 */
import { setToken } from '../api.js';
import { getCachedMe, initialsFor } from '../auth.js';

export async function renderProfile(root) {
    const me = getCachedMe();
    root.innerHTML = render(me);
    bindHandlers(root);
}

function render(me) {
    const initials = initialsFor(me?.username || '?');
    const room = me?.room;
    const roomTitle = room
        ? `${escapeHtml(room.dormitory_name)}, ком. ${escapeHtml(room.room_number)}`
        : 'Не привязан';
    const area = room?.apartment_area ? Number(room.apartment_area).toFixed(1) + ' м²' : '—';

    return `
        <div class="screen">
            <header class="header">
                <div class="header__greeting">
                    <div class="header__hello">Профиль</div>
                    <div class="header__name">${escapeHtml(me?.username || 'Жилец')}</div>
                </div>
                <div class="header__avatar">${initials}</div>
            </header>

            <!-- Карточка помещения -->
            <div class="card">
                <div class="card__label">Помещение</div>
                <div style="font-size:17px; font-weight:600; margin-top:4px;">${roomTitle}</div>
                <div style="font-size:13px; color:var(--text-secondary); margin-top:6px;">
                    Площадь: ${area} · Жильцов: ${me?.residents_count || 1}
                </div>
            </div>

            <!-- Настройки безопасности -->
            <div class="section-title">Безопасность</div>
            <div class="setting-group">
                <button class="setting-item" data-action="password">
                    <div class="setting-item__icon">
                        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <rect x="3" y="11" width="18" height="11" rx="2"/>
                            <path d="M7 11V7a5 5 0 0 1 10 0v4"/>
                        </svg>
                    </div>
                    <div class="setting-item__content">
                        <div class="setting-item__title">Сменить пароль</div>
                        <div class="setting-item__sub">Обновите пароль входа</div>
                    </div>
                    <div class="setting-item__chevron">›</div>
                </button>
                <button class="setting-item" data-action="2fa">
                    <div class="setting-item__icon">
                        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <path d="M12 3l8 4v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V7l8-4z"/>
                        </svg>
                    </div>
                    <div class="setting-item__content">
                        <div class="setting-item__title">Двухфакторная аутентификация</div>
                        <div class="setting-item__sub">${me?.is_2fa_enabled ? 'Включена ✓' : 'Не настроена'}</div>
                    </div>
                    <div class="setting-item__chevron">›</div>
                </button>
            </div>

            <!-- Поддержка -->
            <div class="section-title">Поддержка</div>
            <div class="setting-group">
                <a class="setting-item" href="#/support">
                    <div class="setting-item__icon" style="background: var(--primary-bg); color: var(--primary);">
                        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
                        </svg>
                    </div>
                    <div class="setting-item__content">
                        <div class="setting-item__title">Обращения</div>
                        <div class="setting-item__sub">Задать вопрос администратору</div>
                    </div>
                    <div class="setting-item__chevron">›</div>
                </a>
            </div>

            <!-- Документы и справки -->
            <div class="section-title">Документы</div>
            <div class="setting-group">
                <button class="setting-item" data-action="certificates">
                    <div class="setting-item__icon">
                        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                            <polyline points="14 2 14 8 20 8"/>
                        </svg>
                    </div>
                    <div class="setting-item__content">
                        <div class="setting-item__title">Заказать справку</div>
                        <div class="setting-item__sub">Выписка из финансово-лицевого счёта</div>
                    </div>
                    <div class="setting-item__chevron">›</div>
                </button>
                <button class="setting-item" data-action="contracts">
                    <div class="setting-item__icon">
                        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <path d="M9 11l3 3L22 4"/>
                            <path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/>
                        </svg>
                    </div>
                    <div class="setting-item__content">
                        <div class="setting-item__title">Договоры найма</div>
                        <div class="setting-item__sub">Просмотр и загрузка</div>
                    </div>
                    <div class="setting-item__chevron">›</div>
                </button>
            </div>

            <!-- Юридический блок: данные и политика (152-ФЗ) -->
            <div class="section-title">Персональные данные</div>
            <div class="setting-group">
                <a class="setting-item" href="#/my-data">
                    <div class="setting-item__icon">
                        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/>
                            <circle cx="9" cy="7" r="4"/>
                            <path d="M22 11h-6M22 15h-6M22 19h-3"/>
                        </svg>
                    </div>
                    <div class="setting-item__content">
                        <div class="setting-item__title">Мои данные</div>
                        <div class="setting-item__sub">Просмотр, экспорт, запрос удаления</div>
                    </div>
                    <div class="setting-item__chevron">›</div>
                </a>
                <a class="setting-item" href="/privacy.html" target="_blank" rel="noopener">
                    <div class="setting-item__icon">
                        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <path d="M12 2L4 6v6c0 5 3.5 8 8 10 4.5-2 8-5 8-10V6l-8-4z"/>
                        </svg>
                    </div>
                    <div class="setting-item__content">
                        <div class="setting-item__title">Политика обработки данных</div>
                        <div class="setting-item__sub">152-ФЗ — полный текст</div>
                    </div>
                    <div class="setting-item__chevron">›</div>
                </a>
            </div>

            <!-- Выход -->
            <button id="logoutBtn" class="btn btn--secondary" style="margin-top: var(--sp-5);">
                Выйти из аккаунта
            </button>

            <div style="text-align:center; color:var(--text-muted); font-size:11px; margin-top:var(--sp-5);">
                ID жильца: ${me?.id || '—'}
            </div>
        </div>
    `;
}

function bindHandlers(root) {
    // Все settings-item с data-action — редиректят на старый портал с anchor'ом.
    // Когда в Фазе 3 будут нативные экраны — заменим на router.navigate.
    root.querySelectorAll('[data-action]').forEach((el) => {
        el.addEventListener('click', () => {
            const action = el.dataset.action;
            const map = {
                password: '/?tab=profile&action=password',
                '2fa':    '/?tab=profile&action=2fa',
                certificates: '/?tab=certificates',
                contracts: '/?tab=profile&action=contracts',
            };
            const url = map[action] || '/';
            window.location.href = url;
        });
    });

    const logout = root.querySelector('#logoutBtn');
    if (logout) {
        logout.addEventListener('click', () => {
            const ok = confirm('Выйти из аккаунта?');
            if (!ok) return;
            setToken(null);
            // Чистим и role из sessionStorage (старый портал его использует).
            try { sessionStorage.removeItem('role'); } catch {}
            try { sessionStorage.removeItem('username'); } catch {}
            window.location.href = '/login.html';
        });
    }
}

function escapeHtml(str) {
    if (str == null) return '';
    return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
