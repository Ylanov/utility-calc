// static/js/modules/readings.js
import { api } from '../core/api.js';
import { el, clear, setLoading, toast } from '../core/dom.js';
import { store } from '../core/store.js';

// Конфигурация цветов и текстов для аномалий
const ANOMALY_MAP = {
    "NEGATIVE_HOT": { text: "ГВС<0", color: "#e74c3c", title: "Ошибка: Показания ГВС меньше предыдущих!" },
    "NEGATIVE_COLD": { text: "ХВС<0", color: "#e74c3c", title: "Ошибка: Показания ХВС меньше предыдущих!" },
    "NEGATIVE_ELECT": { text: "Свет<0", color: "#e74c3c", title: "Ошибка: Показания Света меньше предыдущих!" },
    "HIGH_VS_PEERS_HOT": { text: "ГВС Peers↑", color: "#9b59b6", title: "Расход ГВС значительно выше среднего по общежитию" },
    "HIGH_VS_PEERS_COLD": { text: "ХВС Peers↑", color: "#9b59b6", title: "Расход ХВС значительно выше среднего по общежитию" },
    "HIGH_VS_PEERS_ELECT": { text: "Свет Peers↑", color: "#9b59b6", title: "Расход Света значительно выше среднего по общежитию" },
    "HIGH_HOT": { text: "ГВС↑", color: "#e74c3c", title: "Очень высокий расход горячей воды" },
    "HIGH_COLD": { text: "ХВС↑", color: "#e74c3c", title: "Очень высокий расход холодной воды" },
    "HIGH_ELECT": { text: "Свет↑", color: "#e74c3c", title: "Очень высокий расход электричества" },
    "ZERO_HOT": { text: "ГВС=0", color: "#f39c12", title: "Нулевой расход горячей воды" },
    "ZERO_COLD": { text: "ХВС=0", color: "#f39c12", title: "Нулевой расход холодной воды" },
    "ZERO_ELECT": { text: "Свет=0", color: "#f39c12", title: "Нулевой расход электричества" },
    "FROZEN_HOT": { text: "ГВС❄️", color: "#3498db", title: "Счетчик ГВС не менялся 3+ мес." },
    "FROZEN_COLD": { text: "ХВС❄️", color: "#3498db", title: "Счетчик ХВС не менялся 3+ мес." },
    "FROZEN_ELECT": { text: "Свет❄️", color: "#3498db", title: "Счетчик света не менялся 3+ мес." }
};

