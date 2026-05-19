/**
 * Экран «Мои данные» — реализация прав субъекта ПД (152-ФЗ ст. 14, 21).
 *
 * Жилец видит:
 *   1. Какие категории данных о нём хранятся (паспорт, адрес, контакты,
 *      финансы, показания, семья, договоры) — без раскрытия конкретных
 *      значений на этом экране (они и так доступны в Профиле).
 *   2. Версия принятого согласия + дата + IP.
 *   3. Кнопка «Скачать в JSON» — выгрузка через /api/me/data-export.
 *   4. Кнопка «Запросить удаление» — отправляет заявку админу.
 */
import { api } from '../api.js';
import { toast } from '../toast.js';
import { getCachedMe } from '../auth.js';

export async function renderMyData(root) {
    const me = getCachedMe();
    root.innerHTML = renderSkeleton();
    let consent = null;
    try {
        consent = await api.get('/me/consent-status');
    } catch {}
    root.innerHTML = render(me, consent);
    bind(root);
}

function renderSkeleton() {
    return `
        <div class="screen">
            <h2 style="font-size:22px; font-weight:700; margin-bottom: var(--sp-4);">Мои данные</h2>
            <div class="skeleton skeleton--card"></div>
            <div class="skeleton skeleton--card"></div>
        </div>
    `;
}

function render(me, consent) {
    const acceptedAt = consent?.accepted_at
        ? new Date(consent.accepted_at).toLocaleString('ru-RU')
        : '—';

    return `
        <div class="screen">
            <h2 style="font-size:22px; font-weight:700; margin-bottom: var(--sp-2);">Мои данные</h2>
            <p style="color:var(--text-secondary); font-size:13px; line-height:1.5; margin-bottom: var(--sp-5);">
                В соответствии с 152-ФЗ вы можете посмотреть какие ваши персональные данные
                хранит оператор, выгрузить их в JSON или подать заявку на удаление.
            </p>

            <!-- Согласие -->
            <div class="card ${consent?.has_consent ? 'card--success' : 'card--warning'}">
                <div class="card__label">Согласие на обработку ПД</div>
                <div style="font-size:14px; margin-top:6px;">
                    ${consent?.has_consent
                        ? `<strong>✓ Действует.</strong> Версия ${escapeHtml(consent.accepted_version || '—')}, принято ${escapeHtml(acceptedAt)}`
                        : '<strong>⚠ Не оформлено.</strong> Подпишите согласие при следующем входе.'}
                </div>
            </div>

            <!-- Категории данных -->
            <div class="section-title">Какие данные мы храним</div>
            <div class="setting-group">
                ${dataCategory('Учётные данные', 'Логин (лицевой счёт), пароль (в виде хэша), роль, дата последнего входа')}
                ${dataCategory('Личные данные', 'ФИО, дата рождения, паспорт (серия/номер/кем выдан), адрес регистрации')}
                ${dataCategory('Жилищные', 'Общежитие, комната, площадь, число проживающих, тип найма')}
                ${dataCategory('Финансы', 'Расчёты, квитанции, долги, переплаты, история начислений')}
                ${dataCategory('Показания', 'Подачи показаний счётчиков (ГВС/ХВС/электричество) по периодам')}
                ${dataCategory('Семья', 'Состав семьи если указан (для справок)')}
                ${dataCategory('Договоры', 'Договоры найма жилого помещения, PDF-сканы')}
                ${dataCategory('Технические', 'IP-адрес и время подачи согласия на обработку ПД, аудит-лог')}
            </div>

            <!-- Действия -->
            <div class="section-title">Действия</div>
            <div class="setting-group">
                <button id="btnExport" class="setting-item">
                    <div class="setting-item__icon" style="background: var(--primary-bg); color: var(--primary);">
                        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/>
                        </svg>
                    </div>
                    <div class="setting-item__content">
                        <div class="setting-item__title">Скачать все мои данные (JSON)</div>
                        <div class="setting-item__sub">Право доступа — ст. 14 152-ФЗ</div>
                    </div>
                    <div class="setting-item__chevron">›</div>
                </button>
                <button id="btnDeletionRequest" class="setting-item">
                    <div class="setting-item__icon" style="background: var(--danger-bg); color: var(--danger);">
                        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M10 11v6M14 11v6M5 6l1 14a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2l1-14"/>
                        </svg>
                    </div>
                    <div class="setting-item__content">
                        <div class="setting-item__title">Запросить удаление</div>
                        <div class="setting-item__sub">Заявка попадёт администратору</div>
                    </div>
                    <div class="setting-item__chevron">›</div>
                </button>
            </div>

            <p style="font-size:12px; color:var(--text-muted); line-height:1.5; margin-top: var(--sp-5);">
                Удаление не происходит автоматически: квитанции и расчёты по жилищному
                кодексу хранятся 5 лет. После заявки администратор анонимизирует ваши
                идентифицирующие данные (ФИО, паспорт), сохранив обезличенный финансовый след.
                Срок ответа — до 30 дней с даты подачи.
            </p>
        </div>
    `;
}

function dataCategory(title, sub) {
    return `
        <div class="setting-item" style="cursor: default;">
            <div class="setting-item__icon">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
                    <polyline points="20 6 9 17 4 12"/>
                </svg>
            </div>
            <div class="setting-item__content">
                <div class="setting-item__title">${escapeHtml(title)}</div>
                <div class="setting-item__sub">${escapeHtml(sub)}</div>
            </div>
        </div>
    `;
}

function bind(root) {
    const btnExport = root.querySelector('#btnExport');
    const btnDelete = root.querySelector('#btnDeletionRequest');

    if (btnExport) {
        btnExport.addEventListener('click', async () => {
            btnExport.disabled = true;
            try {
                const data = await api.get('/me/data-export');
                const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = `my-data-${new Date().toISOString().slice(0, 10)}.json`;
                document.body.appendChild(a);
                a.click();
                a.remove();
                URL.revokeObjectURL(url);
                toast.success('Файл сохранён');
            } catch (e) {
                toast.error('Не удалось выгрузить: ' + e.message);
            } finally {
                btnExport.disabled = false;
            }
        });
    }

    if (btnDelete) {
        btnDelete.addEventListener('click', async () => {
            const reason = prompt(
                'Опишите причину запроса на удаление (необязательно):',
                ''
            );
            if (reason === null) return;  // отмена
            const ok = confirm(
                'Отправить заявку на удаление персональных данных?\n\n' +
                'Это не автоматическое действие — администратор обработает заявку ' +
                'в течение 30 дней. Часть данных (квитанции за 5 лет) останется ' +
                'в обезличенном виде.'
            );
            if (!ok) return;
            btnDelete.disabled = true;
            try {
                const res = await api.post('/me/data-deletion-request', { reason: reason || '' });
                toast.success(res.message || 'Заявка отправлена');
            } catch (e) {
                toast.error('Не удалось отправить: ' + e.message);
            } finally {
                btnDelete.disabled = false;
            }
        });
    }
}

function escapeHtml(str) {
    if (str == null) return '';
    return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
