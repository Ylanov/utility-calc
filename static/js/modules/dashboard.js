// static/js/modules/dashboard.js
import { api } from '../core/api.js';
import { el, toast } from '../core/dom.js';

function esc(str) {
    if (str === null || str === undefined) return '';
    const d = document.createElement('div');
    d.textContent = String(str);
    return d.innerHTML;
}

// Человекопонятные названия действий и сущностей
const ACTION_LABELS = {
    create: '➕ Создание', update: '✏️ Изменение', delete: '🗑 Удаление',
    approve: '✅ Утверждение', approve_bulk: '⚡ Массовое утв.',
    close_period: '🔒 Закрытие периода', open_period: '📂 Открытие периода',
    import: '📥 Импорт', login: '🔑 Вход', change_password: '🔐 Смена пароля',
    relocate: '🚚 Переселение', evict: '🚪 Выселение',
    replace_meter: '🔄 Замена счётчика', adjustment: '💰 Корректировка',
    activate_tariff: '⚡ Активация тарифа',
};

const ENTITY_LABELS = {
    user: 'Жилец', room: 'Комната', tariff: 'Тариф',
    reading: 'Показания', period: 'Период', adjustment: 'Корректировка',
    system: 'Система',
};

export const DashboardModule = {
    isInitialized: false,
    auditState: { page: 1, limit: 30, total: 0, filterAction: '', filterEntity: '' },

    init() {
        this.cacheDOM();
        if (!this.isInitialized) {
            this.bindEvents();
            this.isInitialized = true;
        }
        this.loadKPI();
        this.loadAuditFilters();
        this.loadAuditLog();
    },

    cacheDOM() {
        this.dom = {
            // KPI
            kpiUsers: document.getElementById('kpiUsers'),
            kpiUsersDetail: document.getElementById('kpiUsersDetail'),
            kpiRooms: document.getElementById('kpiRooms'),
            kpiRoomsDetail: document.getElementById('kpiRoomsDetail'),
            kpiSubmitted: document.getElementById('kpiSubmitted'),
            kpiSubmittedDetail: document.getElementById('kpiSubmittedDetail'),
            kpiAnomalies: document.getElementById('kpiAnomalies'),
            kpiAnomaliesDetail: document.getElementById('kpiAnomaliesDetail'),
            kpiRevenue: document.getElementById('kpiRevenue'),
            kpiRevenueDetail: document.getElementById('kpiRevenueDetail'),
            comparisonBanner: document.getElementById('comparisonBanner'),
            comparisonText: document.getElementById('comparisonText'),
            comparisonDelta: document.getElementById('comparisonDelta'),
            // Audit
            auditContainer: document.getElementById('auditLogContainer'),
            auditFilterAction: document.getElementById('auditFilterAction'),
            auditFilterEntity: document.getElementById('auditFilterEntity'),
            auditTotal: document.getElementById('auditTotal'),
            btnRefresh: document.getElementById('btnRefreshAudit'),
            btnPrev: document.getElementById('btnAuditPrev'),
            btnNext: document.getElementById('btnAuditNext'),
            pageInfo: document.getElementById('auditPageInfo'),
        };
    },

    bindEvents() {
        if (this.dom.btnRefresh) this.dom.btnRefresh.addEventListener('click', () => {
            this.loadKPI();
            this.auditState.page = 1;
            this.loadAuditLog();
        });
        if (this.dom.btnPrev) this.dom.btnPrev.addEventListener('click', () => {
            if (this.auditState.page > 1) { this.auditState.page--; this.loadAuditLog(); }
        });
        if (this.dom.btnNext) this.dom.btnNext.addEventListener('click', () => {
            const totalPages = Math.ceil(this.auditState.total / this.auditState.limit) || 1;
            if (this.auditState.page < totalPages) { this.auditState.page++; this.loadAuditLog(); }
        });
        if (this.dom.auditFilterAction) this.dom.auditFilterAction.addEventListener('change', (e) => {
            this.auditState.filterAction = e.target.value;
            this.auditState.page = 1;
            this.loadAuditLog();
        });
        if (this.dom.auditFilterEntity) this.dom.auditFilterEntity.addEventListener('change', (e) => {
            this.auditState.filterEntity = e.target.value;
            this.auditState.page = 1;
            this.loadAuditLog();
        });
    },

    // =====================================================
    // KPI
    // =====================================================
    async loadKPI() {
        try {
            const data = await api.get('/admin/dashboard');
            this.renderKPI(data);
        } catch (e) {
            console.error('Dashboard KPI error:', e);
        }
    },

    renderKPI(data) {
        // Жильцы
        if (this.dom.kpiUsers) this.dom.kpiUsers.textContent = data.users.total;
        if (this.dom.kpiUsersDetail) {
            this.dom.kpiUsersDetail.textContent = `${data.users.with_room} с комнатой, ${data.users.without_room} без`;
        }

        // Комнаты
        if (this.dom.kpiRooms) this.dom.kpiRooms.textContent = data.rooms.total;
        if (this.dom.kpiRoomsDetail) {
            this.dom.kpiRoomsDetail.textContent = `${data.rooms.occupied} заняты, ${data.rooms.empty} свободны`;
        }

        // Показания
        if (data.period) {
            if (this.dom.kpiSubmitted) {
                this.dom.kpiSubmitted.textContent = `${data.period.submit_percent}%`;
                this.dom.kpiSubmitted.style.color = data.period.submit_percent >= 80 ? '#10b981' :
                    data.period.submit_percent >= 50 ? '#f59e0b' : '#ef4444';
            }
            if (this.dom.kpiSubmittedDetail) {
                this.dom.kpiSubmittedDetail.textContent =
                    `${data.period.submitted_rooms} из ${data.period.total_occupied_rooms} комнат (${data.period.name})`;
            }

            // Аномалии
            if (this.dom.kpiAnomalies) {
                this.dom.kpiAnomalies.textContent = data.period.anomalies;
                this.dom.kpiAnomalies.style.color = data.period.anomalies > 0 ? '#f59e0b' : '#10b981';
            }
            if (this.dom.kpiAnomaliesDetail) {
                this.dom.kpiAnomaliesDetail.textContent = `${data.period.total_drafts} черновиков всего`;
            }

            // Начислено
            if (this.dom.kpiRevenue) {
                this.dom.kpiRevenue.textContent = `${Number(data.period.approved_sum).toLocaleString('ru-RU')} ₽`;
            }
            if (this.dom.kpiRevenueDetail) {
                this.dom.kpiRevenueDetail.textContent = `${data.period.approved_count} утверждённых записей`;
            }
        } else {
            if (this.dom.kpiSubmitted) this.dom.kpiSubmitted.textContent = '—';
            if (this.dom.kpiSubmittedDetail) this.dom.kpiSubmittedDetail.textContent = 'Нет активного периода';
            if (this.dom.kpiAnomalies) this.dom.kpiAnomalies.textContent = '—';
            if (this.dom.kpiAnomaliesDetail) this.dom.kpiAnomaliesDetail.textContent = '—';
            if (this.dom.kpiRevenue) this.dom.kpiRevenue.textContent = '—';
            if (this.dom.kpiRevenueDetail) this.dom.kpiRevenueDetail.textContent = '—';
        }

        // Сравнение с прошлым периодом
        if (data.comparison && this.dom.comparisonBanner) {
            this.dom.comparisonBanner.style.display = 'block';
            const c = data.comparison;
            const color = c.delta > 0 ? '#ef4444' : c.delta < 0 ? '#10b981' : '#6b7280';
            const arrow = c.delta > 0 ? '▲' : c.delta < 0 ? '▼' : '—';
            const sign = c.delta > 0 ? '+' : '';

            this.dom.comparisonBanner.style.borderLeftColor = color;
            if (this.dom.comparisonText) {
                this.dom.comparisonText.innerHTML =
                    `<strong>${esc(c.prev_period_name)}</strong>: ${Number(c.prev_sum).toLocaleString('ru-RU')} ₽ → ` +
                    `<strong>Текущий</strong>: ${Number(c.current_sum).toLocaleString('ru-RU')} ₽`;
            }
            if (this.dom.comparisonDelta) {
                this.dom.comparisonDelta.innerHTML =
                    `<span style="color:${color}">${arrow} ${sign}${Number(c.delta).toLocaleString('ru-RU')} ₽</span>` +
                    `<div style="font-size:13px; color:${color};">${sign}${c.percent_change}%</div>`;
            }
        } else if (this.dom.comparisonBanner) {
            this.dom.comparisonBanner.style.display = 'none';
        }
    },

    // =====================================================
    // ЖУРНАЛ ДЕЙСТВИЙ
    // =====================================================
    async loadAuditFilters() {
        try {
            const data = await api.get('/admin/audit-log/actions');

            if (this.dom.auditFilterAction && data.actions) {
                let html = '<option value="">Все действия</option>';
                data.actions.forEach(a => {
                    const label = ACTION_LABELS[a.name] || a.name;
                    html += `<option value="${esc(a.name)}">${label} (${a.count})</option>`;
                });
                this.dom.auditFilterAction.innerHTML = html;
            }

            if (this.dom.auditFilterEntity && data.entities) {
                let html = '<option value="">Все объекты</option>';
                data.entities.forEach(e => {
                    const label = ENTITY_LABELS[e.name] || e.name;
                    html += `<option value="${esc(e.name)}">${label} (${e.count})</option>`;
                });
                this.dom.auditFilterEntity.innerHTML = html;
            }
        } catch (e) {
            // Если журнал пуст — фильтры будут дефолтными
        }
    },

    async loadAuditLog() {
        if (!this.dom.auditContainer) return;

        const params = new URLSearchParams({
            page: this.auditState.page,
            limit: this.auditState.limit
        });
        if (this.auditState.filterAction) params.set('action', this.auditState.filterAction);
        if (this.auditState.filterEntity) params.set('entity_type', this.auditState.filterEntity);

        try {
            const data = await api.get(`/admin/audit-log?${params.toString()}`);
            this.auditState.total = data.total;
            this.renderAuditLog(data.items);
            this.updateAuditPagination();
        } catch (e) {
            this.dom.auditContainer.innerHTML =
                `<div style="text-align:center; padding:30px; color:var(--text-secondary);">Журнал пуст или недоступен</div>`;
        }
    },

    renderAuditLog(items) {
        if (!items || items.length === 0) {
            this.dom.auditContainer.innerHTML =
                `<div style="text-align:center; padding:40px; color:var(--text-secondary);">
                    <div style="font-size:32px; margin-bottom:12px;">📋</div>
                    <div style="font-size:15px; font-weight:500;">Журнал действий пуст</div>
                    <div style="font-size:13px; margin-top:4px;">Действия будут появляться здесь по мере работы в системе</div>
                </div>`;
            return;
        }

        let html = '<div style="padding:0;">';
        items.forEach(item => {
            const actionLabel = ACTION_LABELS[item.action] || item.action;
            const entityLabel = ENTITY_LABELS[item.entity_type] || item.entity_type;
            const idPart = item.entity_id ? ` #${item.entity_id}` : '';

            let detailsHtml = '';
            if (item.details) {
                const entries = Object.entries(item.details).slice(0, 4);
                if (entries.length > 0) {
                    detailsHtml = '<div style="font-size:11px; color:#9ca3af; margin-top:4px;">' +
                        entries.map(([k, v]) => `${esc(k)}: <b>${esc(String(v))}</b>`).join(' · ') +
                        '</div>';
                }
            }

            html += `
                <div style="display:flex; gap:14px; padding:12px 16px; border-bottom:1px solid var(--border-color); align-items:flex-start;">
                    <div style="flex-shrink:0; width:36px; height:36px; border-radius:50%; background:#f3f4f6; display:flex; align-items:center; justify-content:center; font-size:14px;">
                        ${(ACTION_LABELS[item.action] || '📝').split(' ')[0]}
                    </div>
                    <div style="flex:1; min-width:0;">
                        <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:4px;">
                            <span style="font-weight:600; font-size:13px; color:var(--text-main);">${esc(item.username)}</span>
                            <span style="font-size:11px; color:#9ca3af; white-space:nowrap;">${esc(item.created_at)}</span>
                        </div>
                        <div style="font-size:13px; color:var(--text-secondary); margin-top:2px;">
                            ${actionLabel} → <span style="font-weight:500;">${entityLabel}${idPart}</span>
                        </div>
                        ${detailsHtml}
                    </div>
                </div>
            `;
        });
        html += '</div>';

        this.dom.auditContainer.innerHTML = html;
    },

    updateAuditPagination() {
        const totalPages = Math.ceil(this.auditState.total / this.auditState.limit) || 1;
        if (this.dom.pageInfo) {
            this.dom.pageInfo.textContent = `Стр. ${this.auditState.page} из ${totalPages} (${this.auditState.total} записей)`;
        }
        if (this.dom.btnPrev) this.dom.btnPrev.disabled = this.auditState.page <= 1;
        if (this.dom.btnNext) this.dom.btnNext.disabled = this.auditState.page >= totalPages;
        if (this.dom.auditTotal) this.dom.auditTotal.textContent = `Всего: ${this.auditState.total}`;
    }
};