export const ReadingsModule = {
    isInitialized: false,
    controller: null, // Для хранения AbortController для отмены старых запросов

    // ============================================================
    // ИНИЦИАЛИЗАЦИЯ
    // ============================================================
    init() {
        if (!this.isInitialized) {
            this.setupEventListeners();
            this.isInitialized = true;
        }

        // Первичная загрузка
        this.loadActivePeriod();
        this.load();
    },

    setupEventListeners() {
        console.log('ReadingsModule: Event listeners setup.');

        // --- Навигация (Пагинация) ---
        const btnPrev = document.getElementById('btnPrev');
        const btnNext = document.getElementById('btnNext');
        if (btnPrev) btnPrev.addEventListener('click', () => this.changePage(-1));
        if (btnNext) btnNext.addEventListener('click', () => this.changePage(1));

        // --- Основные действия ---
        const btnRefresh = document.getElementById('btnRefreshReadings');
        if (btnRefresh) btnRefresh.addEventListener('click', () => this.load());

        const btnBulk = document.getElementById('btnBulkApprove');
        if (btnBulk) btnBulk.addEventListener('click', () => this.bulkApprove());

        // --- Фильтры ---
        const filterCheck = document.getElementById('filterAnomalies');
        if (filterCheck) {
            filterCheck.addEventListener('change', (e) => {
                // При включении фильтра сбрасываем на 1 страницу
                this.load(1, e.target.checked);
            });
        }

        // --- Модальное окно (Approve) ---
        const btnModalClose = document.getElementById('btnModalClose');
        const btnModalSubmit = document.getElementById('btnModalSubmit');
        if (btnModalClose) btnModalClose.addEventListener('click', () => this.closeModal());
        if (btnModalSubmit) btnModalSubmit.addEventListener('click', () => this.submitApproval());

        // --- Управление периодом (закрытие/открытие месяца) ---
        const periodActiveBlock = document.getElementById('periodActiveState');
        if (periodActiveBlock) {
            const closeBtn = periodActiveBlock.querySelector('button');
            if (closeBtn) closeBtn.addEventListener('click', () => this.closePeriodAction(closeBtn));
        }

        const periodClosedBlock = document.getElementById('periodClosedState');
        if (periodClosedBlock) {
            const openBtn = periodClosedBlock.querySelector('button');
            if (openBtn) openBtn.addEventListener('click', () => this.openPeriodAction(openBtn));
        }
    },

    // ============================================================
    // ЗАГРУЗКА И ОТОБРАЖЕНИЕ СПИСКА
    // ============================================================
    async load(page = store.state.pagination.page, anomaliesOnly = null) {
        // 1. ОТМЕНА ПРЕДЫДУЩЕГО ЗАПРОСА
        if (this.controller) {
            this.controller.abort();
        }
        this.controller = new AbortController();
        const signal = this.controller.signal;

        const tbody = clear('readingsTableBody');

        // Индикатор загрузки
        tbody.appendChild(el('tr', {},
            el('td', { colspan: 7, style: { textAlign: 'center', padding: '20px', color: '#666' } }, 'Загрузка данных...')
        ));

        // Определяем состояние фильтра
        if (anomaliesOnly === null) {
            const checkbox = document.getElementById('filterAnomalies');
            anomaliesOnly = checkbox ? checkbox.checked : false;
        }

        try {
            const limit = store.state.pagination.limit;
            const query = `/admin/readings?page=${page}&limit=${limit}${anomaliesOnly ? '&anomalies_only=true' : ''}`;

            // Передаем signal в API (api.js прокидывает options в fetch)
            const data = await api.get(query, { signal });

            // Сохраняем данные в Store
            store.setReadings(data);
            store.setPage(page);

            // Рендер
            this.renderTable(data);
            this.updatePagination(data.length);

        } catch (error) {
            // Если ошибка вызвана отменой запроса — игнорируем её
            if (error.name === 'AbortError') {
                return;
            }

            tbody.innerHTML = '';
            tbody.appendChild(el('tr', {},
                el('td', { colspan: 7, style: { color: 'red', textAlign: 'center', padding: '20px' } }, `Ошибка: ${error.message}`)
            ));
        }
    },

    renderTable(readings) {
        const tbody = clear('readingsTableBody');

        if (!readings || readings.length === 0) {
            const isFiltered = document.getElementById('filterAnomalies')?.checked;
            const msg = isFiltered ? "Нет подозрительных показаний" : "Нет данных на этой странице";

            tbody.appendChild(el('tr', {},
                el('td', { colspan: 7, style: { textAlign: 'center', padding: '20px' } }, msg)
            ));
            return;
        }

        readings.forEach(r => {
            const tr = el('tr', {},
                // 1. Жилец
                el('td', {},
                    el('strong', {}, r.username),
                    el('div', { style: { fontSize: '11px', color: '#888' } }, r.dormitory || '')
                ),
                // 2. Статус (Аномалии)
                el('td', {}, this.createAnomalyBadges(r.anomaly_flags)),
                // 3. ГВС
                el('td', {}, r.cur_hot),
                // 4. ХВС
                el('td', {}, r.cur_cold),
                // 5. Свет
                el('td', {}, r.cur_elect),
                // 6. Сумма
                el('td', { style: { color: 'green', fontWeight: 'bold' } }, `~ ${Number(r.total_cost).toFixed(2)} ₽`),
                // 7. Действия (Обновленная структура кнопок)
                el('td', {},
                    el('div', { class: 'controls-group', style: { justifyContent: 'flex-start', gap: '5px' } },
                        // Кнопка Корректировки
                        el('button', {
                            class: 'btn-icon btn-adjust',
                            title: 'Добавить фин. корректировку',
                            onclick: () => this.openAdjustmentModal(r.user_id, r.username)
                        }, '±'),

                        // Кнопка Проверки
                        el('button', {
                            class: 'btn-icon btn-check',
                            title: 'Проверить и утвердить',
                            onclick: () => this.openModal(r.id)
                        }, '📝')
                    )
                )
            );
            tbody.appendChild(tr);
        });
    },

    createAnomalyBadges(flags) {
        if (!flags) return el('span', { style: { color: '#27ae60', fontWeight: 'bold' } }, 'OK');

        const container = document.createDocumentFragment();

        flags.split(',').forEach(flag => {
            const meta = ANOMALY_MAP[flag] || { text: flag, color: '#95a5a6', title: 'Неизвестно' };

            const badge = el('span', {
                title: meta.title,
                style: {
                    display: 'inline-block',
                    background: meta.color,
                    color: 'white',
                    padding: '2px 5px',
                    borderRadius: '3px',
                    fontSize: '10px',
                    margin: '1px',
                    fontWeight: 'bold',
                    cursor: 'help'
                }
            }, meta.text);

            container.appendChild(badge);
            container.appendChild(document.createTextNode(' '));
        });

        return container;
    },

    updatePagination(itemsCount) {
        const pageInd = document.getElementById('pageIndicator');
        if (pageInd) pageInd.textContent = `Стр. ${store.state.pagination.page}`;

        const btnPrev = document.getElementById('btnPrev');
        const btnNext = document.getElementById('btnNext');

        if (btnPrev) btnPrev.disabled = store.state.pagination.page <= 1;
        if (btnNext) btnNext.disabled = itemsCount < store.state.pagination.limit;
    },

    changePage(delta) {
        const newPage = store.state.pagination.page + delta;
        if (newPage > 0) this.load(newPage);
    },

    // ============================================================
    // МОДАЛЬНОЕ ОКНО (ПРОВЕРКА И КОРРЕКЦИЯ ОБЪЕМОВ)
    // ============================================================
    openModal(id) {
        const reading = store.getReadingById(id);
        if (!reading) return;

        // Заполняем скрытый ID и имя
        document.getElementById('modal_reading_id').value = id;
        document.getElementById('m_username').textContent = reading.username;

        // Рассчитываем дельту
        const dHot = (Number(reading.cur_hot) - Number(reading.prev_hot)).toFixed(3);
        const dCold = (Number(reading.cur_cold) - Number(reading.prev_cold)).toFixed(3);
        const dElect = (Number(reading.cur_elect) - Number(reading.prev_elect)).toFixed(3);

        document.getElementById('m_hot_usage').textContent = dHot;
        document.getElementById('m_cold_usage').textContent = dCold;
        document.getElementById('m_elect_usage').textContent = dElect;

        // Сбрасываем поля коррекции в 0
        ['m_corr_hot', 'm_corr_cold', 'm_corr_elect', 'm_corr_sewage'].forEach(inputId => {
            const input = document.getElementById(inputId);
            if (input) input.value = 0;
        });

        document.getElementById('approveModal').classList.add('open');
    },

    closeModal() {
        document.getElementById('approveModal').classList.remove('open');
    },

    async submitApproval() {
        const id = document.getElementById('modal_reading_id').value;
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
            toast(`Показания утверждены! Новая сумма: ${Number(res.new_total).toFixed(2)} ₽`, 'success');
            this.closeModal();
            this.load();
        } catch (e) {
            toast('Ошибка при утверждении: ' + e.message, 'error');
        } finally {
            setLoading(btn, false);
        }
    },

    // ============================================================
    // ФИНАНСОВЫЕ КОРРЕКТИРОВКИ
    // ============================================================
    openAdjustmentModal(userId, username) {
        // Здесь prompt пока оставим, так как создание модалки "на лету" требует больше кода HTML
        const amountStr = prompt(`Добавить корректировку для ${username}.\n\nВведите сумму:\n(например: -500 для скидки или 1000 для доплаты)`);
        if (!amountStr) return;

        const amount = parseFloat(amountStr);
        if (isNaN(amount)) {
            toast("Некорректная сумма", 'error');
            return;
        }

        const desc = prompt("Введите причину (например: Перерасчет за отсутствие):");
        if (!desc) return;

        this.sendAdjustment(userId, amount, desc);
    },

    async sendAdjustment(userId, amount, description) {
        try {
            await api.post('/admin/adjustments', {
                user_id: userId,
                amount: amount,
                description: description
            });
            toast("Корректировка успешно добавлена!", 'success');
            this.load();
        } catch (e) {
            toast("Ошибка при добавлении корректировки: " + e.message, 'error');
        }
    },

    // ============================================================
    // МАССОВЫЕ ОПЕРАЦИИ
    // ============================================================
    async bulkApprove() {
        if (!confirm("ВНИМАНИЕ!\n\nЭто автоматически утвердит все черновики текущего месяца.\nПродолжить?")) {
            return;
        }

        const btn = document.getElementById('btnBulkApprove');
        setLoading(btn, true, 'Обработка...');

        try {
            const res = await api.post('/admin/approve-bulk', {});
            toast(`Успешно утверждено записей: ${res.approved_count}`, 'success');
            this.load(1);
        } catch (e) {
            toast("Ошибка: " + e.message, 'error');
        } finally {
            setLoading(btn, false);
        }
    },

    // ============================================================
    // УПРАВЛЕНИЕ ПЕРИОДАМИ
    // ============================================================
    async loadActivePeriod() {
        const activeDiv = document.getElementById('periodActiveState');
        const closedDiv = document.getElementById('periodClosedState');
        const label = document.getElementById('activePeriodLabel');

        try {
            const data = await api.get('/admin/periods/active');

            if (data && data.name) {
                if (activeDiv) activeDiv.style.display = 'flex';
                if (closedDiv) closedDiv.style.display = 'none';
                if (label) label.textContent = data.name;
            } else {
                if (activeDiv) activeDiv.style.display = 'none';
                if (closedDiv) closedDiv.style.display = 'flex';
            }
        } catch (e) {
            console.error("Ошибка проверки периода:", e);
        }
    },

    async closePeriodAction(btnElement) {
        if (!confirm(`ВНИМАНИЕ!\n\nВы закрываете текущий месяц.\nАвто-расчет для должников будет выполнен.\n\nПродолжить?`)) {
            return;
        }

        setLoading(btnElement, true, 'Закрытие...');

        try {
            const res = await api.post('/admin/periods/close', {});
            toast(`Месяц закрыт! Авто-показаний: ${res.auto_generated}`, 'success');

            // Даем время на чтение тоста перед перезагрузкой
            setTimeout(() => window.location.reload(), 1500);
        } catch (e) {
            toast("Ошибка закрытия периода: " + e.message, 'error');
            setLoading(btnElement, false);
        }
    },

    async openPeriodAction(btnElement) {
        const nameInput = document.getElementById('newPeriodNameInput');
        const newName = nameInput ? nameInput.value.trim() : null;

        if (!newName) {
            toast("Введите название месяца", 'info');
            return;
        }

        setLoading(btnElement, true, 'Открытие...');

        try {
            await api.post('/admin/periods/open', { name: newName });
            toast(`Новый месяц "${newName}" открыт!`, 'success');

            setTimeout(() => window.location.reload(), 1500);
        } catch (e) {
            toast("Ошибка открытия периода: " + e.message, 'error');
            setLoading(btnElement, false);
        }
    }
};