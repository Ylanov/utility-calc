// static/js/modules/client-dashboard.js
import { api } from '../core/api.js';
import { el, clear, toast } from '../core/dom.js'; // Используем глобальный toast

/**
 * ClientDashboard - Модуль, управляющий всем интерфейсом
 * личного кабинета жильца.
 */
export const ClientDashboard = {
    isInitialized: false,

    // Внутреннее состояние модуля для хранения данных
    state: {
        lastReadings: { hot: 0, cold: 0, elect: 0 }
    },

    /**
     * Инициализация модуля.
     */
    init() {
        if (this.isInitialized) return;
        this.isInitialized = true;

        this.setupEventListeners();

        // Запускаем асинхронную загрузку всех необходимых данных
        this.loadAllData();
    },

    setupEventListeners() {
        // Привязываем обработчик к форме отправки показаний
        const form = document.getElementById('meterForm');
        if (form) form.addEventListener('submit', (e) => this.submit(e));

        // Добавляем валидацию "на лету" при вводе данных в поля
        ['hotWater', 'coldWater', 'electricity'].forEach(id => {
            const input = document.getElementById(id);
            if (input) input.addEventListener('input', () => this.validateInputs());
        });
    },

    /**
     * Главная функция для загрузки всех данных страницы.
     */
    async loadAllData() {
        try {
            await Promise.all([
                this.loadUserProfile(),     // Загрузка данных профиля (имя, адрес)
                this.loadInitialState(),    // Загрузка состояния счетчиков и периода
                this.loadHistory()          // Загрузка истории начислений
            ]);

            // После загрузки всех данных убираем "экран загрузки" (прозрачность)
            const appContainer = document.getElementById('app-container');
            if (appContainer) appContainer.classList.remove('opacity-0');
        } catch (e) {
            console.error(e);
            toast("Ошибка инициализации кабинета", "error");
        }
    },

    // ============================================================
    // ЗАГРУЗКА ДАННЫХ
    // ============================================================

    /**
     * Загружает и отображает данные профиля пользователя.
     */
    async loadUserProfile() {
        try {
            const user = await api.get('/users/me');

            document.getElementById('pUser').textContent = user.username ?? 'Неизвестно';
            document.getElementById('pAddress').textContent = user.dormitory ?? 'Не указано';
            document.getElementById('pArea').textContent = `${user.apartment_area ?? 0} м²`;
            document.getElementById('pResidents').textContent = user.residents_count ?? 1;

        } catch (error) {
            console.warn("Could not load user profile:", error.message);
            document.getElementById('pUser').textContent = '-';
            document.getElementById('pAddress').textContent = '-';
            document.getElementById('pArea').textContent = '-';
            document.getElementById('pResidents').textContent = '-';
        }
    },

    /**
     * Загружает основное состояние: показания, статус периода, расчеты.
     */
    async loadInitialState() {
        try {
            const stateData = await api.get('/readings/state');

            this.updateStatus(stateData);
            this.updateMeters(stateData);
            this.updateResults(stateData);

            document.getElementById('pPeriod').textContent = stateData.period_name || 'Прием закрыт';

        } catch (error) {
            toast(error.message, "error");
        }
    },

    /**
     * Загружает и отображает историю начислений.
     */
    async loadHistory() {
        const tbody = clear('historyBody');
        try {
            const historyData = await api.get('/readings/history');

            if (!historyData || historyData.length === 0) {
                tbody.appendChild(el('tr', {},
                    el('td', { colspan: "6", class: "text-center p-4 text-gray-500" }, "История начислений пуста.")
                ));
                return;
            }

            historyData.forEach(r => {
                const tr = el('tr', { class: 'hover:bg-gray-50' },
                    el('td', { class: 'border p-2 font-semibold' }, r.period),
                    el('td', { class: 'border p-2 text-center' }, Number(r.hot).toFixed(2)),
                    el('td', { class: 'border p-2 text-center' }, Number(r.cold).toFixed(2)),
                    el('td', { class: 'border p-2 text-center' }, Number(r.electric).toFixed(2)),
                    el('td', { class: 'border p-2 text-center font-bold text-green-800' }, Number(r.total).toFixed(2)),
                    el('td', { class: 'border p-2 text-center' },
                         el('button', {
                            class: 'text-blue-600 hover:underline text-2xl',
                            title: 'Скачать квитанцию',
                            onclick: () => this.downloadReceipt(r.id)
                        }, '📄')
                    )
                );
                tbody.appendChild(tr);
            });

        } catch (error) {
            console.warn("History not loaded:", error);
            tbody.appendChild(el('tr', {},
                el('td', { colspan: "6", class: "text-center p-4 text-gray-400" }, "Не удалось загрузить историю.")
            ));
        }
    },

    // ============================================================
    // ОБНОВЛЕНИЕ UI
    // ============================================================

    updateStatus(data) {
        const statusArea = clear('statusArea');
        const fieldset = document.getElementById('meterFieldset');
        let statusBlock;

        if (!data.is_period_open) {
            statusBlock = el('div', { class: "bg-blue-100 border-l-4 border-blue-500 text-blue-700 p-4 rounded-md" },
                el('p', { class: "font-bold" }, "🔒 Расчетный период закрыт"),
                el('p', {}, "Подача показаний в этом месяце завершена.")
            );
            fieldset.disabled = true;
        } else if (data.is_draft) {
            statusBlock = el('div', { class: "bg-yellow-100 border-l-4 border-yellow-500 text-yellow-700 p-4 rounded-md" },
                el('p', { class: "font-bold" }, "✏️ Черновик на проверке"),
                el('p', {}, "Ваши показания сохранены. Вы можете изменить их до закрытия периода.")
            );
            fieldset.disabled = false;
        } else {
            statusBlock = el('div', { class: "bg-green-100 border-l-4 border-green-500 text-green-700 p-4 rounded-md" },
                el('p', { class: "font-bold" }, "🟢 Сбор показаний открыт"),
                el('p', {}, "Пожалуйста, внесите текущие показания счетчиков.")
            );
            fieldset.disabled = false;
        }
        statusArea.appendChild(statusBlock);
    },

    updateMeters(data) {
        this.state.lastReadings = { hot: data.prev_hot, cold: data.prev_cold, elect: data.prev_elect };

        document.getElementById('prevHot').textContent = Number(data.prev_hot).toFixed(2);
        document.getElementById('prevCold').textContent = Number(data.prev_cold).toFixed(2);
        document.getElementById('prevElect').textContent = Number(data.prev_elect).toFixed(2);

        if (data.is_draft) {
            document.getElementById('hotWater').value = data.current_hot || '';
            document.getElementById('coldWater').value = data.current_cold || '';
            document.getElementById('electricity').value = data.current_elect || '';
        }
    },

    updateResults(data) {
        const resultDiv = document.getElementById('result');
        if (!data.total_cost && data.total_cost !== 0) {
            resultDiv.classList.add('hidden');
            return;
        }
        resultDiv.classList.remove('hidden');

        const fmt = (val) => `${Number(val || 0).toFixed(2)} ₽`;

        document.getElementById('rHot').textContent = fmt(data.cost_hot_water);
        document.getElementById('rCold').textContent = fmt(data.cost_cold_water);
        document.getElementById('rSew').textContent = fmt(data.cost_sewage);
        document.getElementById('rEl').textContent = fmt(data.cost_electricity);
        document.getElementById('rMain').textContent = fmt(data.cost_maintenance);
        document.getElementById('rRent').textContent = fmt(data.cost_social_rent);
        document.getElementById('rWaste').textContent = fmt(data.cost_waste);
        document.getElementById('rFix').textContent = fmt(data.cost_fixed_part);
        document.getElementById('rTotal').textContent = fmt(data.total_cost);
    },

    // ============================================================
    // ЛОГИКА ФОРМЫ И ДЕЙСТВИЙ
    // ============================================================

    validateInputs() {
        let isFormValid = true;
        const inputs = [
            { id: 'hotWater', prev: this.state.lastReadings.hot, errorId: 'hotError' },
            { id: 'coldWater', prev: this.state.lastReadings.cold, errorId: 'coldError' },
            { id: 'electricity', prev: this.state.lastReadings.elect, errorId: 'electError' }
        ];

        inputs.forEach(item => {
            const inputEl = document.getElementById(item.id);
            const errorEl = document.getElementById(item.errorId);
            const val = parseFloat(inputEl.value);

            if (inputEl.value && val < item.prev) {
                inputEl.classList.add('input-error');
                errorEl.textContent = `Меньше пред. (${item.prev})`;
                isFormValid = false;
            } else {
                inputEl.classList.remove('input-error');
                errorEl.textContent = '';
            }
        });

        document.getElementById('submitBtn').disabled = !isFormValid;
        return isFormValid;
    },

    async submit(e) {
        e.preventDefault();
        if (!this.validateInputs()) return;

        const btn = document.getElementById('submitBtn');
        const spinner = document.getElementById('submitBtnSpinner');
        const btnText = document.getElementById('submitBtnText');

        btn.disabled = true;
        btnText.textContent = 'Сохранение...';
        spinner.classList.remove('hidden');

        const data = {
            hot_water: parseFloat(document.getElementById('hotWater').value),
            cold_water: parseFloat(document.getElementById('coldWater').value),
            electricity: parseFloat(document.getElementById('electricity').value)
        };

        try {
            await api.post('/calculate', data);
            toast('Показания успешно сохранены!', 'success');
            await this.loadInitialState();
        } catch (error) {
            toast(error.message, 'error');
        } finally {
            btn.disabled = false;
            btnText.textContent = '💾 Сохранить';
            spinner.classList.add('hidden');
        }
    },

    async downloadReceipt(id) {
        try {
            // Используем безопасный эндпоинт для клиента
            await api.download(`/client/receipts/${id}`, `receipt_${id}.pdf`);
            toast("Квитанция скачивается", "success");
        } catch (e) {
            toast('Ошибка скачивания: ' + e.message, 'error');
        }
    }
};