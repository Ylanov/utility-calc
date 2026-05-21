// static/js/modules/manual.js
//
// Ручной ввод показаний за жильца. UI — autocomplete-поиск:
// dropdown появляется ТОЛЬКО при вводе текста (раньше слева висел
// постоянный список 400px который ничего не давал пока пусто).
//
// Клавиатура: ↑↓ — навигация, Enter — выбрать, Esc — закрыть dropdown.
import { api } from '../core/api.js';
import { el, toast, setLoading, escapeHtml } from '../core/dom.js';

const DROPDOWN_LIMIT = 12;
const MIN_QUERY_LEN = 1;
const SEARCH_DEBOUNCE_MS = 220;

export const ManualModule = {
    isInitialized: false,
    state: {
        searchTimer: null,
        selectedUserId: null,
        prevReadings: { hot: 0, cold: 0, elect: 0 },
        // Текущие результаты dropdown и индекс активной строки (для клавиатуры).
        results: [],
        activeIdx: -1,
        // Чтобы не показывать устаревшие результаты, если ответ пришёл после нового запроса.
        searchToken: 0,
        // Гасим blur-handler сразу после клика по dropdown
        // (иначе click не успеет отработать — фокус уйдёт раньше).
        suppressBlur: false,
    },

    init() {
        this.cacheDOM();
        if (!this.isInitialized) {
            this.bindEvents();
            this.isInitialized = true;
        }
        // Без автозагрузки. Dropdown пуст и скрыт, пока админ не наберёт текст.
        this._hideDropdown();
        this._renderSelectedChip(null);
    },

    cacheDOM() {
        this.dom = {
            searchInput: document.getElementById('manualSearchInput'),
            searchClear: document.getElementById('manualSearchClear'),
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

            // Раздельная подача: тоглы и контейнеры групп.
            // См. /api/admin/readings/manual — поля hot/cold/elect стали nullable.
            groupWater: document.getElementById('manGroupWater'),
            groupElect: document.getElementById('manGroupElect'),
            toggleWater: document.getElementById('manToggleWater'),
            toggleElect: document.getElementById('manToggleElect'),

            periodSelect: document.getElementById('manualPeriodSelect'),
            periodWarn: document.getElementById('manualPeriodWarn'),
            btnSubmit: document.getElementById('btnSaveManual')
        };
    },

    // Применяет состояние тогла к группе: визуальный is-off + поля disabled
    // и required=false (чтобы браузер не валидировал пустые при submit).
    _applyGroupState(group) {
        const cfg = {
            water: { toggle: this.dom.toggleWater, container: this.dom.groupWater, inputs: [this.dom.inHot, this.dom.inCold] },
            electricity: { toggle: this.dom.toggleElect, container: this.dom.groupElect, inputs: [this.dom.inElect] },
        }[group];
        if (!cfg || !cfg.toggle || !cfg.container) return;
        const on = !!cfg.toggle.checked;
        cfg.container.classList.toggle('is-off', !on);
        cfg.inputs.forEach(inp => {
            if (!inp) return;
            inp.disabled = !on;
            if (on) {
                inp.setAttribute('required', '');
            } else {
                inp.removeAttribute('required');
            }
        });
    },

    bindEvents() {
        const input = this.dom.searchInput;
        if (input) {
            input.addEventListener('input', (e) => this._onSearchInput(e.target.value));
            input.addEventListener('focus', () => {
                // Если в поле есть текст и есть результаты — показываем dropdown снова.
                if (input.value.trim().length >= MIN_QUERY_LEN && this.state.results.length) {
                    this._showDropdown();
                }
            });
            input.addEventListener('blur', () => {
                // Даём шанс клику по li (mousedown ставит suppressBlur).
                setTimeout(() => {
                    if (this.state.suppressBlur) {
                        this.state.suppressBlur = false;
                        return;
                    }
                    this._hideDropdown();
                }, 120);
            });
            input.addEventListener('keydown', (e) => this._onSearchKey(e));
        }

        if (this.dom.searchClear) {
            this.dom.searchClear.addEventListener('click', () => this._clearSearch());
        }

        // Клик ВНЕ поиска и dropdown — скрывает dropdown.
        document.addEventListener('click', (e) => {
            const dd = this.dom.userList;
            const inp = this.dom.searchInput;
            if (!dd || !inp) return;
            if (dd.hidden) return;
            if (dd.contains(e.target) || inp.contains(e.target)) return;
            this._hideDropdown();
        });

        // Клик по li — выбор. Используем mousedown чтобы успеть до blur input'а.
        if (this.dom.userList) {
            this.dom.userList.addEventListener('mousedown', (e) => {
                const li = e.target.closest('li[data-user-id]');
                if (!li) return;
                this.state.suppressBlur = true;
                const id = parseInt(li.dataset.userId);
                const user = this.state.results.find(u => u.id === id);
                if (user) this.selectUser(user);
            });
        }

        if (this.dom.form) {
            this.dom.form.addEventListener('submit', (e) => this.handleSubmit(e));
        }

        // Авто-замена запятой, auto-format 5+3 на blur. Без визуальной имитации
        // счётчика (для админ-ввода она избыточна — админ вводит цифры из тетради).
        ['inHot', 'inCold', 'inElect'].forEach(key => {
            const inp = this.dom[key];
            if (!inp) return;
            inp.addEventListener('input', () => {
                let v = inp.value.replace(',', '.').replace(/[^\d.]/g, '');
                const firstDot = v.indexOf('.');
                if (firstDot !== -1) {
                    v = v.slice(0, firstDot + 1) + v.slice(firstDot + 1).replace(/\./g, '');
                }
                inp.value = v;
            });
            inp.addEventListener('blur', () => {
                if (inp.dataset.strictFormat !== '5_3') return;
                const raw = (inp.value || '').trim();
                if (!raw) return;
                const m = raw.match(/^(\d{1,5})(?:\.(\d{0,3}))?$/);
                if (!m) return;
                inp.value = m[1].padStart(5, '0') + '.' + (m[2] || '').padEnd(3, '0');
            });
        });

        if (this.dom.periodSelect) {
            this.dom.periodSelect.addEventListener('change', () => this._updatePeriodWarn());
            this._loadPeriods();
        }

        // Тоглы раздельной подачи. По умолчанию обе группы включены —
        // привычное поведение. Пользователь сам решает что подавать.
        this.dom.toggleWater?.addEventListener('change', () => this._applyGroupState('water'));
        this.dom.toggleElect?.addEventListener('change', () => this._applyGroupState('electricity'));
        this._applyGroupState('water');
        this._applyGroupState('electricity');
    },

    // -------- ПОИСК ---------------------------------------------------------

    _onSearchInput(value) {
        const q = value.trim();
        // Кнопка-крестик показывается только когда есть текст.
        if (this.dom.searchClear) this.dom.searchClear.hidden = (q.length === 0);

        clearTimeout(this.state.searchTimer);
        if (q.length < MIN_QUERY_LEN) {
            this._hideDropdown();
            return;
        }
        this.state.searchTimer = setTimeout(() => this.searchUsers(q), SEARCH_DEBOUNCE_MS);
        // Сразу показываем «загрузка», чтобы пользователь видел что мы работаем.
        this._renderDropdownLoading(q);
    },

    _onSearchKey(e) {
        if (e.key === 'Escape') {
            if (!this.dom.userList.hidden) {
                this._hideDropdown();
                e.preventDefault();
            }
            return;
        }
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
            if (this.dom.userList.hidden || !this.state.results.length) return;
            e.preventDefault();
            const max = this.state.results.length - 1;
            const cur = this.state.activeIdx;
            const next = e.key === 'ArrowDown'
                ? (cur < max ? cur + 1 : 0)
                : (cur > 0 ? cur - 1 : max);
            this._setActiveIdx(next);
            return;
        }
        if (e.key === 'Enter') {
            if (this.dom.userList.hidden) return;
            const idx = this.state.activeIdx >= 0 ? this.state.activeIdx : 0;
            const user = this.state.results[idx];
            if (user) {
                e.preventDefault();
                this.selectUser(user);
            }
        }
    },

    _clearSearch() {
        this.dom.searchInput.value = '';
        if (this.dom.searchClear) this.dom.searchClear.hidden = true;
        this._hideDropdown();
        this.dom.searchInput.focus();
    },

    async searchUsers(query) {
        const myToken = ++this.state.searchToken;
        try {
            const res = await api.get(`/users?search=${encodeURIComponent(query)}&limit=${DROPDOWN_LIMIT}`);
            if (myToken !== this.state.searchToken) return;  // устаревший ответ
            this.state.results = res.items || [];
            this.state.activeIdx = this.state.results.length ? 0 : -1;
            this._renderDropdownResults(query);
        } catch (e) {
            if (myToken !== this.state.searchToken) return;
            this._renderDropdownError(e.message);
        }
    },

    // -------- DROPDOWN render -----------------------------------------------

    _showDropdown() {
        if (this.dom.userList) this.dom.userList.hidden = false;
    },

    _hideDropdown() {
        if (this.dom.userList) {
            this.dom.userList.hidden = true;
            this.state.activeIdx = -1;
        }
    },

    _renderDropdownLoading(query) {
        this.dom.userList.innerHTML = '';
        const li = el('li', { class: 'is-loading' },
            '🔎 Ищу «' + query + '»…'
        );
        this.dom.userList.appendChild(li);
        this._showDropdown();
    },

    _renderDropdownError(message) {
        this.dom.userList.innerHTML = '';
        const li = el('li', { class: 'is-empty', style: { color: 'var(--danger-color)' } },
            'Ошибка: ' + message
        );
        this.dom.userList.appendChild(li);
        this._showDropdown();
    },

    _renderDropdownResults(query) {
        this.dom.userList.innerHTML = '';
        if (!this.state.results.length) {
            this.dom.userList.appendChild(
                el('li', { class: 'is-empty' }, 'Ничего не найдено')
            );
            this._showDropdown();
            return;
        }
        this.state.results.forEach((user, idx) => {
            const li = document.createElement('li');
            li.setAttribute('role', 'option');
            li.dataset.userId = String(user.id);
            if (idx === this.state.activeIdx) li.classList.add('is-active');

            const address = user.room
                ? `${user.room.dormitory_name} / ком. ${user.room.room_number}`
                : 'Без адреса (не привязан к комнате)';

            // Подсветка совпадения в username (case-insensitive).
            const nameHtml = this._highlight(user.username, query);
            li.innerHTML =
                `<strong>${nameHtml}</strong>` +
                `<span class="addr">${escapeHtml(address)}</span>`;
            this.dom.userList.appendChild(li);
        });
        this._showDropdown();
    },

    _setActiveIdx(idx) {
        this.state.activeIdx = idx;
        Array.from(this.dom.userList.children).forEach((li, i) => {
            li.classList.toggle('is-active', i === idx);
        });
        const activeLi = this.dom.userList.children[idx];
        if (activeLi && activeLi.scrollIntoView) {
            activeLi.scrollIntoView({ block: 'nearest' });
        }
    },

    // Подсветка совпадающей подстроки. Возвращает HTML с <mark> вокруг матча.
    _highlight(text, query) {
        const safe = escapeHtml(text || '');
        if (!query) return safe;
        const qLower = query.toLowerCase();
        const tLower = safe.toLowerCase();
        const idx = tLower.indexOf(qLower);
        if (idx === -1) return safe;
        const end = idx + query.length;
        return safe.slice(0, idx) + '<mark>' + safe.slice(idx, end) + '</mark>' + safe.slice(end);
    },

    // -------- ВЫБОР ЖИЛЬЦА --------------------------------------------------

    async selectUser(user) {
        this.state.selectedUserId = user.id;
        this.dom.inId.value = user.id;
        this._renderSelectedChip(user);
        this._hideDropdown();

        // Очищаем поиск — пользователь выбран, поле должно быть «пустым приглашением».
        this.dom.searchInput.value = '';
        if (this.dom.searchClear) this.dom.searchClear.hidden = true;

        // Разблокируем форму
        this.dom.formCard.style.opacity = '1';
        this.dom.formCard.style.pointerEvents = 'auto';
        this.dom.form.reset();

        // Фокус на первое поле — админ сразу печатает показания.
        this.dom.inHot.focus();

        // Загружаем состояние счётчиков для пользователя.
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
            toast('Ошибка получения истории (возможно, жилец не привязан к комнате): ' + e.message, 'error');
            this.dom.formCard.style.opacity = '0.5';
            this.dom.formCard.style.pointerEvents = 'none';
        }
    },

    _renderSelectedChip(user) {
        if (!this.dom.lblSelectedUser) return;
        if (!user) {
            this.dom.lblSelectedUser.classList.remove('has-user');
            this.dom.lblSelectedUser.innerHTML = '<i class="fa-regular fa-user"></i> Жилец не выбран';
            return;
        }
        this.dom.lblSelectedUser.classList.add('has-user');
        const addr = user.room
            ? ` · ${escapeHtml(user.room.dormitory_name)}/${escapeHtml(String(user.room.room_number))}`
            : '';
        this.dom.lblSelectedUser.innerHTML =
            `<i class="fa-solid fa-circle-check"></i> ${escapeHtml(user.username)}${addr}` +
            ` <button type="button" class="change-btn" id="manualChangeUserBtn">сменить</button>`;
        // Кнопка «сменить» — фокус возвращается на поиск.
        const changeBtn = document.getElementById('manualChangeUserBtn');
        if (changeBtn) {
            changeBtn.addEventListener('click', () => {
                this._resetSelection();
                this.dom.searchInput.focus();
            });
        }
    },

    _resetSelection() {
        this.state.selectedUserId = null;
        this.dom.inId.value = '';
        this._renderSelectedChip(null);
        this.dom.formCard.style.opacity = '0.5';
        this.dom.formCard.style.pointerEvents = 'none';
        this.dom.alertDraft.style.display = 'none';
        this.dom.form.reset();
        // Form.reset() сбросит чекбоксы к defaultChecked=true — но это не
        // триггерит change, поэтому пересчитываем визуальное состояние.
        if (this.dom.toggleWater) this.dom.toggleWater.checked = true;
        if (this.dom.toggleElect) this.dom.toggleElect.checked = true;
        this._applyGroupState('water');
        this._applyGroupState('electricity');
    },

    // -------- ПЕРИОД --------------------------------------------------------

    async _loadPeriods() {
        if (!this.dom.periodSelect) return;
        try {
            const periods = await api.get('/admin/periods/history');
            const items = Array.isArray(periods) ? periods : (periods.items || []);
            items.sort((a, b) => {
                if (a.is_active && !b.is_active) return -1;
                if (!a.is_active && b.is_active) return 1;
                return (b.id || 0) - (a.id || 0);
            });
            const cur = this.dom.periodSelect.querySelector('option[value=""]');
            this.dom.periodSelect.innerHTML = '';
            if (cur) this.dom.periodSelect.appendChild(cur);
            items.forEach(p => {
                const opt = document.createElement('option');
                opt.value = String(p.id);
                opt.textContent = p.name + (p.is_active ? ' (активный)' : ' (закрытый)');
                this.dom.periodSelect.appendChild(opt);
            });
            this._updatePeriodWarn();
        } catch (e) {
            console.warn('manual: не удалось загрузить периоды:', e.message);
        }
    },

    _updatePeriodWarn() {
        if (!this.dom.periodWarn || !this.dom.periodSelect) return;
        const v = this.dom.periodSelect.value;
        const opt = this.dom.periodSelect.options[this.dom.periodSelect.selectedIndex];
        const isPast = v && opt && !opt.textContent.includes('(активный)');
        this.dom.periodWarn.style.display = isPast ? 'block' : 'none';
    },

    // -------- SUBMIT --------------------------------------------------------

    validate() {
        // Раздельная подача: проверяем только включённые группы.
        const waterOn = !!this.dom.toggleWater?.checked;
        const electOn = !!this.dom.toggleElect?.checked;

        if (!waterOn && !electOn) {
            toast('Включите хотя бы одну группу (вода или электричество)', 'error');
            return false;
        }

        if (waterOn) {
            const h = parseFloat(this.dom.inHot.value);
            const c = parseFloat(this.dom.inCold.value);
            if (isNaN(h) || isNaN(c)) {
                toast('Заполните оба значения воды (ГВС и ХВС)', 'error');
                return false;
            }
            if (h < this.state.prevReadings.hot || c < this.state.prevReadings.cold) {
                toast('Новые показания воды не могут быть меньше предыдущих', 'error');
                return false;
            }
        }
        if (electOn) {
            const e = parseFloat(this.dom.inElect.value);
            if (isNaN(e)) {
                toast('Заполните показания электричества', 'error');
                return false;
            }
            if (e < this.state.prevReadings.elect) {
                toast('Показания электричества не могут быть меньше предыдущих', 'error');
                return false;
            }
        }
        return true;
    },

    async handleSubmit(e) {
        e.preventDefault();
        if (!this.state.selectedUserId) return toast('Выберите жильца', 'error');
        if (!this.validate()) return;

        setLoading(this.dom.btnSubmit, true, 'Сохранение...');

        // Раздельная подача — отправляем только включённые поля.
        // Сервер (admin_readings_manual.py) принимает hot/cold/elect как
        // Optional и проверяет пару «вода-вместе» + «хоть что-то».
        const waterOn = !!this.dom.toggleWater?.checked;
        const electOn = !!this.dom.toggleElect?.checked;
        const payload = {
            user_id: parseInt(this.state.selectedUserId),
        };
        if (waterOn) {
            payload.hot_water = parseFloat(this.dom.inHot.value);
            payload.cold_water = parseFloat(this.dom.inCold.value);
        }
        if (electOn) {
            payload.electricity = parseFloat(this.dom.inElect.value);
        }
        const pid = this.dom.periodSelect?.value;
        if (pid) payload.period_id = parseInt(pid);

        try {
            await api.post('/admin/readings/manual', payload);
            toast('Показания успешно сохранены (Черновик)', 'success');

            // Сбрасываем форму и готовим к следующему жильцу.
            this._resetSelection();
            this.dom.searchInput.focus();

        } catch (err) {
            toast(err.message, 'error');
        } finally {
            setLoading(this.dom.btnSubmit, false, '💾 Сохранить показания (Черновик)');
        }
    }
};
