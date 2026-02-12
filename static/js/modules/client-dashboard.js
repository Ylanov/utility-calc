// static/js/modules/client-dashboard.js
import { api } from '../core/api.js';
import { el, clear, toast, setLoading } from '../core/dom.js';

export const ClientDashboard = {
    state: {
        lastReadings: { hot: 0, cold: 0, elect: 0 },
        isPeriodOpen: false
    },

    init() {
        this.cacheDOM();
        this.bindEvents();
        this.loadAllData();
    },

    cacheDOM() {
        this.dom = {
            container: document.getElementById('app-container'),
            profile: {
                user: document.getElementById('pUser'),
                address: document.getElementById('pAddress'),
                area: document.getElementById('pArea'),
                residents: document.getElementById('pResidents'),
                period: document.getElementById('pPeriod')
            },
            statusArea: document.getElementById('statusArea'),
            form: document.getElementById('meterForm'),
            fieldset: document.getElementById('meterFieldset'),
            inputs: {
                hot: document.getElementById('hotWater'),
                cold: document.getElementById('coldWater'),
                elect: document.getElementById('electricity')
            },
            prev: {
                hot: document.getElementById('prevHot'),
                cold: document.getElementById('prevCold'),
                elect: document.getElementById('prevElect')
            },
            errors: {
                hot: document.getElementById('hotError'),
                cold: document.getElementById('coldError'),
                elect: document.getElementById('electError')
            },
            btnSubmit: document.getElementById('submitBtn'),
            result: document.getElementById('result'),
            historyBody: document.getElementById('historyBody')
        };
    },

    bindEvents() {
        if (this.dom.form) {
            this.dom.form.addEventListener('submit', (e) => this.handleSubmit(e));
        }

        // Валидация при вводе
        ['hot', 'cold', 'elect'].forEach(key => {
            const input = this.dom.inputs[key];
            if (input) {
                input.addEventListener('input', () => this.validate());
            }
        });
    },

    async loadAllData() {
        try {
            // Параллельная загрузка данных
            await Promise.all([
                this.loadProfile(),
                this.loadState(),
                this.loadHistory()
            ]);

            // Показываем интерфейс после загрузки
            if (this.dom.container) {
                this.dom.container.classList.remove('opacity-0');
            }
        } catch (e) {
            toast('Ошибка загрузки данных: ' + e.message, 'error');
        }
    },

    async loadProfile() {
        try {
            const user = await api.get('/users/me');
            this.dom.profile.user.textContent = user.username;
            this.dom.profile.address.textContent = user.dormitory || '-';
            this.dom.profile.area.textContent = `${Number(user.apartment_area).toFixed(1)} м²`;
            this.dom.profile.residents.textContent = user.residents_count;
        } catch (e) {
            console.warn('Profile load error', e);
        }
    },

    async loadState() {
        const data = await api.get('/readings/state');

        this.state.isPeriodOpen = data.is_period_open;
        this.state.lastReadings = {
            hot: Number(data.prev_hot),
            cold: Number(data.prev_cold),
            elect: Number(data.prev_elect)
        };

        // Обновляем UI
        this.dom.profile.period.textContent = data.period_name || 'Закрыт';

        this.renderStatus(data);
        this.renderMeters(data);
        this.renderResults(data);
    },

    renderStatus(data) {
        this.dom.statusArea.innerHTML = '';
        let content;

        if (!data.is_period_open) {
            content = this.createStatusBox('gray', '🔒 Прием закрыт', 'Подача показаний завершена.');
            this.dom.fieldset.disabled = true;
        } else if (data.is_draft) {
            content = this.createStatusBox('yellow', '✏️ Черновик', 'Показания сохранены, но их можно изменить.');
            this.dom.fieldset.disabled = false;
        } else {
            content = this.createStatusBox('green', '🟢 Период открыт', 'Введите текущие показания.');
            this.dom.fieldset.disabled = false;
        }

        this.dom.statusArea.appendChild(content);
    },

    createStatusBox(color, title, text) {
        const map = {
            gray: { bg: 'bg-gray-100', border: 'border-gray-500', text: 'text-gray-700' },
            yellow: { bg: 'bg-yellow-100', border: 'border-yellow-500', text: 'text-yellow-700' },
            green: { bg: 'bg-green-100', border: 'border-green-500', text: 'text-green-700' }
        };
        const c = map[color];

        return el('div', { class: `${c.bg} border-l-4 ${c.border} ${c.text} p-4 rounded-md shadow-sm` },
            el('p', { class: 'font-bold' }, title),
            el('p', { class: 'text-sm' }, text)
        );
    },

    renderMeters(data) {
        // Предыдущие значения
        this.dom.prev.hot.textContent = Number(data.prev_hot).toFixed(3);
        this.dom.prev.cold.textContent = Number(data.prev_cold).toFixed(3);
        this.dom.prev.elect.textContent = Number(data.prev_elect).toFixed(3);

        // Если есть черновик, заполняем инпуты
        if (data.is_draft) {
            this.dom.inputs.hot.value = data.current_hot;
            this.dom.inputs.cold.value = data.current_cold;
            this.dom.inputs.elect.value = data.current_elect;
        }
    },

    renderResults(data) {
        if (!data.total_cost && data.total_cost !== 0) {
            this.dom.result.classList.add('hidden');
            return;
        }

        this.dom.result.classList.remove('hidden');

        const fmt = (val) => `${Number(val || 0).toFixed(2)} ₽`;

        const map = {
            rHot: data.cost_hot_water,
            rCold: data.cost_cold_water,
            rSew: data.cost_sewage,
            rEl: data.cost_electricity,
            rMain: data.cost_maintenance,
            rRent: data.cost_social_rent,
            rWaste: data.cost_waste,
            rFix: data.cost_fixed_part,
            rTotal: data.total_cost
        };

        for (const [id, val] of Object.entries(map)) {
            const elem = document.getElementById(id);
            if (elem) elem.textContent = fmt(val);
        }
    },

    async loadHistory() {
        this.dom.historyBody.innerHTML = '';

        try {
            const history = await api.get('/readings/history');

            if (!history.length) {
                this.dom.historyBody.innerHTML = '<tr><td colspan="6" class="p-4 text-center text-gray-500">История пуста</td></tr>';
                return;
            }

            const fragment = document.createDocumentFragment();

            history.forEach(r => {
                const tr = el('tr', { class: 'hover:bg-gray-50 transition-colors' },
                    el('td', { class: 'border p-2 font-medium' }, r.period),
                    el('td', { class: 'border p-2 text-center' }, Number(r.hot).toFixed(2)),
                    el('td', { class: 'border p-2 text-center' }, Number(r.cold).toFixed(2)),
                    el('td', { class: 'border p-2 text-center' }, Number(r.electric).toFixed(2)),
                    el('td', { class: 'border p-2 text-center font-bold text-green-700' }, Number(r.total).toFixed(2)),
                    el('td', { class: 'border p-2 text-center' },
                        el('button', {
                            class: 'text-blue-500 hover:text-blue-700 transition-colors text-xl',
                            title: 'Скачать PDF',
                            onclick: () => this.downloadReceipt(r.id)
                        }, '📄')
                    )
                );
                fragment.appendChild(tr);
            });

            this.dom.historyBody.appendChild(fragment);
        } catch (e) {
            console.warn('History load error', e);
        }
    },

    // --- ЛОГИКА ---

    validate() {
        let isValid = true;

        const check = (key, prevVal) => {
            const input = this.dom.inputs[key];
            const error = this.dom.errors[key];
            const val = parseFloat(input.value);

            // Если поле пустое или меньше предыдущего
            if (!input.value || isNaN(val)) {
                return false; // Просто невалидно, но ошибку не показываем пока
            }

            if (val < prevVal) {
                input.classList.add('border-red-500', 'focus:ring-red-500');
                error.textContent = `Меньше пред. (${prevVal})`;
                return false;
            } else {
                input.classList.remove('border-red-500', 'focus:ring-red-500');
                error.textContent = '';
                return true;
            }
        };

        const v1 = check('hot', this.state.lastReadings.hot);
        const v2 = check('cold', this.state.lastReadings.cold);
        const v3 = check('elect', this.state.lastReadings.elect);

        isValid = v1 && v2 && v3;

        this.dom.btnSubmit.disabled = !isValid;
        return isValid;
    },

    async handleSubmit(e) {
        e.preventDefault();
        if (!this.validate()) return;

        setLoading(this.dom.btnSubmit, true, 'Расчет...');
        document.getElementById('submitBtnSpinner').classList.remove('hidden');

        const data = {
            hot_water: parseFloat(this.dom.inputs.hot.value),
            cold_water: parseFloat(this.dom.inputs.cold.value),
            electricity: parseFloat(this.dom.inputs.elect.value)
        };

        try {
            await api.post('/calculate', data);
            toast('Показания сохранены', 'success');
            // Перезагружаем состояние, чтобы обновить "Черновик" и расчеты
            await this.loadState();
        } catch (e) {
            toast(e.message, 'error');
        } finally {
            setLoading(this.dom.btnSubmit, false, '💾 Сохранить');
            document.getElementById('submitBtnSpinner').classList.add('hidden');
        }
    },

    async downloadReceipt(id) {
        toast('Скачивание квитанции...', 'info');
        await api.download(`/client/receipts/${id}`, `receipt_${id}.pdf`);
    }
};