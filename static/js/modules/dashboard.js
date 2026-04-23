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
    // Request-id счётчики — защита от race condition. Если пользователь
    // кликает рефреш/открывает KPI-модалку несколько раз подряд, ответ
    // на старый запрос может прилететь ПОСЛЕ свежего и перезаписать UI.
    // Проверяем актуальность id после каждого await.
    _kpiReqId: 0,
    _gsheetsReqId: 0,
    _detailReqId: 0,

    init() {
        this.cacheDOM();
        if (!this.isInitialized) {
            this.bindEvents();
            this.isInitialized = true;
        }
        this.loadKPI();
        this.loadGsheetsWidget();
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
            // GSheets widget
            gsheetsWidgetBody: document.getElementById('gsheetsWidgetBody'),
            gsheetsWidgetMeta: document.getElementById('gsheetsWidgetMeta'),
            btnGsheetsWidgetOpen: document.getElementById('btnGsheetsWidgetOpen'),
            btnGsheetsWidgetRefresh: document.getElementById('btnGsheetsWidgetRefresh'),
            // KPI detail modal (единая модалка для всех кликабельных KPI)
            detailModal: document.getElementById('dashboardDetailModal'),
            detailTitle: document.getElementById('dashboardDetailTitle'),
            detailBody: document.getElementById('dashboardDetailBody'),
            detailNavBtn: document.querySelector('[data-dd-navigate]'),
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
            this.loadGsheetsWidget();
            this.auditState.page = 1;
            this.loadAuditLog();
        });
        this.dom.btnGsheetsWidgetRefresh?.addEventListener('click', () => this.loadGsheetsWidget());
        this.dom.btnGsheetsWidgetOpen?.addEventListener('click', () => {
            // «Операции» — там секция gsheets. Роутер переключает таб по hash.
            window.location.hash = 'tools';
            // После переключения таба — чуть позже проскроллим до аккордеона gsheets.
            setTimeout(() => {
                document.querySelector('[data-section="gsheets"]')
                    ?.scrollIntoView({ behavior: 'smooth', block: 'start' });
            }, 400);
        });

        // Кликабельные KPI — все карточки с data-dashboard-kpi.
        document.querySelectorAll('[data-dashboard-kpi]').forEach(card => {
            card.addEventListener('click', () => this.openKpiDetail(card.dataset.dashboardKpi));
        });
        // Закрытие модалки
        this.dom.detailModal?.addEventListener('click', (e) => {
            if (e.target.closest('[data-dd-close]') || e.target === this.dom.detailModal) {
                this.closeDetailModal();
            }
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
        const myId = ++this._kpiReqId;
        try {
            const data = await api.get('/admin/dashboard');
            if (myId !== this._kpiReqId) return;  // поздний ответ — игнор
            this.renderKPI(data);
        } catch (e) {
            if (myId !== this._kpiReqId) return;
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
    // KPI DETAIL MODAL — единая модалка деталей по любой KPI-карточке.
    // Роутинг по data-dashboard-kpi: users | rooms | submissions |
    // anomalies | finance | comparison. Каждый handler использует
    // существующие endpoint-ы и не добавляет новых backend-вызовов.
    // =====================================================
    async openKpiDetail(kpi) {
        if (!this.dom.detailModal) return;
        const config = {
            users:       { title: '👥 Жильцы — распределение',        tab: 'users',   navLabel: 'Открыть вкладку «Жильцы»' },
            rooms:       { title: '🏠 Жилфонд — состояние комнат',    tab: 'housing', navLabel: 'Открыть «Жилфонд»' },
            submissions: { title: '📈 Подача показаний за период',    tab: null,      navLabel: null },
            anomalies:   { title: '⚠️ Анализатор — срабатывания',     tab: 'tools',   navLabel: 'Открыть Центр анализа' },
            finance:     { title: '💰 Финансы — должники и переплаты', tab: 'debts',   navLabel: 'Открыть «Долги 1С»' },
            comparison:  { title: '📊 Сравнение с прошлым периодом',   tab: 'tools',   navLabel: 'Открыть сравнение периодов' },
        }[kpi];
        if (!config) return;

        this.dom.detailTitle.textContent = config.title;
        this.dom.detailBody.innerHTML =
            `<div style="padding:40px; text-align:center; color:var(--text-secondary);">
                <i class="fa-solid fa-spinner fa-spin"></i> Загрузка…
            </div>`;

        // Кнопка «Открыть вкладку» — deep-link в соответствующий раздел.
        const navBtn = this.dom.detailNavBtn;
        if (navBtn) {
            if (config.tab) {
                navBtn.style.display = '';
                navBtn.querySelector('[data-dd-navigate-label]').textContent = config.navLabel;
                navBtn.onclick = () => {
                    this.closeDetailModal();
                    window.location.hash = config.tab;
                };
            } else {
                navBtn.style.display = 'none';
            }
        }
        this.dom.detailModal.classList.add('open');

        // Race guard: если админ быстро кликает разные KPI, поздний ответ
        // старого рендера мог перезаписать свежий заголовок — в модалку
        // «Жильцы» показывались бы цифры из «Комнат». Проверяем id после await.
        const myId = ++this._detailReqId;
        this._currentKpi = kpi;

        try {
            switch (kpi) {
                case 'users':       await this._renderUsersDetail(myId); break;
                case 'rooms':       await this._renderRoomsDetail(myId); break;
                case 'submissions': await this._renderSubmissionsDetail(myId); break;
                case 'anomalies':   await this._renderAnomaliesDetail(myId); break;
                case 'finance':     await this._renderFinanceDetail(myId); break;
                case 'comparison':  await this._renderComparisonDetail(myId); break;
            }
        } catch (e) {
            if (myId !== this._detailReqId) return;
            this.dom.detailBody.innerHTML =
                `<div style="padding:24px; color:var(--danger-color);">Ошибка: ${this._escape(e.message)}</div>`;
        }
    },

    closeDetailModal() {
        this.dom.detailModal?.classList.remove('open');
    },

    // Универсальный рендер KPI-блока в модалке: сетка маленьких карточек.
    _detailGrid(cards) {
        return `
            <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(140px, 1fr)); gap:10px; margin-bottom:16px;">
                ${cards.map(c => `
                    <div style="background:${c.bg || '#f9fafb'}; border:1px solid ${(c.color || '#6b7280')}22; border-radius:10px; padding:12px;">
                        <div style="font-size:11px; color:${c.color || '#6b7280'}; text-transform:uppercase; letter-spacing:.3px; margin-bottom:4px;">
                            ${this._escape(c.label)}
                        </div>
                        <div style="font-size:22px; font-weight:700; color:#111827;">${this._escape(String(c.value))}</div>
                        ${c.hint ? `<div style="font-size:11px; color:var(--text-secondary); margin-top:3px;">${this._escape(c.hint)}</div>` : ''}
                    </div>
                `).join('')}
            </div>
        `;
    },

    async _renderUsersDetail(myId) {
        const s = await api.get('/users/stats');
        if (myId !== undefined && myId !== this._detailReqId) return;
        const cards = [
            { label: 'Всего жильцов',   value: s.total_users,      color: '#2563eb', bg: '#eff6ff' },
            { label: 'С комнатой',      value: s.with_room,        color: '#10b981', bg: '#ecfdf5' },
            { label: 'Семьи',           value: s.by_resident_type?.family || 0,  color: '#7c3aed', bg: '#f5f3ff' },
            { label: 'Холостяки',       value: s.by_resident_type?.single || 0,  color: '#ea580c', bg: '#fff7ed' },
            { label: 'По счётчикам',    value: s.by_billing_mode?.by_meter || 0,  color: '#0ea5e9', bg: '#eff6ff' },
            { label: 'Койко-место',     value: s.by_billing_mode?.per_capita || 0,color: '#d946ef', bg: '#fdf4ff' },
        ];
        const dorms = s.by_dormitory || [];
        const dormTable = dorms.length ? `
            <h4 style="margin:0 0 10px;"><i class="fa-solid fa-building"></i> По общежитиям</h4>
            <table style="width:100%; border-collapse:collapse; font-size:13px; background:white; border:1px solid var(--border-color); border-radius:6px; overflow:hidden;">
                <thead style="background:#f3f4f6;">
                    <tr><th style="text-align:left; padding:8px 12px;">Общежитие</th>
                        <th style="text-align:right; padding:8px 12px;">Жильцов</th></tr>
                </thead>
                <tbody>${dorms.map(d => `
                    <tr style="border-top:1px solid #f3f4f6;">
                        <td style="padding:8px 12px;">${this._escape(d.name)}</td>
                        <td style="padding:8px 12px; text-align:right; font-weight:600;">${d.count}</td>
                    </tr>`).join('')}</tbody>
            </table>
        ` : '';
        const topD = (s.top_debtors || []).slice(0, 5);
        const topO = (s.top_overpaid || []).slice(0, 5);
        const topList = (items, color, label, field) => items.length ? `
            <h4 style="margin:16px 0 8px;"><i class="fa-solid fa-chart-simple" style="color:${color};"></i> ${label}</h4>
            <ul style="list-style:none; margin:0; padding:0; font-size:13px; background:white; border:1px solid var(--border-color); border-radius:6px; overflow:hidden;">
                ${items.map(r => `
                    <li style="padding:8px 12px; border-top:1px solid #f3f4f6; display:flex; justify-content:space-between;">
                        <span>${this._escape(r.username)}</span>
                        <b style="color:${color};">${Number(r[field] || 0).toLocaleString('ru-RU', {minimumFractionDigits:2, maximumFractionDigits:2})} ₽</b>
                    </li>`).join('')}
            </ul>
        ` : '';

        this.dom.detailBody.innerHTML = `
            ${this._detailGrid(cards)}
            ${dormTable}
            ${topList(topD, '#dc2626', 'Топ-5 должников', 'debt')}
            ${topList(topO, '#7c3aed', 'Топ-5 переплатчиков', 'overpayment')}
        `;
    },

    async _renderRoomsDetail(myId) {
        const s = await api.get('/rooms/stats');
        if (myId !== undefined && myId !== this._detailReqId) return;
        const cards = [
            { label: 'Всего комнат',  value: s.total_rooms,       color: '#2563eb', bg: '#eff6ff' },
            { label: 'Пустых',        value: s.empty,             color: '#6b7280', bg: '#f3f4f6' },
            { label: 'Частичных',     value: s.partial,           color: '#a16207', bg: '#fef9c3' },
            { label: 'Полных',        value: s.full,              color: '#10b981', bg: '#ecfdf5' },
            { label: 'Переполнено',   value: s.overcrowded,       color: '#dc2626', bg: '#fef2f2' },
            { label: 'Заполненность', value: `${s.occupancy_pct}%`,color: '#4338ca', bg: '#eef2ff',
              hint: `${s.total_residents}/${s.total_capacity} мест` },
            { label: 'Без счётчиков', value: s.missing_meters_count, color: '#ea580c', bg: '#fff7ed',
              hint: 'не хватает серийника ГВС/ХВС/эл.' },
        ];
        const byD = s.by_dormitory || [];
        this.dom.detailBody.innerHTML = `
            ${this._detailGrid(cards)}
            ${byD.length ? `
                <h4 style="margin:0 0 10px;"><i class="fa-solid fa-building"></i> По общежитиям</h4>
                <table style="width:100%; border-collapse:collapse; font-size:13px; background:white; border:1px solid var(--border-color); border-radius:6px; overflow:hidden;">
                    <thead style="background:#f3f4f6;">
                        <tr>
                            <th style="text-align:left; padding:8px 12px;">Общежитие</th>
                            <th style="text-align:right; padding:8px 12px;">Комнат</th>
                            <th style="text-align:right; padding:8px 12px;">Жильцов / мест</th>
                            <th style="text-align:right; padding:8px 12px;">Заполн.</th>
                        </tr>
                    </thead>
                    <tbody>${byD.map(d => `
                        <tr style="border-top:1px solid #f3f4f6;">
                            <td style="padding:8px 12px;">${this._escape(d.name)}</td>
                            <td style="padding:8px 12px; text-align:right;">${d.rooms}</td>
                            <td style="padding:8px 12px; text-align:right;">${d.residents} / ${d.capacity}</td>
                            <td style="padding:8px 12px; text-align:right; color:${d.occupancy_pct >= 80 ? '#10b981' : d.occupancy_pct >= 50 ? '#f59e0b' : '#ef4444'}; font-weight:600;">${d.occupancy_pct}%</td>
                        </tr>`).join('')}</tbody>
                </table>
            ` : ''}
        `;
    },

    async _renderSubmissionsDetail(myId) {
        const data = await api.get('/admin/periods/close-preview');
        if (myId !== undefined && myId !== this._detailReqId) return;
        const pct = data.total_occupied_rooms > 0
            ? Math.round(data.rooms_with_readings / data.total_occupied_rooms * 100) : 0;
        const cards = [
            { label: 'Сдали',        value: data.rooms_with_readings,    color: '#10b981', bg: '#ecfdf5' },
            { label: 'Не сдали',     value: data.rooms_without_readings, color: '#dc2626', bg: '#fef2f2' },
            { label: 'Авто-утв.',    value: data.safe_drafts,            color: '#3b82f6', bg: '#eff6ff' },
            { label: 'Аномалий',     value: data.anomalies_count,        color: '#f59e0b', bg: '#fef3c7' },
            { label: '% сдачи',      value: `${pct}%`,                   color: '#4338ca', bg: '#eef2ff' },
        ];
        const dorms = data.dormitories || [];
        this.dom.detailBody.innerHTML = `
            ${this._detailGrid(cards)}
            ${dorms.length ? `
                <h4 style="margin:0 0 10px;"><i class="fa-solid fa-building"></i> По общежитиям</h4>
                <table style="width:100%; border-collapse:collapse; font-size:13px; background:white; border:1px solid var(--border-color); border-radius:6px; overflow:hidden;">
                    <thead style="background:#f3f4f6;">
                        <tr>
                            <th style="text-align:left; padding:8px 12px;">Общежитие</th>
                            <th style="text-align:center; padding:8px 12px; color:#10b981;">Сдали</th>
                            <th style="text-align:center; padding:8px 12px; color:#ef4444;">Не сдали</th>
                            <th style="text-align:center; padding:8px 12px;">%</th>
                        </tr>
                    </thead>
                    <tbody>${dorms.map(d => `
                        <tr style="border-top:1px solid #f3f4f6;">
                            <td style="padding:8px 12px;">${this._escape(d.name)}</td>
                            <td style="padding:8px 12px; text-align:center; font-weight:600;">${d.submitted}</td>
                            <td style="padding:8px 12px; text-align:center; color:${d.missing > 0 ? '#ef4444' : '#9ca3af'}; font-weight:600;">${d.missing}</td>
                            <td style="padding:8px 12px; text-align:center; color:${d.percent >= 80 ? '#10b981' : d.percent >= 50 ? '#f59e0b' : '#ef4444'}; font-weight:600;">${d.percent}%</td>
                        </tr>`).join('')}</tbody>
                </table>
            ` : ''}
        `;
    },

    async _renderAnomaliesDetail(myId) {
        const data = await api.get('/admin/analyzer/dashboard?days=30');
        if (myId !== undefined && myId !== this._detailReqId) return;
        const a = data.anomalies || {};
        const sev = a.by_severity || {};
        const cards = [
            { label: 'Всего за 30 дн.',   value: a.total_flagged_readings || 0, color: '#dc2626', bg: '#fef2f2' },
            { label: 'Критических',       value: sev['critical (80-100)'] || 0, color: '#ef4444', bg: '#fee2e2' },
            { label: 'Средних',           value: sev['medium (40-79)'] || 0,    color: '#f59e0b', bg: '#fef3c7' },
            { label: 'Низких',            value: sev['low (1-39)'] || 0,        color: '#10b981', bg: '#ecfdf5' },
        ];
        const top = a.top_flags || [];
        const max = top[0]?.count || 1;
        this.dom.detailBody.innerHTML = `
            ${this._detailGrid(cards)}
            <h4 style="margin:0 0 10px;"><i class="fa-solid fa-fire" style="color:#dc2626;"></i> Топ срабатываний</h4>
            ${top.length ? top.map(f => {
                const pctBar = Math.round((f.count / max) * 100);
                return `
                    <div style="display:flex; align-items:center; gap:10px; margin-bottom:6px; font-size:13px;">
                        <div style="width:200px; font-family:monospace;" title="${this._escape(f.flag)}">${this._escape(f.flag)}</div>
                        <div style="flex:1; background:#f3f4f6; border-radius:4px; height:14px; overflow:hidden;">
                            <div style="width:${pctBar}%; height:100%; background:#dc2626;"></div>
                        </div>
                        <div style="width:40px; text-align:right; font-weight:600;">${f.count}</div>
                    </div>`;
            }).join('') : `<div style="color:var(--text-secondary); font-style:italic;">За 30 дней аномалий не было.</div>`}
        `;
    },

    async _renderFinanceDetail(myId) {
        // Берём stats по долгам (быстро, одно число + разбивка).
        const s = await api.get('/financier/debts/stats');
        if (myId !== undefined && myId !== this._detailReqId) return;
        const fmtMoney = (v) => Number(v || 0).toLocaleString('ru-RU', {minimumFractionDigits:2, maximumFractionDigits:2}) + ' ₽';
        const cards = [
            { label: 'Период',        value: s.period_name || '—',  color: '#7c3aed', bg: '#f5f3ff' },
            { label: 'Должников',     value: s.debtors_count,       color: '#dc2626', bg: '#fef2f2' },
            { label: 'Переплатчиков', value: s.overpayers_count,    color: '#10b981', bg: '#ecfdf5' },
            { label: 'Сумма долга',   value: fmtMoney(s.total_debt),color: '#ea580c', bg: '#fff7ed' },
            { label: 'Сумма перепл.', value: fmtMoney(s.total_overpay), color: '#059669', bg: '#ecfdf5' },
            { label: 'Ср. долг',      value: fmtMoney(s.avg_debt_per_debtor), color: '#f59e0b', bg: '#fef3c7' },
        ];
        const dorms = s.by_dormitory || [];
        this.dom.detailBody.innerHTML = `
            ${this._detailGrid(cards)}
            ${dorms.length ? `
                <h4 style="margin:0 0 10px;"><i class="fa-solid fa-building"></i> Топ-${dorms.length} общежитий по долгу</h4>
                <table style="width:100%; border-collapse:collapse; font-size:13px; background:white; border:1px solid var(--border-color); border-radius:6px; overflow:hidden;">
                    <thead style="background:#f3f4f6;">
                        <tr>
                            <th style="text-align:left; padding:8px 12px;">Общежитие</th>
                            <th style="text-align:right; padding:8px 12px;">Должников</th>
                            <th style="text-align:right; padding:8px 12px;">Сумма</th>
                        </tr>
                    </thead>
                    <tbody>${dorms.map(d => `
                        <tr style="border-top:1px solid #f3f4f6;">
                            <td style="padding:8px 12px;">${this._escape(d.name)}</td>
                            <td style="padding:8px 12px; text-align:right;">${d.debtors}</td>
                            <td style="padding:8px 12px; text-align:right; color:#dc2626; font-weight:600;">${fmtMoney(d.total_debt)}</td>
                        </tr>`).join('')}</tbody>
                </table>
            ` : ''}
        `;
    },

    async _renderComparisonDetail() {
        // Без запросов — используем то что уже лежит в comparisonBanner.
        // Если данные не загружены — предлагаем открыть tools → compare.
        const txt = (this.dom.comparisonText?.textContent || '').trim();
        const delta = (this.dom.comparisonDelta?.textContent || '').trim();
        this.dom.detailBody.innerHTML = `
            <div style="padding:20px;">
                <div style="display:flex; justify-content:space-between; align-items:center; padding:16px 20px; background:#eff6ff; border-radius:8px; border-left:4px solid #3b82f6; margin-bottom:16px;">
                    <div>
                        <div style="font-size:13px; color:var(--text-secondary);">Сравнение двух периодов</div>
                        <div style="font-size:15px; margin-top:4px;">${this._escape(txt || '—')}</div>
                    </div>
                    <div style="font-size:22px; font-weight:700;">${this._escape(delta || '—')}</div>
                </div>
                <p class="hint-text" style="font-size:13px;">
                    Для подробного сравнения по ресурсам и общежитиям — откройте Операции → Центр анализа → «Анализ периода».
                </p>
            </div>
        `;
    },

    // =====================================================
    // ВИДЖЕТ GOOGLE SHEETS
    // Легковесная сводка на дашборде — чтобы админ видел
    // состояние буфера импорта без переключения вкладок.
    // Используется существующий endpoint /admin/gsheets/stats —
    // новых запросов на backend не добавляем.
    // =====================================================
    async loadGsheetsWidget() {
        if (!this.dom.gsheetsWidgetBody) return;
        const myId = ++this._gsheetsReqId;
        try {
            const s = await api.get('/admin/gsheets/stats');
            if (myId !== this._gsheetsReqId) return;
            this.renderGsheetsWidget(s);
        } catch (e) {
            if (myId !== this._gsheetsReqId) return;
            this.dom.gsheetsWidgetBody.innerHTML =
                `<div style="padding:12px; color:var(--danger-color); grid-column:1/-1;">Ошибка: ${this._escape(e.message)}</div>`;
            if (this.dom.gsheetsWidgetMeta) this.dom.gsheetsWidgetMeta.textContent = '';
        }
    },

    renderGsheetsWidget(s) {
        const by = s.by_status || {};
        const active = (by.pending || 0) + (by.conflict || 0) + (by.unmatched || 0);
        const auto = by.auto_approved || 0;
        const done = by.approved || 0;
        const reject = by.rejected || 0;

        // Мета-строка в шапке виджета: последний импорт + статус настройки sheet_id.
        if (this.dom.gsheetsWidgetMeta) {
            const parts = [];
            if (!s.sheet_id_configured) {
                parts.push('<span style="color:var(--danger-color);">⚠ GSHEETS_SHEET_ID не задан</span>');
            } else if (s.last_import_at) {
                const d = new Date(s.last_import_at);
                parts.push('последний sync: ' + d.toLocaleString('ru-RU', {
                    day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit',
                }));
            } else {
                parts.push('импортов пока не было');
            }
            if (s.auto_sync_interval_min) {
                parts.push(`авто каждые ${s.auto_sync_interval_min} мин`);
            }
            this.dom.gsheetsWidgetMeta.innerHTML = parts.join(' · ');
        }

        const chip = (bg, color, icon, value, label, big = false) => `
            <div style="background:${bg}; border:1px solid ${color}33; border-radius:10px; padding:12px; text-align:left;">
                <div style="display:flex; align-items:center; gap:6px; color:${color}; font-size:11px; text-transform:uppercase; letter-spacing:0.3px; margin-bottom:4px;">
                    <span>${icon}</span>${this._escape(label)}
                </div>
                <div style="font-size:${big ? '24' : '20'}px; font-weight:700; color:${big && value > 0 ? color : '#111827'};">${value}</div>
            </div>
        `;

        // Подсветка «требуют действия» красным, если > 0
        const activeColor = active > 0 ? '#dc2626' : '#6b7280';
        const activeBg = active > 0 ? '#fef2f2' : '#f9fafb';

        this.dom.gsheetsWidgetBody.innerHTML = [
            chip(activeBg, activeColor, '⚠️', active, 'Требуют действия', true),
            chip('#eff6ff', '#3b82f6', '⏳', by.pending || 0, 'В ожидании'),
            chip('#fef3c7', '#f59e0b', '🔀', by.conflict || 0, 'Конфликт'),
            chip('#fee2e2', '#ef4444', '🔍', by.unmatched || 0, 'Не найден'),
            chip('#ede9fe', '#8b5cf6', '🤖', auto, 'Авто-утверждено'),
            chip('#d1fae5', '#10b981', '✅', done, 'Утверждено'),
            chip('#f3f4f6', '#6b7280', '🗑', reject, 'Отклонено'),
        ].join('');
    },

    _escape(s) {
        if (s == null) return '';
        return String(s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
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