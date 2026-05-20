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

        // Автозамена запятой на точку, валидация на лету, auto-format на blur.
        // + sync визуального табло (8 ячеек: 5 чёрных + 3 красных).
        const syncDisplay = (inputEl) => {
            const display = document.querySelector(
                `.meter-display[data-display-for="${inputEl.id}"]`
            );
            if (!display) return;
            // Канонизируем значение в 8-цифровое представление для отображения.
            // Алгоритм: убираем не-цифры/точку, разбиваем по точке, обрезаем
            // до 5 целых + 3 дробных, padStart/padEnd для отображения.
            const raw = (inputEl.value || '').replace(',', '.');
            const m = raw.match(/^(\d{0,5})(?:\.(\d{0,3}))?/);
            const intPart = (m && m[1] ? m[1] : '').padStart(5, '0').slice(-5);
            const fracPart = (m && m[2] ? m[2] : '').padEnd(3, '0').slice(0, 3);
            const all = intPart + fracPart;  // 8 chars
            display.querySelectorAll('.meter-cell').forEach((cell, i) => {
                cell.textContent = all[i] || '0';
            });
        };

        ['hot', 'cold', 'elect'].forEach(key => {
            const input = this.dom.inputs[key];
            if (input) {
                input.addEventListener('input', (e) => {
                    let v = e.target.value.replace(',', '.');
                    v = v.replace(/[^\d.]/g, '');
                    const firstDot = v.indexOf('.');
                    if (firstDot !== -1) {
                        v = v.slice(0, firstDot + 1) + v.slice(firstDot + 1).replace(/\./g, '');
                    }
                    e.target.value = v;
                    syncDisplay(e.target);
                    this.validate();
                });

                input.addEventListener('blur', (e) => {
                    if (e.target.dataset.strictFormat !== '5_3') return;
                    const raw = (e.target.value || '').trim();
                    if (!raw) {
                        syncDisplay(e.target);  // обнулим табло до 00000.000
                        return;
                    }
                    const m = raw.match(/^(\d{1,5})(?:\.(\d{0,3}))?$/);
                    if (!m) return;
                    const intPart = m[1].padStart(5, '0');
                    const fracPart = (m[2] || '').padEnd(3, '0');
                    e.target.value = `${intPart}.${fracPart}`;
                    syncDisplay(e.target);
                    this.validate();
                });

                // Первичная инициализация при загрузке (если уже есть значение
                // от draft или fill из state).
                syncDisplay(input);
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
            this.renderMeterFormatHint(data);
        } catch (e) {
            console.warn('Ошибка загрузки состояния показаний', e);
        }
    },

    renderMeterFormatHint(data) {
        // Показывает админскую инструкцию по формату ввода показаний
        // (см. /api/settings/meter-format). Поля приходят в state-ответе.
        // Если бэк не вернул данные — оставляем блок скрытым: лучше пусто,
        // чем показать пустую панель «Как записать показания».
        const wrap = document.getElementById('meterFormatHint');
        const instr = document.getElementById('meterFormatInstructions');
        const example = document.getElementById('meterFormatExample');
        if (!wrap || !instr || !example) return;
        if (!data.meter_instructions) {
            wrap.classList.add('hide');
            return;
        }
        instr.textContent = data.meter_instructions;
        example.textContent = data.meter_example || '';
        wrap.classList.remove('hide');
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

        // Прячем карточки счётчиков, которых у жильца нет (meters_001_per_user_config).
        // Поля становятся не-required, чтобы валидация формы не блокировала submit.
        // По умолчанию (старые серверы / отсутствие поля) показываем всё.
        const hasHw = data.has_hw_meter !== false;
        const hasCw = data.has_cw_meter !== false;
        const hasEl = data.has_el_meter !== false;
        this._applyMeterVisibility('hot', hasHw);
        this._applyMeterVisibility('cold', hasCw);
        this._applyMeterVisibility('elect', hasEl);
        // Запоминаем для confirm-сообщения / submit (см. handleSubmit).
        this.state.hasMeters = { hot: hasHw, cold: hasCw, elect: hasEl };

        if (data.is_draft || data.is_already_approved) {
            // Скрытые поля (нет счётчика) НЕ перезаписываем current_X — у них
            // уже стоит 0 от _applyMeterVisibility. Перезатирать саппортит
            // прошлые значения, что вводит в заблуждение.
            if (hasHw && this.dom.inputs.hot) this.dom.inputs.hot.value = data.current_hot;
            if (hasCw && this.dom.inputs.cold) this.dom.inputs.cold.value = data.current_cold;
            if (hasEl && this.dom.inputs.elect) this.dom.inputs.elect.value = data.current_elect;
            // Триггерим input-event чтобы пере-syncнулось табло счётчиков
            // (5 чёрных + 3 красных ячейки под каждым input).
            ['hot', 'cold', 'elect'].forEach(k => {
                const inp = this.dom.inputs[k];
                if (inp && inp.value) inp.dispatchEvent(new Event('input', { bubbles: true }));
            });
            this.validate();
        }
    },

    /**
     * Скрыть / показать карточку счётчика. При скрытии поле снимаем с required
     * и зануляем, чтобы submit не падал на пустом required input. Если у жильца
     * нет счётчика — потребление считается по нормативу из тарифа на сервере.
     */
    _applyMeterVisibility(key, isPresent) {
        const card = this.dom.cards[key];
        const input = this.dom.inputs[key];
        if (!card || !input) return;
        if (isPresent) {
            card.style.display = '';
            input.required = true;
            input.disabled = false;
        } else {
            card.style.display = 'none';
            input.required = false;
            input.disabled = true;
            // Шлём 0 — сервер всё равно перезатрёт расход на норматив × residents.
            input.value = 0;
        }
    },

    renderResults(data) {
        // Предварительный расчёт убран по решению admin'а (may 2026):
        // жильцы пугались разных сумм между «предварительно» и финальной
        // квитанцией (например, из-за корректировок бухгалтера). Теперь
        // показываем нейтральный статус «ждите квитанцию в конце периода».
        if (!this.dom.resultArea) return;
        this.dom.resultArea.classList.add('hide');
    },

    validate() {
        let isValid = true;

        const check = (key, prevVal) => {
            const input = this.dom.inputs[key];
            const error = this.dom.errors[key];
            const card = this.dom.cards[key];
            if (!input) return true;

            // Если счётчика у жильца нет (карточка скрыта / поле disabled),
            // не валидируем — расход возьмётся из норматива на сервере.
            if (input.disabled) {
                if (error) error.textContent = '';
                if (card) card.classList.remove('error');
                return true;
            }

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
            // В confirm-сообщении показываем только реальные счётчики жильца —
            // если у него нет ГВС, не пишем «ГВС: 0», это сбивает.
            const hm = this.state.hasMeters || { hot: true, cold: true, elect: true };
            const lines = ['Новые данные:'];
            if (hm.hot)   lines.push(`🔥 ГВС: ${data.hot_water}`);
            if (hm.cold)  lines.push(`❄️ ХВС: ${data.cold_water}`);
            if (hm.elect) lines.push(`⚡ Свет: ${data.electricity}`);
            const msg = `Вы уверены, что хотите перезаписать показания?\n\n${lines.join('\n')}\n\nСтарые данные будут заменены.`;
            if (!confirm(msg)) return;
        }

        const originalText = document.getElementById('submitBtnText')?.textContent || 'Отправка...';
        setLoading(this.dom.btnSubmit, true, 'Расчет...');

        const spinner = document.getElementById('submitBtnSpinner');
        if (spinner) spinner.classList.remove('hide');

        try {
            await api.post('/calculate', data);
            toast('Спасибо! Показания приняты. Квитанция появится в разделе «История» после закрытия периода.', 'success');
            await this.loadState(); // Перезагружаем UI
        } catch (e) {
            toast(e.message, 'error');
        } finally {
            setLoading(this.dom.btnSubmit, false, originalText);
            if (spinner) spinner.classList.add('hide');
        }
    }
};