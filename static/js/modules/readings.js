// static/js/modules/readings.js
import { api } from '../core/api.js';
import { el, clear, setLoading } from '../core/dom.js';
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
    // ============================================================
    // ИНИЦИАЛИЗАЦИЯ
    // ============================================================
    init() {
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
        const btnClosePeriod = document.querySelector('button[onclick="closePeriodAction()"]');
        // Обратите внимание: мы ищем по селектору, если ID не задан, или меняем HTML.
        // Но лучше, если в HTML у кнопок периода есть ID. Предположим, мы их добавим или найдем через DOM.
        // Для совместимости с текущим HTML найдем кнопки внутри блоков.

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

        // --- Первичная загрузка ---
        this.loadActivePeriod();
        this.load();
    },

    // ============================================================
    // ЗАГРУЗКА И ОТОБРАЖЕНИЕ СПИСКА
    // ============================================================
    async load(page = store.state.pagination.page, anomaliesOnly = null) {
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

            const data = await api.get(query);

            // Сохраняем данные в Store
            store.setReadings(data);
            store.setPage(page);

            // Рендер
            this.renderTable(data);
            this.updatePagination(data.length);

        } catch (error) {
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
                // 7. Действия
                el('td', {},
                    el('button', {
                        class: 'action-btn',
                        style: { padding: '5px 15px', fontSize: '13px', margin: '0', background: '#4a90e2' },
                        onclick: () => this.openModal(r.id)
                    }, '📝 Проверить')
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
        // Если пришло меньше элементов, чем размер страницы, значит дальше данных нет
        if (btnNext) btnNext.disabled = itemsCount < store.state.pagination.limit;
    },

    changePage(delta) {
        const newPage = store.state.pagination.page + delta;
        if (newPage > 0) this.load(newPage);
    },

    // ============================================================
    // МОДАЛЬНОЕ ОКНО (ПРОВЕРКА И КОРРЕКЦИЯ)
    // ============================================================
    openModal(id) {
        const reading = store.getReadingById(id);
        if (!reading) return;

        // Заполняем скрытый ID и имя
        document.getElementById('modal_reading_id').value = id;
        document.getElementById('m_username').textContent = reading.username;

        // Рассчитываем дельту (сколько набежало)
        // ВАЖНО: Приводим к Number для корректной математики
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

            alert(`Показания утверждены!\nНовая сумма к оплате: ${Number(res.new_total).toFixed(2)} руб.`);
            this.closeModal();
            this.load(); // Перезагружаем текущую страницу таблицы
        } catch (e) {
            alert('Ошибка при утверждении: ' + e.message);
        } finally {
            setLoading(btn, false);
        }
    },

    // ============================================================
    // МАССОВЫЕ ОПЕРАЦИИ
    // ============================================================
    async bulkApprove() {
        if (!confirm("ВНИМАНИЕ!\n\nЭто автоматически утвердит все черновики текущего месяца, где показания больше предыдущих.\nРучные коррекции не будут применены.\n\nПродолжить?")) {
            return;
        }

        const btn = document.getElementById('btnBulkApprove');
        setLoading(btn, true, 'Обработка...');

        try {
            // Эндпоинт для массового утверждения должен быть реализован на бэкенде
            // Если его нет, этот вызов вернет 404 или ошибку.
            const res = await api.post('/admin/approve-bulk', {});

            alert(`Успешно утверждено записей: ${res.approved_count}`);
            this.load(1); // Перезагружаем с первой страницы
        } catch (e) {
            alert("Ошибка при массовом утверждении: " + e.message);
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
                // Период активен
                if (activeDiv) activeDiv.style.display = 'flex';
                if (closedDiv) closedDiv.style.display = 'none';
                if (label) label.textContent = data.name;
            } else {
                // Периода нет (закрыт)
                if (activeDiv) activeDiv.style.display = 'none';
                if (closedDiv) closedDiv.style.display = 'flex';
            }
        } catch (e) {
            console.error("Ошибка проверки периода:", e);
        }
    },

    async closePeriodAction(btnElement) {
        if (!confirm(`ВНИМАНИЕ!\n\nВы закрываете текущий месяц.\n\n1. Прием показаний остановится.\n2. Должникам будет начислено "по среднему".\n3. Все черновики утвердятся.\n\nПродолжить?`)) {
            return;
        }

        setLoading(btnElement, true, 'Закрытие...');

        try {
            const res = await api.post('/admin/periods/close', {});
            alert(`Месяц успешно закрыт!\nАвто-показаний создано: ${res.auto_generated}`);
            window.location.reload(); // Перезагружаем страницу полностью, чтобы обновить все состояния
        } catch (e) {
            alert("Ошибка закрытия периода: " + e.message);
            setLoading(btnElement, false);
        }
    },

    async openPeriodAction(btnElement) {
        const nameInput = document.getElementById('newPeriodNameInput');
        const newName = nameInput ? nameInput.value.trim() : null;

        if (!newName) {
            alert("Пожалуйста, введите название месяца (например: 'Март 2026')");
            return;
        }

        setLoading(btnElement, true, 'Открытие...');

        try {
            await api.post('/admin/periods/open', { name: newName });
            alert(`Новый месяц "${newName}" успешно открыт!\nПользователи могут подавать показания.`);
            window.location.reload(); // Перезагружаем страницу полностью
        } catch (e) {
            alert("Ошибка открытия периода: " + e.message);
            setLoading(btnElement, false);
        }
    }
};