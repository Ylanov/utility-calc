// static/js/modules/readings.js (ФИНАЛЬНАЯ ВЕРСИЯ)
import { api } from '../core/api.js';
import { el, toast, setLoading, showPrompt } from '../core/dom.js';
import { TableController } from '../core/table-controller.js';

const ANOMALY_MAP = {
    "NEGATIVE": { color: "#c0392b", label: "Ошибка (<0)" },
    "ZERO": { color: "#f39c12", label: "Нулевой" },
    "HIGH": { color: "#e74c3c", label: "Высокий" },
    "FROZEN": { color: "#3498db", label: "Замерзший" },
    "PEERS": { color: "#9b59b6", label: "Аномалия (Группа)" }
};

export const ReadingsModule = {
    table: null,

    init() {
        this.cacheDOM();
        this.bindEvents();
        this.loadActivePeriod();
        this.initTable();
    },

    cacheDOM() {
        this.dom = {
            btnRefresh: document.getElementById('btnRefreshReadings'),
            btnBulk: document.getElementById('btnBulkApprove'),
            filterCheckbox: document.getElementById('filterAnomalies'),
            periodActive: document.getElementById('periodActiveState'),
            periodClosed: document.getElementById('periodClosedState'),
            periodLabel: document.getElementById('activePeriodLabel'),
            btnClosePeriod: document.querySelector('#periodActiveState button'),
            periodNameInput: document.getElementById('newPeriodNameInput'),
            btnOpenPeriod: document.querySelector('#periodClosedState button')
        };
    },

    bindEvents() {
        if (this.dom.btnRefresh) {
            this.dom.btnRefresh.addEventListener('click', () => this.table.refresh());
        }

        // ИСПРАВЛЕНО: Теперь обработчик просто перезагружает таблицу
        if (this.dom.filterCheckbox) {
            this.dom.filterCheckbox.addEventListener('change', () => {
                if (this.table) {
                    this.table.state.page = 1;
                    this.table.load();
                }
            });
        }

        if (this.dom.btnBulk) this.dom.btnBulk.addEventListener('click', () => this.bulkApprove());
        if (this.dom.btnClosePeriod) this.dom.btnClosePeriod.addEventListener('click', () => this.closePeriodAction());
        if (this.dom.btnOpenPeriod) this.dom.btnOpenPeriod.addEventListener('click', () => this.openPeriodAction());
    },

    initTable() {
        this.table = new TableController({
            endpoint: '/admin/readings',

            dom: {
                tableBody: 'readingsTableBody',
                prevBtn: 'btnPrev',
                nextBtn: 'btnNext',
                pageInfo: 'pageIndicator'
            },

            // ИСПРАВЛЕНО: Используем новую функцию для передачи фильтра
            getExtraParams: () => {
                return {
                    anomalies_only: this.dom.filterCheckbox.checked
                };
            },

            renderRow: (r) => {
                return el('tr', {},
                    el('td', {},
                        el('div', { style: { fontWeight: '600' } }, r.username),
                        el('div', { style: { fontSize: '11px', color: '#888' } }, r.dormitory || 'Общ. не указано')
                    ),
                    el('td', {}, this.createBadges(r.anomaly_flags)),
                    el('td', { class: 'text-right' }, Number(r.cur_hot).toFixed(3)),
                    el('td', { class: 'text-right' }, Number(r.cur_cold).toFixed(3)),
                    el('td', { class: 'text-right' }, Number(r.cur_elect).toFixed(3)),
                    el('td', { class: 'text-right', style: { color: '#27ae60', fontWeight: 'bold' } },
                        `${Number(r.total_cost).toFixed(2)} ₽`
                    ),
                    el('td', { class: 'text-center' },
                        el('button', {
                            class: 'btn-icon btn-adjust',
                            title: 'Финансовая корректировка',
                            style: { marginRight: '5px' },
                            onclick: () => this.openAdjustmentModal(r.user_id, r.username)
                        }, '±'),
                        el('button', {
                            class: 'btn-icon btn-check',
                            title: 'Проверить и утвердить',
                            onclick: () => this.openApproveModal(r)
                        }, '✓')
                    )
                );
            }
        });

        this.table.init();
    },

    // Остальные методы (createBadges, loadActivePeriod, модалки и т.д.) остаются без изменений
    createBadges(flags) {
        if (!flags) return el('span', { style: { color: '#ccc' } }, '-');
        const container = el('div', { style: { display: 'flex', gap: '4px', flexWrap: 'wrap' } });
        flags.split(',').forEach(flag => {
            let type = "UNKNOWN";
            for (const key in ANOMALY_MAP) {
                if (flag.includes(key)) type = key;
            }
            const meta = ANOMALY_MAP[type] || { color: '#95a5a6', label: flag };
            container.appendChild(el('span', {
                title: flag,
                style: {
                    background: meta.color, color: 'white', padding: '2px 6px',
                    borderRadius: '4px', fontSize: '10px', fontWeight: 'bold', cursor: 'help'
                }
            }, meta.label));
        });
        return container;
    },

    async loadActivePeriod() {
        try {
            const data = await api.get('/admin/periods/active');
            if (data && data.name) {
                this.dom.periodActive.style.display = 'flex';
                this.dom.periodClosed.style.display = 'none';
                this.dom.periodLabel.textContent = data.name;
            } else {
                this.dom.periodActive.style.display = 'none';
                this.dom.periodClosed.style.display = 'flex';
            }
        } catch (e) { console.warn("Ошибка проверки периода", e); }
    },

    async openApproveModal(reading) {
        const modal = document.getElementById('approveModal');
        if (!modal) return;
        document.getElementById('modal_reading_id').value = reading.id;
        document.getElementById('m_username').textContent = reading.username;
        const dHot = (Number(reading.cur_hot) - Number(reading.prev_hot)).toFixed(3);
        const dCold = (Number(reading.cur_cold) - Number(reading.prev_cold)).toFixed(3);
        const dElect = (Number(reading.cur_elect) - Number(reading.prev_elect)).toFixed(3);
        document.getElementById('m_hot_usage').textContent = dHot;
        document.getElementById('m_cold_usage').textContent = dCold;
        document.getElementById('m_elect_usage').textContent = dElect;
        ['m_corr_hot', 'm_corr_cold', 'm_corr_elect', 'm_corr_sewage'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.value = 0;
        });
        modal.classList.add('open');
        const btnSubmit = document.getElementById('btnModalSubmit');
        btnSubmit.onclick = () => this.submitApproval(reading.id);
        const btnClose = document.getElementById('btnModalClose');
        btnClose.onclick = () => modal.classList.remove('open');
    },

    async submitApproval(id) {
        const btn = document.getElementById('btnModalSubmit');
        const data = {
            hot_correction: parseFloat(document.getElementById('m_corr_hot').value) || 0,
            cold_correction: parseFloat(document.getElementById('m_corr_cold').value) || 0,
            electricity_correction: parseFloat(document.getElementById('m_corr_elect').value) || 0,
            sewage_correction: parseFloat(document.getElementById('m_corr_sewage').value) || 0
        };
        setLoading(btn, true, 'Сохранение...');
        try {
            const res = await api.post(`/admin/approve/${id}`, data);
            toast(`Утверждено! Сумма: ${Number(res.new_total).toFixed(2)} ₽`, 'success');
            document.getElementById('approveModal').classList.remove('open');
            this.table.refresh();
        } catch (e) {
            toast('Ошибка: ' + e.message, 'error');
        } finally {
            setLoading(btn, false);
        }
    },

    async openAdjustmentModal(userId, username) {
        const amountStr = await showPrompt(`Корректировка: ${username}`, 'Введите сумму (например -500 для скидки или 1000 для долга):');
        if (!amountStr) return;
        const amount = parseFloat(amountStr);
        if (isNaN(amount)) {
            toast('Нужно ввести число!', 'error');
            return;
        }
        const desc = await showPrompt('Причина', 'Укажите основание (например: перерасчет):', 'Перерасчет');
        if (!desc) return;
        try {
            await api.post('/admin/adjustments', { user_id: userId, amount, description: desc });
            toast('Корректировка сохранена', 'success');
            this.table.refresh();
        } catch (e) {
            toast(e.message, 'error');
        }
    },

    async bulkApprove() {
        if (!confirm('Вы уверены? Это утвердит ВСЕ текущие черновики без ошибок.')) return;
        setLoading(this.dom.btnBulk, true);
        try {
            const res = await api.post('/admin/approve-bulk', {});
            toast(`Утверждено записей: ${res.approved_count}`, 'success');
            this.table.refresh();
        } catch (e) {
            toast(e.message, 'error');
        } finally {
            setLoading(this.dom.btnBulk, false);
        }
    },

    async closePeriodAction() {
        if (!confirm('Закрыть месяц? Будет произведен авто-расчет для должников.')) return;
        setLoading(this.dom.btnClosePeriod, true);
        try {
            const res = await api.post('/admin/periods/close', {});
            toast(`Месяц закрыт. Авто-расчетов: ${res.auto_generated}`, 'success');
            setTimeout(() => window.location.reload(), 1500);
        } catch (e) {
            toast(e.message, 'error');
            setLoading(this.dom.btnClosePeriod, false);
        }
    },

    async openPeriodAction() {
        const name = this.dom.periodNameInput.value.trim();
        if (!name) {
            toast('Введите название месяца!', 'info');
            return;
        }
        setLoading(this.dom.btnOpenPeriod, true);
        try {
            await api.post('/admin/periods/open', { name });
            toast(`Период "${name}" открыт`, 'success');
            setTimeout(() => window.location.reload(), 1500);
        } catch (e) {
            toast(e.message, 'error');
            setLoading(this.dom.btnOpenPeriod, false);
        }
    }
};