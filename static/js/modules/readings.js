// static/js/modules/readings.js
import { api } from '../core/api.js';
import { el, clear, setLoading, toast, showPrompt } from '../core/dom.js';

// Карта цветов для аномалий
const ANOMALY_MAP = {
    "NEGATIVE": { color: "#c0392b", label: "Ошибка (<0)" },
    "ZERO": { color: "#f39c12", label: "Нулевой" },
    "HIGH": { color: "#e74c3c", label: "Высокий" },
    "FROZEN": { color: "#3498db", label: "Замерзший" },
    "PEERS": { color: "#9b59b6", label: "Аномалия (Группа)" }
};

export const ReadingsModule = {
    state: {
        page: 1,
        limit: 50,
        anomaliesOnly: false,
        isLoading: false
    },

    init() {
        this.cacheDOM();
        this.bindEvents();
        this.load();
    },

    cacheDOM() {
        this.dom = {
            tbody: document.getElementById('readingsTableBody'),
            btnRefresh: document.getElementById('btnRefreshReadings'),
            btnBulk: document.getElementById('btnBulkApprove'),
            btnPrev: document.getElementById('btnPrev'),
            btnNext: document.getElementById('btnNext'),
            pageIndicator: document.getElementById('pageIndicator'),
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
        if (this.dom.btnRefresh) this.dom.btnRefresh.addEventListener('click', () => this.load());
        if (this.dom.btnBulk) this.dom.btnBulk.addEventListener('click', () => this.bulkApprove());

        if (this.dom.btnPrev) this.dom.btnPrev.addEventListener('click', () => this.changePage(-1));
        if (this.dom.btnNext) this.dom.btnNext.addEventListener('click', () => this.changePage(1));

        if (this.dom.filterCheckbox) {
            this.dom.filterCheckbox.addEventListener('change', (e) => {
                this.state.anomaliesOnly = e.target.checked;
                this.state.page = 1;
                this.load();
            });
        }

        // Управление периодами
        if (this.dom.btnClosePeriod) {
            this.dom.btnClosePeriod.addEventListener('click', () => this.closePeriodAction());
        }
        if (this.dom.btnOpenPeriod) {
            this.dom.btnOpenPeriod.addEventListener('click', () => this.openPeriodAction());
        }
    },

    async load() {
        if (this.state.isLoading) return;
        this.state.isLoading = true;

        // Показываем спиннер в таблице
        this.dom.tbody.innerHTML = '<tr><td colspan="7" style="text-align:center; padding:20px;">Загрузка данных...</td></tr>';

        try {
            // Параллельно загружаем статус периода и таблицу
            await Promise.all([
                this.loadActivePeriod(),
                this.loadTableData()
            ]);
        } catch (error) {
            this.dom.tbody.innerHTML = `<tr><td colspan="7" style="color:red; text-align:center; padding:20px;">Ошибка: ${error.message}</td></tr>`;
        } finally {
            this.state.isLoading = false;
        }
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
        } catch (e) {
            console.warn("Ошибка проверки периода", e);
        }
    },

    async loadTableData() {
        const query = `/admin/readings?page=${this.state.page}&limit=${this.state.limit}${this.state.anomaliesOnly ? '&anomalies_only=true' : ''}`;
        const data = await api.get(query);

        this.renderTable(data);
        this.updatePagination(data.length);
    },

    renderTable(readings) {
        this.dom.tbody.innerHTML = '';

        if (!readings || readings.length === 0) {
            this.dom.tbody.appendChild(el('tr', {},
                el('td', { colspan: 7, style: { textAlign: 'center', padding: '20px', color: '#777' } },
                    this.state.anomaliesOnly ? "Нет подозрительных показаний" : "Список пуст"
                )
            ));
            return;
        }

        // 🚀 Optimization: Используем Fragment для вставки
        const fragment = document.createDocumentFragment();

        readings.forEach(r => {
            const tr = el('tr', {},
                // 1. Жилец
                el('td', {},
                    el('div', { style: { fontWeight: '600' } }, r.username),
                    el('div', { style: { fontSize: '11px', color: '#888' } }, r.dormitory || 'Общ. не указано')
                ),
                // 2. Статус (Баджи)
                el('td', {}, this.createBadges(r.anomaly_flags)),
                // 3. Показания
                el('td', {}, Number(r.cur_hot).toFixed(3)),
                el('td', {}, Number(r.cur_cold).toFixed(3)),
                el('td', {}, Number(r.cur_elect).toFixed(3)),
                // 4. Деньги
                el('td', { style: { color: '#27ae60', fontWeight: 'bold' } },
                    `${Number(r.total_cost).toFixed(2)} ₽`
                ),
                // 5. Действия
                el('td', {},
                    el('button', {
                        class: 'btn-icon btn-adjust',
                        title: 'Финансовая корректировка',
                        onclick: () => this.openAdjustmentModal(r.user_id, r.username)
                    }, '±'),
                    el('button', {
                        class: 'btn-icon btn-check',
                        title: 'Проверить и утвердить',
                        onclick: () => this.openApproveModal(r)
                    }, '✓')
                )
            );
            fragment.appendChild(tr);
        });

        this.dom.tbody.appendChild(fragment);
    },

    createBadges(flags) {
        if (!flags) return el('span', { style: { color: '#ccc' } }, '-');

        const container = el('div', { style: { display: 'flex', gap: '4px', flexWrap: 'wrap' } });

        flags.split(',').forEach(flag => {
            // Ищем совпадение по части ключа (например NEGATIVE_HOT -> NEGATIVE)
            let type = "UNKNOWN";
            for (const key in ANOMALY_MAP) {
                if (flag.includes(key)) type = key;
            }

            const meta = ANOMALY_MAP[type] || { color: '#95a5a6', label: flag };

            container.appendChild(el('span', {
                title: flag,
                style: {
                    background: meta.color,
                    color: 'white',
                    padding: '2px 6px',
                    borderRadius: '4px',
                    fontSize: '10px',
                    fontWeight: 'bold',
                    cursor: 'help'
                }
            }, meta.label));
        });

        return container;
    },

    updatePagination(itemsCount) {
        if (this.dom.pageIndicator) {
            this.dom.pageIndicator.textContent = `Стр. ${this.state.page}`;
        }
        if (this.dom.btnPrev) {
            this.dom.btnPrev.disabled = this.state.page <= 1;
        }
        if (this.dom.btnNext) {
            // Если вернулось меньше лимита, значит это последняя страница
            this.dom.btnNext.disabled = itemsCount < this.state.limit;
        }
    },

    changePage(delta) {
        const newPage = this.state.page + delta;
        if (newPage > 0) {
            this.state.page = newPage;
            this.loadTableData();
        }
    },

    // --- МОДАЛЬНЫЕ ОКНА И ДЕЙСТВИЯ ---

    async openApproveModal(reading) {
        // Здесь мы используем существующую HTML-модалку (approveModal), так как она сложная
        // Но заполняем её данными через JS
        const modal = document.getElementById('approveModal');
        if (!modal) return;

        // Заполняем поля
        document.getElementById('modal_reading_id').value = reading.id;
        document.getElementById('m_username').textContent = reading.username;

        const dHot = (Number(reading.cur_hot) - Number(reading.prev_hot)).toFixed(3);
        const dCold = (Number(reading.cur_cold) - Number(reading.prev_cold)).toFixed(3);
        const dElect = (Number(reading.cur_elect) - Number(reading.prev_elect)).toFixed(3);

        document.getElementById('m_hot_usage').textContent = dHot;
        document.getElementById('m_cold_usage').textContent = dCold;
        document.getElementById('m_elect_usage').textContent = dElect;

        // Сброс инпутов
        ['m_corr_hot', 'm_corr_cold', 'm_corr_elect', 'm_corr_sewage'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.value = 0;
        });

        // Показываем
        modal.classList.add('open');

        // Вешаем обработчик на кнопку "Подтвердить" внутри модалки
        // (Важно удалить старый обработчик, чтобы не плодить их, или использовать onclick)
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
            this.loadTableData(); // Обновляем только таблицу
        } catch (e) {
            toast('Ошибка: ' + e.message, 'error');
        } finally {
            setLoading(btn, false);
        }
    },

    async openAdjustmentModal(userId, username) {
        // Используем нашу новую красивую модалку из dom.js
        const amountStr = await showPrompt(
            `Корректировка: ${username}`,
            'Введите сумму (например -500 для скидки или 1000 для долга):'
        );

        if (!amountStr) return;

        const amount = parseFloat(amountStr);
        if (isNaN(amount)) {
            toast('Нужно ввести число!', 'error');
            return;
        }

        const desc = await showPrompt('Причина', 'Укажите основание (например: перерасчет):', 'Перерасчет');
        if (!desc) return;

        try {
            await api.post('/admin/adjustments', {
                user_id: userId,
                amount: amount,
                description: desc
            });
            toast('Корректировка сохранена', 'success');
            this.loadTableData();
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
            this.loadTableData();
        } catch (e) {
            toast(e.message, 'error');
        } finally {
            setLoading(this.dom.btnBulk, false);
        }
    },

    // --- ПЕРИОДЫ ---

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