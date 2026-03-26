// static/js/modules/client-readings.js
import { api } from '../core/api.js';
import { el, toast, setLoading } from '../core/dom.js';

export const ClientReadings = {
    state: {
        lastReadings: { hot: 0, cold: 0, elect: 0 },
        isPeriodOpen: false,
        isDraft: false,
        isAlreadyApproved: false
    },

    init() {
        this.cacheDOM();
        this.bindEvents();
        this.checkSchedule();
        this.loadState();
    },

    cacheDOM() {
        this.dom = {
            statusArea: document.getElementById('statusArea'),
            form: document.getElementById('meterForm'),
            fieldset: document.getElementById('meterFieldset'),
            btnSubmit: document.getElementById('submitBtn'),
            resultArea: document.getElementById('result'),

            cards: {
                hot: document.getElementById('cardHot'),
                cold: document.getElementById('cardCold'),
                elect: document.getElementById('cardElect')
            },
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
            }
        };
    },

    bindEvents() {
        if (this.dom.form) {
            this.dom.form.addEventListener('submit', (e) => this.handleSubmit(e));
        }

        // Автозамена запятой на точку и валидация на лету
        ['hot', 'cold', 'elect'].forEach(key => {
            const input = this.dom.inputs[key];
            if (input) {
                input.addEventListener('input', (e) => {
                    e.target.value = e.target.value.replace(',', '.');
                    this.validate();
                });
            }
        });
    },

    async checkSchedule() {
        try {
            const settings = await api.get('/settings/submission-period');
            const now = new Date();
            const day = now.getDate();

            const alertBox = document.getElementById('submissionAlert');
            const title = document.getElementById('subAlertTitle');
            const text = document.getElementById('subAlertText');

            if (!alertBox || !title || !text) return;

            alertBox.style.display = 'flex';

            if (day >= settings.start_day && day <= settings.end_day) {
                alertBox.style.background = '#ecfdf5';
                alertBox.style.border = '1px solid #a7f3d0';
                title.textContent = 'Прием показаний открыт!';
                title.style.color = '#065f46';
                text.textContent = `Пожалуйста, внесите данные до ${settings.end_day}-го числа. Осталось дней: ${settings.end_day - day}.`;
            } else if (day < settings.start_day) {
                alertBox.style.background = '#eff6ff';
                alertBox.style.border = '1px solid #bfdbfe';
                title.textContent = 'Прием показаний скоро начнется';
                title.style.color = '#1e40af';
                text.textContent = `Ввод данных будет доступен с ${settings.start_day}-го числа (через ${settings.start_day - day} дн).`;
            } else {
                alertBox.style.background = '#fef2f2';
                alertBox.style.border = '1px solid #fecaca';
                title.textContent = 'Прием показаний завершен';
                title.style.color = '#991b1b';
                text.textContent = `В этом месяце прием закрыт. Следующий период начнется ${settings.start_day}-го числа.`;
            }
        } catch (e) {
            console.warn('Ошибка загрузки графика', e);
        }
    },

    async loadState() {
        try {
            const data = await api.get('/readings/state');

            this.state.isPeriodOpen = data.is_period_open;
            this.state.isDraft = data.is_draft;
            this.state.isAlreadyApproved = data.is_already_approved;
            this.state.lastReadings = {
                hot: Number(data.prev_hot),
                cold: Number(data.prev_cold),
                elect: Number(data.prev_elect)
            };

            this.renderStatus(data);
            this.renderMeters(data);
            this.renderResults(data);
        } catch (e) {
            console.warn('Ошибка загрузки состояния показаний', e);
        }
    },

    renderStatus(data) {
        if (!this.dom.statusArea) return;
        this.dom.statusArea.innerHTML = '';

        let content;
        const btnText = document.getElementById('submitBtnText');

        if (!data.is_period_open) {
            content = this.createStatusBox('#f3f4f6', '#9ca3af', '#374151', '🔒 Прием закрыт', 'Подача показаний в данный момент недоступна.');
            this.dom.fieldset.disabled = true;
            if (btnText) btnText.textContent = 'Прием закрыт';
            this.dom.btnSubmit.style.background = '#9ca3af';
            this.dom.btnSubmit.style.boxShadow = 'none';

        } else if (data.is_already_approved) {
            content = this.createStatusBox('#eff6ff', '#3b82f6', '#1e3a8a', '🔒 Показания приняты', 'Ваши показания уже проверены и приняты бухгалтерией. Изменение данных недоступно.');
            this.dom.fieldset.disabled = true;
            if (btnText) btnText.textContent = 'Утверждено';
            this.dom.btnSubmit.style.background = '#9ca3af';
            this.dom.btnSubmit.style.boxShadow = 'none';

        } else if (data.is_draft) {
            content = this.createStatusBox('#fef3c7', '#f59e0b', '#92400e', '✏️ Черновик сохранен', `Ваши показания приняты. Период: ${data.period_name}. Вы можете обновить их до закрытия месяца.`);
            this.dom.fieldset.disabled = false;
            if (btnText) btnText.textContent = 'Обновить данные';
            this.dom.btnSubmit.style.background = '#f59e0b';
            this.dom.btnSubmit.style.boxShadow = '0 4px 15px rgba(245, 158, 11, 0.4)';

        } else {
            content = this.createStatusBox('#d1fae5', '#10b981', '#065f46', '🟢 Период открыт', `Текущий расчетный период: ${data.period_name}. Пожалуйста, внесите показания.`);
            this.dom.fieldset.disabled = false;
            if (btnText) btnText.textContent = 'Отправить показания';
            this.dom.btnSubmit.style.background = 'var(--primary-color)';
            this.dom.btnSubmit.style.boxShadow = '0 4px 15px rgba(59, 130, 246, 0.4)';
        }

        this.dom.statusArea.appendChild(content);
    },

    createStatusBox(bg, border, text, title, desc) {
        return el('div', {
                style: { backgroundColor: bg, borderLeft: `4px solid ${border}`, color: text, padding: '15px 20px', borderRadius: '8px', boxShadow: '0 2px 4px rgba(0,0,0,0.05)'}
            },
            el('h4', { style: { margin: '0 0 5px 0', fontSize: '15px' } }, title),
            el('p', { style: { margin: 0, fontSize: '13px' } }, desc)
        );
    },

    renderMeters(data) {
        if (this.dom.prev.hot) this.dom.prev.hot.textContent = Number(data.prev_hot).toFixed(3);
        if (this.dom.prev.cold) this.dom.prev.cold.textContent = Number(data.prev_cold).toFixed(3);
        if (this.dom.prev.elect) this.dom.prev.elect.textContent = Number(data.prev_elect).toFixed(3);

        if (data.is_draft || data.is_already_approved) {
            if (this.dom.inputs.hot) this.dom.inputs.hot.value = data.current_hot;
            if (this.dom.inputs.cold) this.dom.inputs.cold.value = data.current_cold;
            if (this.dom.inputs.elect) this.dom.inputs.elect.value = data.current_elect;
            this.validate();
        }
    },

    renderResults(data) {
        if (!this.dom.resultArea) return;

        if (!data.total_cost && data.total_cost !== 0) {
            this.dom.resultArea.classList.add('hide');
            return;
        }

        this.dom.resultArea.classList.remove('hide');
        const fmt = (val) => `${Number(val || 0).toFixed(2)} ₽`;

        const map = {
            rHot: data.cost_hot_water, rCold: data.cost_cold_water, rSew: data.cost_sewage,
            rEl: data.cost_electricity, rMain: data.cost_maintenance, rRent: data.cost_social_rent,
            rWaste: data.cost_waste, rFix: data.cost_fixed_part, rTotal: data.total_cost
        };

        for (const[id, val] of Object.entries(map)) {
            const elem = document.getElementById(id);
            if (elem) elem.textContent = fmt(val);
        }
    },

    validate() {
        let isValid = true;

        const check = (key, prevVal) => {
            const input = this.dom.inputs[key];
            const error = this.dom.errors[key];
            const card = this.dom.cards[key];
            if (!input) return true;

            const val = parseFloat(input.value);

            if (!input.value || isNaN(val)) {
                card.classList.remove('error');
                error.textContent = '';
                return false;
            }

            if (val < prevVal) {
                card.classList.add('error');
                error.textContent = `Значение должно быть не меньше ${prevVal}`;
                return false;
            } else {
                card.classList.remove('error');
                error.textContent = '';
                return true;
            }
        };

        const v1 = check('hot', this.state.lastReadings.hot);
        const v2 = check('cold', this.state.lastReadings.cold);
        const v3 = check('elect', this.state.lastReadings.elect);

        isValid = v1 && v2 && v3;
        if (this.dom.btnSubmit) this.dom.btnSubmit.disabled = !isValid;
        return isValid;
    },

    async handleSubmit(e) {
        e.preventDefault();
        if (!this.validate()) return;

        if (this.state.isAlreadyApproved) {
            toast('Ваши показания уже утверждены бухгалтерией!', 'error');
            return;
        }

        const data = {
            hot_water: parseFloat(this.dom.inputs.hot.value),
            cold_water: parseFloat(this.dom.inputs.cold.value),
            electricity: parseFloat(this.dom.inputs.elect.value)
        };

        if (this.state.isDraft) {
            const msg = `Вы уверены, что хотите перезаписать показания?\n\nНовые данные:\n🔥 ГВС: ${data.hot_water}\n❄️ ХВС: ${data.cold_water}\n⚡ Свет: ${data.electricity}\n\nСтарые данные будут заменены.`;
            if (!confirm(msg)) return;
        }

        const originalText = document.getElementById('submitBtnText')?.textContent || 'Отправка...';
        setLoading(this.dom.btnSubmit, true, 'Расчет...');

        const spinner = document.getElementById('submitBtnSpinner');
        if (spinner) spinner.classList.remove('hide');

        try {
            await api.post('/calculate', data);
            toast('Показания успешно сохранены', 'success');
            await this.loadState(); // Перезагружаем UI
        } catch (e) {
            toast(e.message, 'error');
        } finally {
            setLoading(this.dom.btnSubmit, false, originalText);
            if (spinner) spinner.classList.add('hide');
        }
    }
};