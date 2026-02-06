// static/js/modules/client-dashboard.js
import { api } from '../core/api.js';
import { el, clear } from '../core/dom.js';

/**
 * ClientDashboard - Модуль, управляющий всем интерфейсом
 * личного кабинета жильца.
 */
export const ClientDashboard = {
    // Внутреннее состояние модуля для хранения данных
    state: {
        lastReadings: { hot: 0, cold: 0, elect: 0 }
    },

    /**
     * Инициализация модуля. Вызывается один раз при загрузке страницы.
     */
    init() {
        // Привязываем обработчик к форме отправки показаний
        const form = document.getElementById('meterForm');
        if (form) form.addEventListener('submit', (e) => this.submit(e));

        // Добавляем валидацию "на лету" при вводе данных в поля
        ['hotWater', 'coldWater', 'electricity'].forEach(id => {
            const input = document.getElementById(id);
            if (input) input.addEventListener('input', () => this.validateInputs());
        });

        // Запускаем асинхронную загрузку всех необходимых данных
        this.loadAllData();
    },

    /**
     * Главная функция для загрузки всех данных страницы.
     */
    async loadAllData() {
        // Promise.all позволяет выполнять несколько независимых запросов одновременно,
        // что ускоряет загрузку страницы.
        await Promise.all([
            this.loadUserProfile(),     // Загрузка данных профиля (имя, адрес)
            this.loadInitialState(),    // Загрузка состояния счетчиков и периода
            this.loadHistory()          // Загрузка истории начислений
        ]);

        // После загрузки всех данных убираем "экран загрузки" (прозрачность)
        const appContainer = document.getElementById('app-container');
        if (appContainer) appContainer.classList.remove('opacity-0');
    },

    // ============================================================
    // ЗАГРУЗКА ДАННЫХ
    // ============================================================

    /**
     * Загружает и отображает данные профиля пользователя (имя, адрес и т.д.).
     * ВАЖНО: Требует наличия эндпоинта /api/users/me на бэкенде.
     */
    async loadUserProfile() {
        try {
            // Этот эндпоинт стандартен для получения информации о текущем пользователе
            const user = await api.get('/users/me');

            // Безопасно обновляем DOM через textContent
            document.getElementById('pUser').textContent = user.username || '-';
            document.getElementById('pAddress').textContent = user.dormitory || '-';
            document.getElementById('pArea').textContent = `${user.apartment_area} м²`;
            document.getElementById('pResidents').textContent = user.residents_count;
        } catch (error) {
            console.warn("Could not load user profile:", error.message);
            // Если эндпоинт не найден, просто оставляем "Загрузка..." или ставим прочерки
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
            // ИСПРАВЛЕНО: Единственный правильный вызов эндпоинта
            const stateData = await api.get('/readings/state');

            this.updateStatus(stateData);
            this.updateMeters(stateData);
            this.updateResults(stateData);

            // Также обновляем информацию о периоде в шапке
            document.getElementById('pPeriod').textContent = stateData.period_name || 'Прием закрыт';

        } catch (error) {
            this.showToast(error.message, true);
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

    /**
     * Отображает статус-бар (период открыт/закрыт/черновик).
     */
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

    /**
     * Заполняет поля счетчиков предыдущими и текущими значениями.
     */
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

    /**
     * Отображает блок с предварительным расчетом стоимости.
     */
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

    /**
     * Проверяет корректность введенных данных в полях счетчиков.
     */
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

    /**
     * Обрабатывает отправку формы с показаниями.
     */
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
            this.showToast('Показания успешно сохранены!', false);
            await this.loadInitialState(); // Перезагружаем только основное состояние
        } catch (error) {
            this.showToast(error.message, true);
        } finally {
            btn.disabled = false;
            btnText.textContent = '💾 Сохранить';
            spinner.classList.add('hidden');
        }
    },

    /**
     * Инициирует скачивание PDF-квитанции для конкретной записи.
     */
    async downloadReceipt(id) {
        try {
            // Используем безопасный эндпоинт для клиента
            await api.download(`/client/receipts/${id}`, `receipt_${id}.pdf`);
        } catch (e) {
            this.showToast('Ошибка скачивания: ' + e.message, true);
        }
    },

    // ============================================================
    // УТИЛИТЫ UI (Всплывающие уведомления)
    // ============================================================
    showToast(message, isError = false) {
        const container = document.getElementById('toast-container');
        if (!container) return;

        const toastType = isError ? 'bg-red-600' : 'bg-green-600';
        const toast = el('div', {
            class: `toast ${toastType} text-white px-6 py-3 rounded-lg shadow-lg`
        }, message);

        container.appendChild(toast);

        // Показать с анимацией
        requestAnimationFrame(() => {
            toast.classList.add('show');
        });

        // Скрыть и удалить через 3 секунды
        setTimeout(() => {
            toast.classList.remove('show');
            toast.addEventListener('transitionend', () => toast.remove());
        }, 3000);
    }
};