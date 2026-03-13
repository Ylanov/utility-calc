// static/js/modules/manual.js
import { api } from '../core/api.js';
import { el, toast, setLoading } from '../core/dom.js';

export const ManualModule = {
    isInitialized: false,
    state: {
        searchTimer: null,
        selectedUserId: null,
        prevReadings: { hot: 0, cold: 0, elect: 0 }
    },

    init() {
        this.cacheDOM();
        if (!this.isInitialized) {
            this.bindEvents();
            this.isInitialized = true;
        }
        // При открытии вкладки загружаем первых 20 пользователей
        this.searchUsers('');
    },

    cacheDOM() {
        this.dom = {
            searchInput: document.getElementById('manualSearchInput'),
            userList: document.getElementById('manualUserList'),
            formCard: document.getElementById('manualFormCard'),
            form: document.getElementById('manualReadingForm'),
            lblSelectedUser: document.getElementById('manualSelectedUser'),
            alertDraft: document.getElementById('manualDraftAlert'),

            inId: document.getElementById('manualUserId'),
            inHot: document.getElementById('manHot'),
            inCold: document.getElementById('manCold'),
            inElect: document.getElementById('manElect'),

            lblPrevHot: document.getElementById('manPrevHot'),
            lblPrevCold: document.getElementById('manPrevCold'),
            lblPrevElect: document.getElementById('manPrevElect'),

            btnSubmit: document.getElementById('btnSaveManual')
        };
    },

    bindEvents() {
        if (this.dom.searchInput) {
            this.dom.searchInput.addEventListener('input', (e) => {
                clearTimeout(this.state.searchTimer);
                this.state.searchTimer = setTimeout(() => {
                    this.searchUsers(e.target.value.trim());
                }, 400);
            });
        }

        if (this.dom.form) {
            this.dom.form.addEventListener('submit', (e) => this.handleSubmit(e));
        }

        // НОВОЕ: Автоматическая замена запятой на точку для скорости ввода с numpad
        ['inHot', 'inCold', 'inElect'].forEach(key => {
            if (this.dom[key]) {
                this.dom[key].addEventListener('input', function() {
                    this.value = this.value.replace(',', '.');
                });
            }
        });
    },

    async searchUsers(query) {
        this.dom.userList.innerHTML = '<li style="padding:15px; text-align:center; color:#888;">Загрузка...</li>';
        try {
            // Используем уже существующий API пользователей (берем только роль user)
            const res = await api.get(`/users?search=${encodeURIComponent(query)}&limit=20`);

            this.dom.userList.innerHTML = '';
            if (res.items.length === 0) {
                this.dom.userList.innerHTML = '<li style="padding:15px; text-align:center; color:#888;">Ничего не найдено</li>';
                return;
            }

            res.items.forEach(user => {
                if (user.role !== 'user') return; // Показываем только жильцов

                const li = el('li', {
                    style: {
                        padding: '12px 15px', borderBottom: '1px solid #e5e7eb', cursor: 'pointer',
                        display: 'flex', flexDirection: 'column', gap: '4px', transition: 'background 0.2s'
                    },
                    onclick: () => this.selectUser(user)
                });

                // Эффект наведения
                li.addEventListener('mouseover', () => li.style.background = '#eff6ff');
                li.addEventListener('mouseout', () => {
                    if (this.state.selectedUserId !== user.id) li.style.background = 'transparent';
                });

                li.appendChild(el('strong', { style: { color: '#1f2937', fontSize: '14px' } }, user.username));
                li.appendChild(el('span', { style: { color: '#6b7280', fontSize: '12px' } }, user.dormitory || 'Без адреса'));

                this.dom.userList.appendChild(li);
            });

        } catch (e) {
            // БЕЗОПАСНЫЙ ВЫВОД ОШИБКИ (Защита от XSS)
            this.dom.userList.innerHTML = '';
            this.dom.userList.appendChild(
                el('li', { style: { padding: '15px', color: 'red', textAlign: 'center' } }, `Ошибка: ${e.message}`)
            );
        }
    },

    async selectUser(user) {
        this.state.selectedUserId = user.id;
        this.dom.inId.value = user.id;
        this.dom.lblSelectedUser.textContent = user.username;

        // Визуальное выделение списка
        Array.from(this.dom.userList.children).forEach(li => li.style.background = 'transparent');
        event.currentTarget.style.background = '#dbeafe';

        // Разблокируем форму
        this.dom.formCard.style.opacity = '1';
        this.dom.formCard.style.pointerEvents = 'auto';
        this.dom.form.reset();

        // Фокус на первое поле для скорости
        this.dom.inHot.focus();

        // Загружаем состояние счетчиков для пользователя
        try {
            const state = await api.get(`/admin/readings/manual-state/${user.id}`);

            this.state.prevReadings = {
                hot: parseFloat(state.prev_hot),
                cold: parseFloat(state.prev_cold),
                elect: parseFloat(state.prev_elect)
            };

            this.dom.lblPrevHot.textContent = state.prev_hot;
            this.dom.lblPrevCold.textContent = state.prev_cold;
            this.dom.lblPrevElect.textContent = state.prev_elect;

            if (state.has_draft) {
                this.dom.alertDraft.style.display = 'block';
                this.dom.inHot.value = state.draft_hot;
                this.dom.inCold.value = state.draft_cold;
                this.dom.inElect.value = state.draft_elect;
            } else {
                this.dom.alertDraft.style.display = 'none';
            }

        } catch (e) {
            toast('Ошибка получения истории: ' + e.message, 'error');
            this.dom.formCard.style.opacity = '0.5';
            this.dom.formCard.style.pointerEvents = 'none';
        }
    },

    validate() {
        const h = parseFloat(this.dom.inHot.value);
        const c = parseFloat(this.dom.inCold.value);
        const e = parseFloat(this.dom.inElect.value);

        if (h < this.state.prevReadings.hot || c < this.state.prevReadings.cold || e < this.state.prevReadings.elect) {
            toast('Новые показания не могут быть меньше предыдущих!', 'error');
            return false;
        }
        return true;
    },

    async handleSubmit(e) {
        e.preventDefault();
        if (!this.state.selectedUserId) return toast('Выберите жильца', 'error');
        if (!this.validate()) return;

        setLoading(this.dom.btnSubmit, true, 'Сохранение...');

        const payload = {
            user_id: parseInt(this.state.selectedUserId),
            hot_water: parseFloat(this.dom.inHot.value),
            cold_water: parseFloat(this.dom.inCold.value),
            electricity: parseFloat(this.dom.inElect.value)
        };

        try {
            await api.post('/admin/readings/manual', payload);
            toast('Показания успешно сохранены (Черновик)', 'success');

            // Если вкладка "Сверка показаний" уже инициализирована, обновим ее данные на фоне
            if (window.ReadingsModule && window.ReadingsModule.table) {
                window.ReadingsModule.table.refresh();
            }

            // Сбрасываем форму
            this.dom.formCard.style.opacity = '0.5';
            this.dom.formCard.style.pointerEvents = 'none';
            this.dom.lblSelectedUser.textContent = 'Не выбран';
            this.state.selectedUserId = null;
            Array.from(this.dom.userList.children).forEach(li => li.style.background = 'transparent');
            this.dom.alertDraft.style.display = 'none';
            this.dom.form.reset();

            // НОВОЕ UX УЛУЧШЕНИЕ: Автоматически возвращаем курсор в строку поиска для следующего жильца!
            this.dom.searchInput.value = '';
            this.dom.searchInput.focus();

        } catch (err) {
            toast(err.message, 'error');
        } finally {
            setLoading(this.dom.btnSubmit, false, '💾 Сохранить показания (Черновик)');
        }
    }
};