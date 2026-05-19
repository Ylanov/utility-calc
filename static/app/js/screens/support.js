/**
 * Экран «Обращения» — жилец задаёт вопрос админу.
 *
 * UX: список своих обращений + кнопка «Новое обращение».
 * При клике на тикет — раскрывается с ответом админа (если есть).
 * Новое обращение — простая форма subject + message.
 */
import { api } from '../api.js';
import { toast } from '../toast.js';

let _list = [];

export async function renderSupport(root) {
    root.innerHTML = renderSkeleton();
    try {
        const data = await api.get('/me/tickets');
        _list = data.items || [];
    } catch (e) {
        toast.error('Не удалось загрузить обращения: ' + e.message);
        _list = [];
    }
    root.innerHTML = render(_list);
    bind(root);
}

function renderSkeleton() {
    return `
        <div class="screen">
            <h2 style="font-size:22px; font-weight:700; margin-bottom: var(--sp-4);">Обращения</h2>
            <div class="skeleton skeleton--card"></div>
            <div class="skeleton skeleton--card"></div>
        </div>
    `;
}

function render(items) {
    const list = items.length
        ? items.map(renderItem).join('')
        : `<div class="empty">
            <svg class="empty__icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
            </svg>
            <div class="empty__title">Пока нет обращений</div>
            <div class="empty__sub">Если у вас есть вопрос — напишите его ниже, администратор ответит.</div>
        </div>`;

    return `
        <div class="screen">
            <h2 style="font-size:22px; font-weight:700; margin-bottom: var(--sp-2);">Обращения</h2>
            <p style="color:var(--text-secondary); font-size:13px; line-height:1.5; margin-bottom: var(--sp-5);">
                Задайте вопрос в бухгалтерию или администрации — получите ответ
                прямо в личном кабинете.
            </p>

            <button id="btnNewTicket" class="btn btn--primary" style="margin-bottom: var(--sp-5);">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" style="margin-right:8px;">
                    <path d="M12 5v14M5 12h14"/>
                </svg>
                Новое обращение
            </button>

            ${list}
        </div>
    `;
}

