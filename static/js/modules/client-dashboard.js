// static/js/modules/client-dashboard.js
import { api } from '../core/api.js';
import { el, clear, toast, setLoading } from '../core/dom.js';
import { Auth } from '../core/auth.js'; // Нужен для логаута после смены логина

export const ClientDashboard = {
    state: {
        lastReadings: { hot: 0, cold: 0, elect: 0 },
        isPeriodOpen: false
    },

    init() {
        this.cacheDOM();
        this.setupTabs();
        this.bindEvents();
        this.loadAllData();
    },

    cacheDOM() {
        this.dom = {
            container: document.getElementById('app-container'),
            headerAddress: document.getElementById('headerAddress'),

            // Профиль
            profile: {
                user: document.getElementById('pUser'),
                address: document.getElementById('pAddress'),
                area: document.getElementById('pArea'),
                residents: document.getElementById('pResidents')
            },

            // Ввод показаний
            statusArea: document.getElementById('statusArea'),
            form: document.getElementById('meterForm'),
            fieldset: document.getElementById('meterFieldset'),
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
            },
            btnSubmit: document.getElementById('submitBtn'),
            result: document.getElementById('result'),

            // История
            historyBody: document.getElementById('historyBody'),

            // Смена пароля
            cpForm: document.getElementById('changePasswordForm'),
            cpOld: document.getElementById('cpOld'),
            cpNew: document.getElementById('cpNew'),
            cpNewConfirm: document.getElementById('cpNewConfirm'),
            btnCp: document.getElementById('btnChangePassword'),

            // Первичная настройка
            fsModal: document.getElementById('firstSetupModal'),
            fsCurrentLogin: document.getElementById('fsCurrentLogin'),
            fsForm: document.getElementById('firstSetupForm'),
            fsNewLogin: document.getElementById('fsNewLogin'),
            fsNewPassword: document.getElementById('fsNewPassword'),
            btnFsSave: document.getElementById('btnFsSave'),
            btnFsShowForm: document.getElementById('btnFsShowForm'),
            btnFsSkip: document.getElementById('btnFsSkip'),
            fsActionButtons: document.getElementById('fsActionButtons')
        };
    },

    setupTabs() {
        const tabs = document.querySelectorAll('.tab-btn');
        const contents = document.querySelectorAll('.tab-content');

        tabs.forEach(tab => {
            tab.addEventListener('click', () => {
                // Убираем активность со всех
                tabs.forEach(t => t.classList.remove('active'));
                contents.forEach(c => c.classList.remove('active'));

                // Добавляем активность выбранному
                tab.classList.add('active');
                const targetId = tab.dataset.tab;
                document.getElementById(targetId).classList.add('active');
            });
        });
    },

    bindEvents() {
        // Форма показаний
        if (this.dom.form) {
            this.dom.form.addEventListener('submit', (e) => this.handleSubmit(e));
        }

        // Валидация при вводе показаний (умные карточки)
        ['hot', 'cold', 'elect'].forEach(key => {
            const input = this.dom.inputs[key];
            if (input) {
                input.addEventListener('input', () => this.validate());
            }
        });

        // Форма смены пароля в профиле
        if (this.dom.cpForm) {
            this.dom.cpForm.addEventListener('submit', (e) => this.handleChangePassword(e));
        }

        // --- События модалки Первичной Настройки ---
        if (this.dom.btnFsSkip) {
            this.dom.btnFsSkip.addEventListener('click', () => this.skipFirstSetup());
        }
        if (this.dom.btnFsShowForm) {
            this.dom.btnFsShowForm.addEventListener('click', () => {
                this.dom.fsActionButtons.classList.add('hide');
                this.dom.fsForm.classList.remove('hide');
            });
        }
        if (this.dom.fsForm) {
            this.dom.fsForm.addEventListener('submit', (e) => this.saveFirstSetup(e));
        }
    },

    async loadAllData() {
        try {
            await Promise.all([
                this.loadProfile(),
                this.loadState(),
                this.loadHistory()
            ]);

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

            // Заполняем интерфейс
            this.dom.profile.user.textContent = user.username;
            this.dom.profile.address.textContent = user.dormitory || 'Адрес не указан';
            this.dom.profile.area.textContent = `${Number(user.apartment_area).toFixed(1)} м²`;
            this.dom.profile.residents.textContent = user.residents_count;
            this.dom.headerAddress.textContent = user.dormitory || 'ЖКХ — управление показаниями';

            // ПРОВЕРКА ПЕРВИЧНОЙ НАСТРОЙКИ
            if (user.is_initial_setup_done === false) {
                this.dom.fsCurrentLogin.textContent = user.username;
                this.dom.fsModal.classList.add('open');
            }

        } catch (e) {
            console.warn('Profile load error', e);
        }
    },

    async loadState() {
        try {
            const data = await api.get('/readings/state');

            this.state.isPeriodOpen = data.is_period_open;
            this.state.lastReadings = {
                hot: Number(data.prev_hot),
                cold: Number(data.prev_cold),
                elect: Number(data.prev_elect)
            };

            this.renderStatus(data);
            this.renderMeters(data);
            this.renderResults(data);
        } catch (e) {
            console.warn('State load error', e);
        }
    },

    renderStatus(data) {
        this.dom.statusArea.innerHTML = '';
        let content;

        if (!data.is_period_open) {
            content = this.createStatusBox('#f3f4f6', '#9ca3af', '#374151', '🔒 Прием закрыт', 'Подача показаний в данный момент недоступна.');
            this.dom.fieldset.disabled = true;
        } else if (data.is_draft) {
            content = this.createStatusBox('#fef3c7', '#f59e0b', '#92400e', '✏️ Черновик сохранен', `Ваши показания приняты. Период: ${data.period_name}. Вы можете изменить их до закрытия месяца.`);
            this.dom.fieldset.disabled = false;
        } else {
            content = this.createStatusBox('#d1fae5', '#10b981', '#065f46', '🟢 Период открыт', `Текущий расчетный период: ${data.period_name}. Пожалуйста, внесите показания.`);
            this.dom.fieldset.disabled = false;
        }

        this.dom.statusArea.appendChild(content);
    },

    createStatusBox(bg, border, text, title, desc) {
        return el('div', {
                style: {
                    backgroundColor: bg,
                    borderLeft: `4px solid ${border}`,
                    color: text,
                    padding: '15px 20px',
                    borderRadius: '8px',
                    boxShadow: '0 2px 4px rgba(0,0,0,0.05)'
                }
            },
            el('h4', { style: { margin: '0 0 5px 0', fontSize: '15px' } }, title),
            el('p', { style: { margin: 0, fontSize: '13px' } }, desc)
        );
    },

    renderMeters(data) {
        this.dom.prev.hot.textContent = Number(data.prev_hot).toFixed(3);
        this.dom.prev.cold.textContent = Number(data.prev_cold).toFixed(3);
        this.dom.prev.elect.textContent = Number(data.prev_elect).toFixed(3);

        if (data.is_draft) {
            this.dom.inputs.hot.value = data.current_hot;
            this.dom.inputs.cold.value = data.current_cold;
            this.dom.inputs.elect.value = data.current_elect;
            this.validate(); // Прогоняем валидацию, чтобы снять красные рамки, если данные верны
        }
    },

    renderResults(data) {
        if (!data.total_cost && data.total_cost !== 0) {
            this.dom.result.classList.add('hide');
            return;
        }

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
                const tr = el('tr', {},
                    el('td', { style: { fontWeight: '600' } }, r.period),
                    el('td', { class: 'text-right' }, Number(r.hot).toFixed(3)),
                    el('td', { class: 'text-right' }, Number(r.cold).toFixed(3)),
                    el('td', { class: 'text-right' }, Number(r.electric).toFixed(3)),
                    el('td', { class: 'text-right', style: { fontWeight: 'bold', color: 'var(--success-color)' } }, `${Number(r.total).toFixed(2)} ₽`),
                    el('td', { class: 'text-center' },
                        el('button', {
                            class: 'action-btn secondary-btn',
                            style: { padding: '4px 10px', fontSize: '12px' },
                            title: 'Скачать PDF',
                            onclick: () => this.downloadReceipt(r.id)
                        }, 'Квитанция')
                    )
                );
                fragment.appendChild(tr);
            });

            this.dom.historyBody.appendChild(fragment);
        } catch (e) {
            console.warn('History load error', e);
        }
    },

    // --- ЛОГИКА ВВОДА ПОКАЗАНИЙ ---

    validate() {
        let isValid = true;

        const check = (key, prevVal) => {
            const input = this.dom.inputs[key];
            const error = this.dom.errors[key];
            const card = this.dom.cards[key];
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
        this.dom.btnSubmit.disabled = !isValid;
        return isValid;
    },

    async handleSubmit(e) {
        e.preventDefault();
        if (!this.validate()) return;

        setLoading(this.dom.btnSubmit, true, 'Расчет...');
        const spinner = document.getElementById('submitBtnSpinner');
        if (spinner) spinner.classList.remove('hide');

        const data = {
            hot_water: parseFloat(this.dom.inputs.hot.value),
            cold_water: parseFloat(this.dom.inputs.cold.value),
            electricity: parseFloat(this.dom.inputs.elect.value)
        };

        try {
            await api.post('/calculate', data);
            toast('Показания успешно сохранены', 'success');
            await this.loadState();
        } catch (e) {
            toast(e.message, 'error');
        } finally {
            setLoading(this.dom.btnSubmit, false, 'Отправить показания');
            if (spinner) spinner.classList.add('hide');
        }
    },

    async downloadReceipt(id) {
        toast('Скачивание квитанции...', 'info');
        await api.download(`/client/receipts/${id}`, `receipt_${id}.pdf`);
    },

    // --- ЛОГИКА ПЕРВИЧНОЙ НАСТРОЙКИ ---

    async skipFirstSetup() {
        setLoading(this.dom.btnFsSkip, true, 'Загрузка...');
        try {
            // Отправляем пустые данные, бэкенд просто переключит флаг is_initial_setup_done
            await api.post('/users/me/setup', {});
            this.dom.fsModal.classList.remove('open');
            toast('Настройка завершена!', 'success');
        } catch (error) {
            toast(error.message, 'error');
            setLoading(this.dom.btnFsSkip, false, 'Оставить как есть');
        }
    },

    async saveFirstSetup(e) {
        e.preventDefault();
        const newLogin = this.dom.fsNewLogin.value.trim();
        const newPassword = this.dom.fsNewPassword.value;

        setLoading(this.dom.btnFsSave, true, 'Сохранение...');
        try {
            const payload = {};
            if (newLogin) payload.new_username = newLogin;
            if (newPassword) payload.new_password = newPassword;

            await api.post('/users/me/setup', payload);

            alert('Ваши данные успешно обновлены. Пожалуйста, войдите в систему с новыми данными.');
            Auth.logout(); // Принудительно выкидываем на логин

        } catch (error) {
            toast(error.message, 'error');
            setLoading(this.dom.btnFsSave, false, 'Сохранить новые данные');
        }
    },

    // --- ЛОГИКА СМЕНЫ ПАРОЛЯ ИЗ ПРОФИЛЯ ---

    async handleChangePassword(e) {
        e.preventDefault();

        const oldPass = this.dom.cpOld.value;
        const newPass = this.dom.cpNew.value;
        const newPassConfirm = this.dom.cpNewConfirm.value;

        if (newPass !== newPassConfirm) {
            toast('Новые пароли не совпадают!', 'error');
            return;
        }

        setLoading(this.dom.btnCp, true, 'Обновление...');
        try {
            await api.post('/users/me/change-password', {
                old_password: oldPass,
                new_password: newPass
            });

            toast('Пароль успешно изменен!', 'success');
            this.dom.cpForm.reset();
        } catch (error) {
            toast(error.message, 'error');
        } finally {
            setLoading(this.dom.btnCp, false, 'Обновить пароль');
        }
    }
};