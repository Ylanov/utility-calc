/**
 * Экран истории квитанций.
 * GET /api/readings/history — отдаёт список approved-квитанций жильца.
 * У каждой можно скачать PDF — /api/client/receipts/{id}/download.
 */
import { api, formatMoney } from '../api.js';
import { toast } from '../toast.js';

export async function renderHistory(root) {
    root.innerHTML = renderSkeleton();
    let data = null;
    try {
        data = await api.get('/readings/history?limit=50');
    } catch (e) {
        toast.error('Не удалось загрузить историю: ' + e.message);
        root.innerHTML = renderError(e.message);
        return;
    }
    const items = Array.isArray(data) ? data : (data?.items || []);
    root.innerHTML = renderList(items);
}

function renderSkeleton() {
    return `
        <div class="screen">
            <h2 style="font-size:22px; font-weight:700; margin-bottom: var(--sp-4);">История квитанций</h2>
            <div class="skeleton skeleton--card" style="height:70px;"></div>
            <div class="skeleton skeleton--card" style="height:70px;"></div>
            <div class="skeleton skeleton--card" style="height:70px;"></div>
        </div>
    `;
}

function renderError(msg) {
    return `
        <div class="screen">
            <h2 style="font-size:22px; font-weight:700; margin-bottom: var(--sp-3);">История</h2>
            <div class="status-box status-box--locked">
                <div class="status-box__title">⚠ Ошибка</div>
                <div class="status-box__sub">${escapeHtml(msg)}</div>
            </div>
        </div>
    `;
}

function renderList(items) {
    if (!items.length) {
        return `
            <div class="screen">
                <h2 style="font-size:22px; font-weight:700; margin-bottom: var(--sp-3);">История</h2>
                <div class="empty">
                    <svg class="empty__icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                        <rect x="3" y="4" width="18" height="16" rx="2"/>
                        <path d="M7 9h10M7 13h7M7 17h4"/>
                    </svg>
                    <div class="empty__title">Пока пусто</div>
                    <div class="empty__sub">Подайте показания за текущий период — после утверждения квитанция появится здесь.</div>
                </div>
            </div>
        `;
    }
    const rows = items.map(renderRow).join('');
    return `
        <div class="screen">
            <h2 style="font-size:22px; font-weight:700; margin-bottom: var(--sp-4);">История квитанций</h2>
            ${rows}
        </div>
    `;
}

function renderRow(item) {
    const period = item.period_name || '—';
    const amount = Number(item.total_cost || 0);
    const date = item.created_at
        ? new Date(item.created_at).toLocaleDateString('ru-RU', { day: '2-digit', month: 'short', year: 'numeric' })
        : '';
    const pdfHref = item.reading_id
        ? `/api/client/receipts/${item.reading_id}/download`
        : null;
    return `
        <div class="history-month">
            <div>
                <div class="history-month__period">${escapeHtml(period)}</div>
                ${date ? `<div class="history-month__sub">Утверждено: ${escapeHtml(date)}</div>` : ''}
            </div>
            <div class="history-month__amount">
                <div class="history-month__amount-value">${formatMoney(amount)} ₽</div>
            </div>
            ${pdfHref ? `
                <a class="history-month__pdf-btn" href="${pdfHref}" target="_blank" rel="noopener" aria-label="Скачать PDF">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/>
                    </svg>
                </a>
            ` : ''}
        </div>
    `;
}

function escapeHtml(str) {
    if (str == null) return '';
    return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
