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
                this.dom.container.style.opacity = '1';
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
        // ИСПРАВЛЕНИЕ: Замена классов Tailwind на обычные CSS стили
        const map = {
            gray: { bg: '#f3f4f6', border: '#9ca3af', text: '#374151' },
            yellow: { bg: '#fef3c7', border: '#f59e0b', text: '#92400e' },
            green: { bg: '#d1fae5', border: '#10b981', text: '#065f46' }
        };
        const c = map[color];

        return el('div', {
                style: {
                    backgroundColor: c.bg,
                    borderLeft: `4px solid ${c.border}`,
                    color: c.text,
                    padding: '15px',
                    borderRadius: '6px',
                    boxShadow: '0 1px 2px rgba(0,0,0,0.05)'
                }
            },
            el('p', { style: { fontWeight: 'bold', margin: '0 0 5px 0' } }, title),
            el('p', { style: { margin: 0, fontSize: '13px' } }, text)
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
            // ИСПРАВЛЕНИЕ: Используем наш класс hide
            this.dom.result.classList.add('hide');
            return;
        }

        // ИСПРАВЛЕНИЕ: Используем наш класс hide
        this.dom.result.classList.remove('hide');

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
                this.dom.historyBody.innerHTML = '<tr><td colspan="6" class="text-center" style="padding: 20px; color: #888;">История пуста</td></tr>';
                return;
            }

            const fragment = document.createDocumentFragment();

            history.forEach(r => {
                // ИСПРАВЛЕНИЕ: Очистили JS от классов Tailwind, используем базу style.css
                const tr = el('tr', {},
                    el('td', { style: { fontWeight: '500' } }, r.period),
                    el('td', { class: 'text-center' }, Number(r.hot).toFixed(2)),
                    el('td', { class: 'text-center' }, Number(r.cold).toFixed(2)),
                    el('td', { class: 'text-center' }, Number(r.electric).toFixed(2)),
                    el('td', { class: 'text-center', style: { fontWeight: 'bold', color: 'var(--success-color)' } }, Number(r.total).toFixed(2)),
                    el('td', { class: 'text-center' },
                        el('button', {
                            style: { background: 'none', border: 'none', cursor: 'pointer', fontSize: '18px' },
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
                // ИСПРАВЛЕНИЕ: Используем наш собственный класс ошибки
                input.classList.add('input-error');
                error.textContent = `Меньше пред. (${prevVal})`;
                return false;
            } else {
                // ИСПРАВЛЕНИЕ: Удаляем собственный класс ошибки
                input.classList.remove('input-error');
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

        const spinner = document.getElementById('submitBtnSpinner');
        if (spinner) {
            // ИСПРАВЛЕНИЕ: Используем наш класс hide
            spinner.classList.remove('hide');
        }

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

            if (spinner) {
                // ИСПРАВЛЕНИЕ: Используем наш класс hide
                spinner.classList.add('hide');
            }
        }
    },

    async downloadReceipt(id) {
        toast('Скачивание квитанции...', 'info');
        await api.download(`/client/receipts/${id}`, `receipt_${id}.pdf`);
    }
};