function renderItem(t) {
    const created = t.created_at
        ? new Date(t.created_at).toLocaleString('ru-RU', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' })
        : '—';
    const responded = t.responded_at
        ? new Date(t.responded_at).toLocaleString('ru-RU', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' })
        : '';
    const statusBadge = renderStatusBadge(t.status);

    return `
        <details class="card" style="margin-bottom: var(--sp-3); padding: 0;">
            <summary style="
                padding: var(--sp-4) var(--sp-5);
                cursor: pointer;
                display: flex;
                justify-content: space-between;
                align-items: flex-start;
                gap: var(--sp-3);
                list-style: none;
            ">
                <div style="flex:1; min-width:0;">
                    <div style="font-weight: 600; font-size: 15px; line-height:1.3; margin-bottom: 4px;">
                        ${escapeHtml(t.subject)}
                    </div>
                    <div style="font-size: 12px; color: var(--text-secondary);">
                        ${escapeHtml(created)}
                    </div>
                </div>
                ${statusBadge}
            </summary>
            <div style="padding: 0 var(--sp-5) var(--sp-5); font-size: 14px; line-height:1.5;">
                <div style="color: var(--text-secondary); font-size: 11px; text-transform: uppercase; letter-spacing: .4px; margin-bottom: 6px;">
                    Ваш вопрос:
                </div>
                <div style="white-space: pre-wrap; margin-bottom: var(--sp-4);">${escapeHtml(t.message)}</div>

                ${t.admin_response ? `
                    <div style="background: var(--success-bg); border-left: 4px solid var(--success); padding: var(--sp-3) var(--sp-4); border-radius: var(--radius-md);">
                        <div style="color: var(--success); font-size: 11px; text-transform: uppercase; letter-spacing: .4px; margin-bottom: 6px; font-weight: 600;">
                            Ответ администратора
                        </div>
                        <div style="white-space: pre-wrap; color: var(--text-primary);">${escapeHtml(t.admin_response)}</div>
                        <div style="font-size: 11px; color: var(--text-secondary); margin-top: 8px;">
                            ${escapeHtml(t.responded_by_username || '—')} · ${escapeHtml(responded)}
                        </div>
                    </div>
                ` : `
                    <div style="color: var(--text-muted); font-style: italic; font-size: 13px;">
                        Администратор ещё не ответил. Срок рассмотрения — до 10 рабочих дней.
                    </div>
                `}
            </div>
        </details>
    `;
}

function renderStatusBadge(status) {
    const map = {
        open:        { label: 'Открыто',     cls: 'status--draft' },
        in_progress: { label: 'В работе',    cls: 'status--open' },
        answered:    { label: 'Отвечено ✓',  cls: 'status--approved' },
        closed:      { label: 'Закрыто',     cls: 'status--closed' },
    };
    const conf = map[status] || map.open;
    return `<span class="status ${conf.cls}">${conf.label}</span>`;
}

function bind(root) {
    const btn = root.querySelector('#btnNewTicket');
    if (btn) {
        btn.addEventListener('click', () => openNewTicketModal(root));
    }
}

function openNewTicketModal(root) {
    const existing = document.getElementById('new-ticket-overlay');
    if (existing) existing.remove();

    const overlay = document.createElement('div');
    overlay.id = 'new-ticket-overlay';
    overlay.style.cssText = `
        position: fixed;
        inset: 0;
        background: rgba(0, 0, 0, 0.5);
        backdrop-filter: blur(4px);
        z-index: 1000;
        display: flex;
        align-items: flex-end;
        justify-content: center;
        padding: var(--sp-4);
        animation: fade-in 200ms ease;
    `;
    overlay.innerHTML = `
        <div style="
            background: var(--bg-elevated);
            border-radius: var(--radius-lg);
            padding: var(--sp-5);
            max-width: var(--max-content-width);
            width: 100%;
            max-height: 85vh;
            overflow-y: auto;
            animation: slide-up 240ms ease;
        ">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom: var(--sp-4);">
                <h3 style="font-size: 18px; font-weight: 700;">Новое обращение</h3>
                <button id="closeNewTicket" type="button" style="background: var(--bg-surface); width: 32px; height: 32px; border-radius: 50%; font-size: 16px; color: var(--text-secondary); border: none; cursor: pointer;">✕</button>
            </div>
            <form id="newTicketForm">
                <div class="form-group" style="margin-bottom: var(--sp-3);">
                    <label style="display: block; font-size: 13px; font-weight: 500; margin-bottom: 6px;">Тема</label>
                    <input type="text" name="subject" required minlength="3" maxlength="200" placeholder="Кратко о вашем вопросе"
                        style="width: 100%; padding: var(--sp-3); border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--bg-page); color: var(--text-primary); font-size: 15px;">
                </div>
                <div class="form-group" style="margin-bottom: var(--sp-4);">
                    <label style="display: block; font-size: 13px; font-weight: 500; margin-bottom: 6px;">Сообщение</label>
                    <textarea name="message" required minlength="10" maxlength="5000" rows="6" placeholder="Опишите ваш вопрос подробно..."
                        style="width: 100%; padding: var(--sp-3); border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--bg-page); color: var(--text-primary); font-size: 15px; resize: vertical; min-height: 120px; font-family: inherit;"></textarea>
                </div>
                <button type="submit" class="btn btn--primary" style="width: 100%;">
                    Отправить
                </button>
                <p style="margin-top: var(--sp-3); font-size: 12px; color: var(--text-muted); text-align: center;">
                    Срок ответа — до 10 рабочих дней (152-ФЗ).
                </p>
            </form>
        </div>
    `;
    document.body.appendChild(overlay);

    const close = () => overlay.remove();
    overlay.querySelector('#closeNewTicket')?.addEventListener('click', close);
    overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });

    overlay.querySelector('#newTicketForm')?.addEventListener('submit', async (e) => {
        e.preventDefault();
        const form = e.target;
        const submitBtn = form.querySelector('button[type="submit"]');
        submitBtn.disabled = true;
        submitBtn.textContent = 'Отправка...';
        try {
            const fd = new FormData(form);
            await api.post('/me/tickets', {
                subject: fd.get('subject').trim(),
                message: fd.get('message').trim(),
            });
            toast.success('Обращение отправлено — ждите ответа в этом разделе');
            close();
            await renderSupport(root);
        } catch (err) {
            toast.error('Ошибка: ' + err.message);
            submitBtn.disabled = false;
            submitBtn.textContent = 'Отправить';
        }
    });
}

function escapeHtml(str) {
    if (str == null) return '';
    return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
