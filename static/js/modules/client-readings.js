// static/js/modules/client-readings.js
import { api } from '../core/api.js';
import { el, toast, setLoading, showConfirm } from '../core/dom.js';

export const ClientReadings = {
    state: {
        lastReadings: { hot: 0, cold: 0, elect: 0 },
        isPeriodOpen: false,
        isDraft: false,
        isAlreadyApproved: false
    },

    init() {
        this.cacheDOM();
        this.buildMeters();
        this.bindEvents();
        this.loadState();
        this.checkDataRefresh();
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
    },

    // ── Интерактивный механический счётчик ──────────────────────────────
    // Барабан строится из data-display-for (=id скрытого input). Каждая
    // цифра — ячейка со стрелками ▲▼ + клавиатурный ввод (0-9, ↑↓←→,
    // Backspace, колесо мыши). Значение собирается в скрытый input
    // (5 целых + 3 дробных), который читают validate()/handleSubmit().
    buildMeters() {
        ['hot', 'cold', 'elect'].forEach(key => this.buildMeter(key));
    },

    buildMeter(key) {
        const input = this.dom.inputs[key];
        if (!input) return;
        const display = document.querySelector(`.meter-display[data-display-for="${input.id}"]`);
        if (!display) return;
        display.innerHTML = '';
        for (let pos = 0; pos < 8; pos++) {
            if (pos === 5) display.appendChild(el('span', { class: 'meter-dot' }, ','));
            const isFrac = pos >= 5;
            const cell = el('span', {
                class: `meter-cell${isFrac ? ' frac' : ''}`,
                'data-pos': String(pos), tabindex: '0',
                role: 'spinbutton', 'aria-label': `Разряд ${pos + 1}`,
            }, '0');
            const up = el('button', { type: 'button', class: 'meter-step up', tabindex: '-1', 'aria-label': '+1' }, '▲');
            const down = el('button', { type: 'button', class: 'meter-step down', tabindex: '-1', 'aria-label': '−1' }, '▼');
            up.addEventListener('click', () => this.stepCell(key, cell, +1));
            down.addEventListener('click', () => this.stepCell(key, cell, -1));
            cell.addEventListener('keydown', (e) => this.onCellKey(key, cell, e));
            cell.addEventListener('wheel', (e) => { e.preventDefault(); this.stepCell(key, cell, e.deltaY < 0 ? +1 : -1); }, { passive: false });
            cell.addEventListener('focus', () => cell.classList.add('active'));
            cell.addEventListener('blur', () => cell.classList.remove('active'));
            display.appendChild(el('span', { class: 'meter-digit' }, up, cell, down));
        }
    },

    _cells(key) {
        const input = this.dom.inputs[key];
        if (!input) return [];
        const display = document.querySelector(`.meter-display[data-display-for="${input.id}"]`);
        return display ? Array.from(display.querySelectorAll('.meter-cell')) : [];
    },

    _focusSibling(key, cell, dir) {
        const cells = this._cells(key);
        const next = cells[cells.indexOf(cell) + dir];
        if (next) next.focus();
    },

    stepCell(key, cell, delta) {
        let d = (parseInt(cell.textContent, 10) || 0) + delta;
        d = ((d % 10) + 10) % 10;
        cell.textContent = String(d);
        cell.classList.remove('changed'); void cell.offsetWidth; cell.classList.add('changed');
        this.syncInputFromCells(key);
    },

    onCellKey(key, cell, e) {
        if (e.key >= '0' && e.key <= '9') {
            cell.textContent = e.key;
            cell.classList.remove('changed'); void cell.offsetWidth; cell.classList.add('changed');
            this.syncInputFromCells(key);
            this._focusSibling(key, cell, +1);
            e.preventDefault();
        } else if (e.key === 'ArrowUp') { this.stepCell(key, cell, +1); e.preventDefault(); }
        else if (e.key === 'ArrowDown') { this.stepCell(key, cell, -1); e.preventDefault(); }
        else if (e.key === 'ArrowLeft') { this._focusSibling(key, cell, -1); e.preventDefault(); }
        else if (e.key === 'ArrowRight') { this._focusSibling(key, cell, +1); e.preventDefault(); }
        else if (e.key === 'Backspace' || e.key === 'Delete') {
            cell.textContent = '0';
            this.syncInputFromCells(key);
            if (e.key === 'Backspace') this._focusSibling(key, cell, -1);
            e.preventDefault();
        }
    },

    syncInputFromCells(key) {
        const cells = this._cells(key);
        if (cells.length !== 8) return;
        const all = cells.map(c => (c.textContent || '0').replace(/\D/g, '') || '0').join('');
        this.dom.inputs[key].value = `${all.slice(0, 5)}.${all.slice(5, 8)}`;
        this.validate();
    },

    setCellsFromValue(key, value) {
        const cells = this._cells(key);
        if (cells.length !== 8) return;
        const fixed = (Number(value) || 0).toFixed(3);
        const [i, f] = fixed.split('.');
        const all = i.padStart(5, '0').slice(-5) + (f || '').padEnd(3, '0').slice(0, 3);
        cells.forEach((c, idx) => { c.textContent = all[idx] || '0'; });
        this.dom.inputs[key].value = `${all.slice(0, 5)}.${all.slice(5, 8)}`;
    },

    // ── Data-refresh (Bug BB): админ запросил актуальные данные ──────────
    // GET /me/data-refresh → {required}. Если true — показываем модалку;
    // жилец подтверждает общагу/комнату/число жильцов → POST /me/data-refresh,
    // флаг на сервере снимается. Раньше эта фича вообще не вызывалась с фронта.
    async checkDataRefresh() {
        try {
            const st = await api.get('/me/data-refresh');
            if (st && st.required) this.openDataRefreshModal();
        } catch (e) { /* не критично — не блокируем портал */ }
    },

    openDataRefreshModal() {
        const modal = document.getElementById('dataRefreshModal');
        if (!modal) return;
        modal.classList.add('open');
        const form = document.getElementById('dataRefreshForm');
        if (form && !form._bound) {
            form._bound = true;
            form.addEventListener('submit', (e) => this.submitDataRefresh(e));
            modal.querySelectorAll('[data-dr-close]').forEach(b =>
                b.addEventListener('click', () => modal.classList.remove('open')));
        }
    },

    async submitDataRefresh(e) {
        e.preventDefault();
        const btn = e.target.querySelector('button[type="submit"]');
        const body = {
            dorm_name: (document.getElementById('drDorm').value || '').trim(),
            room_number: (document.getElementById('drRoom').value || '').trim(),
            residents_count: parseInt(document.getElementById('drResidents').value) || 1,
        };
        if (!body.dorm_name || !body.room_number) {
            toast('Укажите общежитие и комнату', 'error');
            return;
        }
        setLoading(btn, true, 'Отправка...');
        try {
            await api.post('/me/data-refresh', body);
            toast('Спасибо! Данные отправлены администратору.', 'success');
            document.getElementById('dataRefreshModal').classList.remove('open');
        } catch (err) {
            toast(err.message, 'error');
        } finally {
            setLoading(btn, false);
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

        // Bug AW3: тариф без счётчиков — клиент НЕ должен видеть форму подачи.
        // submission_required=false когда у тарифа жильца все 4 charge_*-meter
        // выключены (например, тариф «Только наём» или «Без счётчиков»).
        // Сюда же попадают per_capita (койко-место).
        if (data.submission_required === false) {
            const isPerCapita = data.billing_mode === 'per_capita';
            const title = isPerCapita ? '🛏 Койко-место' : '✓ Подача не требуется';
            const subtitle = isPerCapita
                ? 'Вы оформлены на тариф «койко-место». Сумма к оплате фиксированная — указана ниже в квитанции. Показания счётчиков подавать не нужно.'
                : 'На вашем тарифе показания счётчиков не подаются. Начисления делает администрация по фиксированным статьям (см. квитанцию).';
            content = this.createStatusBox('#ecfdf5', '#10b981', '#065f46', title, subtitle);
            this.dom.fieldset.disabled = true;
            if (btnText) btnText.textContent = 'Подача не требуется';
            this.dom.btnSubmit.style.background = '#9ca3af';
            this.dom.btnSubmit.style.boxShadow = 'none';
            this.dom.btnSubmit.disabled = true;
            this.dom.statusArea.appendChild(content);
            return;
        }

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
            // Уже поданное значение — заполняем барабан текущими показаниями.
            if (hasHw) this.setCellsFromValue('hot', data.current_hot);
            if (hasCw) this.setCellsFromValue('cold', data.current_cold);
            if (hasEl) this.setCellsFromValue('elect', data.current_elect);
        } else {
            // Новая подача — стартуем барабан с ПРЕДЫДУЩЕГО показания, жильцу
            // остаётся «докрутить» стрелками до текущего (так удобнее и
            // меньше ошибок, чем вводить с нуля).
            if (hasHw) this.setCellsFromValue('hot', data.prev_hot);
            if (hasCw) this.setCellsFromValue('cold', data.prev_cold);
            if (hasEl) this.setCellsFromValue('elect', data.prev_elect);
        }

        // Живая метка «не начисляется»: charge_*-флаги эффективного тарифа
        // комнаты приходят в /api/readings/state. Если услуга не начисляется
        // (charge_*=false) — карточка приглушается, поле снимается с submit,
        // под ней подпись. Нигде не хранится → переезд в тариф со счётчиками
        // (charge=true) вернёт карточку в норму на следующем ответе.
        this._applyMeterCharge('hot', data.charge_hot_water !== false);
        this._applyMeterCharge('cold', data.charge_cold_water !== false);
        this._applyMeterCharge('elect', data.charge_electricity !== false);

        this.validate();
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

    /**
     * Живая метка «не начисляется по тарифу» на карточке счётчика.
     * Источник истины — charge_*-флаги из /api/readings/state (эффективный
     * тариф КОМНАТЫ, не сохранён). При isCharged=false карточка приглушается,
     * поле снимается с submit, добавляется подпись. При isCharged=true метка
     * убирается (видимость карточки остаётся за _applyMeterVisibility/has_meter).
     * Так переезд в тариф со счётчиками автоматически возвращает поле в строй.
     */
    _applyMeterCharge(key, isCharged) {
        const card = this.dom.cards[key];
        const input = this.dom.inputs[key];
        if (!card) return;
        let lbl = card.querySelector('.not-charged-label');
        if (!isCharged) {
            card.style.display = '';        // показать даже если has_meter=false
            card.style.opacity = '0.55';
            if (input) { input.required = false; input.disabled = true; input.value = 0; }
            if (!lbl) {
                lbl = el('div', {
                    class: 'not-charged-label',
                    style: { marginTop: '6px', fontSize: '11px', fontStyle: 'italic', color: '#9ca3af', textAlign: 'center' }
                }, 'не начисляется по тарифу');
                card.appendChild(lbl);
            }
            lbl.style.display = '';
        } else {
            card.style.opacity = '';
            if (lbl) lbl.style.display = 'none';
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
            if (!await showConfirm(msg)) return;
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