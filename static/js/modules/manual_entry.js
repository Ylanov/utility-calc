// static/js/modules/manual_entry.js
//
// Модалка «Ручной ввод показаний» — вызывается из Реестра показаний
// (кнопка «➕ Ручной ввод»). Перенос строчного мульти-ввода из вкладки
// «Операции» (бывший manual.js, 2026-07-15): админ ищет жильца → выбор
// ДОБАВЛЯЕТ его карточкой. У каждого — три месяца (выбранный + 2
// предыдущих) × ГВС/ХВС/электр. «Утвердить» сохраняет заполненные месяцы
// хронологически (от старого к новому — prev пересчитывается цепочкой).
//
// Периоды приходят от реестра (/api/admin/registry → periods, уже
// отсортированы бэкендом по period_chron_key, свежие первыми) — свой
// JS-парсер русских месяцев больше не нужен.
//
// Бэкенд: GET /admin/readings/manual-grid-state/{uid}?period_ids=…
//         POST /admin/readings/manual  (вернёт reading_id, auto_approved)
//         POST /admin/approve/{reading_id}  (для активного периода)
import { api } from '../core/api.js';
import { el, toast, escapeHtml, showConfirm } from '../core/dom.js';

const DROPDOWN_LIMIT = 12;
const MIN_QUERY_LEN = 1;
const SEARCH_DEBOUNCE_MS = 220;

export const ManualEntryModal = {
    isBuilt: false,
    state: {
        searchTimer: null,
        results: [],
        activeIdx: -1,
        searchToken: 0,
        suppressBlur: false,
        periods: [],           // от реестра: [{id, name, is_active}], свежие первыми
        targetPeriods: [],     // [выбранный, prev1, prev2]
        users: [],             // добавленные жильцы [{id, username, room}]
        onSaved: null,         // колбэк реестру (softReload) после утверждения
        savedAny: false,
    },

    // periods — data.periods реестра; defaultPeriodId — выбранный в реестре
    // период (модалка открывается сразу на нём); onSaved — тихое обновление.
    open({ periods, defaultPeriodId, onSaved } = {}) {
        this._build();
        this.state.periods = periods || [];
        this.state.onSaved = onSaved || null;
        this.state.savedAny = false;
        this._fillPeriodSelect(defaultPeriodId);
        this._resolveTargets();
        this._updatePeriodWarn();
        this._hideDropdown();
        this.dom.overlay.classList.add('open');
        this.dom.searchInput.value = '';
        this.dom.searchInput.focus();
    },

    async close() {
        if (!this.isBuilt) return;
        // Несохранённый ввод (значение отличается от отрендеренного) —
        // подтверждаем закрытие, чтобы случайный Escape/клик мимо окна
        // не стёр набранное молча.
        if (this._hasUnsaved()) {
            const ok = await showConfirm(
                'Закрыть ручной ввод? Заполненные, но НЕ утверждённые показания будут потеряны.',
                { title: 'Есть несохранённый ввод', confirmText: 'Закрыть' });
            if (!ok) return;
        }
        this.dom.overlay.classList.remove('open');
        // Карточки не переживают закрытие — следующий ввод начинается чисто.
        this.state.users = [];
        if (this.dom.grid) this.dom.grid.innerHTML = '';
        this._updateEmpty();
        if (this.state.savedAny && this.state.onSaved) this.state.onSaved();
    },

    // Есть ли ввод, не совпадающий с последним отрендеренным состоянием
    // (после успешного сохранения карточка перерисовывается — defaultValue
    // обновляется, и сохранённые значения «чистые»).
    _hasUnsaved() {
        if (!this.dom?.grid) return false;
        return Array.from(this.dom.grid.querySelectorAll('input[data-meter]'))
            .some(i => i.value !== i.defaultValue);
    },

    _build() {
        if (this.isBuilt) { this.cacheDOM(); return; }
        const ov = document.createElement('div');
        ov.id = 'manualEntryOverlay';
        ov.className = 'modal-overlay';
        ov.innerHTML = `
            <div class="modal-window" style="max-width:860px; width:100%;">
                <div class="modal-header">
                    <h3><i class="fa-solid fa-pen-to-square"></i> Ручной ввод показаний</h3>
                    <button class="close-btn close-icon" data-me-close>&times;</button>
                </div>
                <div class="modal-body" style="max-height:72vh; overflow:auto;">
                    <div style="display:flex; gap:12px; flex-wrap:wrap; align-items:flex-end; margin-bottom:6px;">
                        <div class="form-group" style="margin:0; min-width:190px;">
                            <label style="font-size:13px; font-weight:600;">
                                <i class="fa-regular fa-calendar"></i> Месяц (период)
                            </label>
                            <select id="manualPeriodSelect" style="font-size:14px;"></select>
                        </div>
                        <div class="manual-search" style="flex:1; min-width:260px;">
                            <i class="fa-solid fa-magnifying-glass manual-search-icon"></i>
                            <input type="text" id="manualSearchInput" class="manual-search-input"
                                   placeholder="Найди жильца — логин, ФИО или комната → добавится карточкой ниже"
                                   autocomplete="off" aria-label="Поиск жильца"
                                   aria-controls="manualUserList" aria-haspopup="listbox">
                            <button type="button" id="manualSearchClear" class="manual-search-clear" title="Очистить" hidden>
                                <i class="fa-solid fa-xmark"></i>
                            </button>
                            <ul id="manualUserList" class="manual-search-dropdown" role="listbox" hidden></ul>
                        </div>
                    </div>
                    <small id="manualPeriodWarn" class="hint-text" style="display:none; color:#b45309; margin:0 0 8px; font-size:11.5px;">
                        ⚠️ Выбран <b>прошлый период</b> — ввод за закрытый месяц утверждается сразу.
                    </small>
                    <div style="font-size:12px; color:var(--text-secondary); margin:6px 0 10px;">
                        Каждый добавленный жилец — карточка с тремя месяцами (выбранный + 2 предыдущих).
                        Заполни показания и нажми «Утвердить» у каждого — они появятся в реестре.
                    </div>
                    <div id="manualGrid"></div>
                    <div id="manualGridEmpty" style="text-align:center; padding:28px; color:var(--text-secondary); border:1px dashed var(--border-color); border-radius:10px;">
                        <i class="fa-solid fa-users-viewfinder" style="font-size:28px; opacity:.4;"></i>
                        <div style="margin-top:8px;">Никто не добавлен. Найди жильца в поиске — он появится здесь карточкой.</div>
                    </div>
                </div>
            </div>`;
        document.body.appendChild(ov);
        this.isBuilt = true;
        this.cacheDOM();
        this.bindEvents();
    },

    cacheDOM() {
        this.dom = {
            overlay: document.getElementById('manualEntryOverlay'),
            searchInput: document.getElementById('manualSearchInput'),
            searchClear: document.getElementById('manualSearchClear'),
            userList: document.getElementById('manualUserList'),
            periodSelect: document.getElementById('manualPeriodSelect'),
            periodWarn: document.getElementById('manualPeriodWarn'),
            grid: document.getElementById('manualGrid'),
            empty: document.getElementById('manualGridEmpty'),
        };
    },

    bindEvents() {
        // Закрытие: крестик, mousedown ПО подложке (click ловил бы и
        // «drag из инпута с отпусканием над подложкой»), Escape (когда
        // dropdown закрыт).
        this.dom.overlay.addEventListener('mousedown', (e) => {
            if (e.target === this.dom.overlay) this.close();
        });
        this.dom.overlay.addEventListener('click', (e) => {
            if (e.target.closest('[data-me-close]')) this.close();
        });
        document.addEventListener('keydown', (e) => {
            if (e.key !== 'Escape' || !this.dom.overlay.classList.contains('open')) return;
            if (!this.dom.userList.hidden) return; // Escape сперва закрывает dropdown (обработчик инпута)
            this.close();
        });

        const input = this.dom.searchInput;
        input.addEventListener('input', (e) => this._onSearchInput(e.target.value));
        input.addEventListener('focus', () => {
            if (input.value.trim().length >= MIN_QUERY_LEN && this.state.results.length) this._showDropdown();
        });
        input.addEventListener('blur', () => {
            setTimeout(() => {
                if (this.state.suppressBlur) { this.state.suppressBlur = false; return; }
                this._hideDropdown();
            }, 120);
        });
        input.addEventListener('keydown', (e) => this._onSearchKey(e));
        this.dom.searchClear.addEventListener('click', () => this._clearSearch());

        this.dom.userList.addEventListener('mousedown', (e) => {
            const li = e.target.closest('li[data-user-id]');
            if (!li) return;
            this.state.suppressBlur = true;
            const id = parseInt(li.dataset.userId, 10);
            const user = this.state.results.find(u => u.id === id);
            if (user) this._addUser(user);
        });

        this.dom.periodSelect.addEventListener('change', () => {
            this._resolveTargets();
            this._updatePeriodWarn();
            this._renderAll();
        });

        // Делегирование действий по карточкам (утвердить / убрать).
        this.dom.grid.addEventListener('click', (e) => {
            const card = e.target.closest('[data-uid]');
            if (!card) return;
            const uid = parseInt(card.dataset.uid, 10);
            if (e.target.closest('[data-act="remove"]')) this._removeUser(uid);
            else if (e.target.closest('[data-act="approve"]')) this._approveUser(card, uid);
        });
        // Нормализация ввода (запятая → точка, только цифры и одна точка).
        this.dom.grid.addEventListener('input', (e) => {
            const inp = e.target;
            if (!inp.matches('input[data-meter]')) return;
            let v = inp.value.replace(',', '.').replace(/[^\d.]/g, '');
            const dot = v.indexOf('.');
            if (dot !== -1) v = v.slice(0, dot + 1) + v.slice(dot + 1).replace(/\./g, '');
            inp.value = v;
        });
    },

    // -------- ПЕРИОДЫ -------------------------------------------------------

    _fillPeriodSelect(defaultPeriodId) {
        const sel = this.dom.periodSelect;
        sel.innerHTML = '';
        // periods от реестра уже отсортированы (свежие первыми, активный помечен).
        this.state.periods.forEach(p => {
            const opt = document.createElement('option');
            opt.value = String(p.id);
            opt.textContent = p.name + (p.is_active ? ' (активный)' : '');
            sel.appendChild(opt);
        });
        const def = defaultPeriodId != null ? String(defaultPeriodId)
            : String((this.state.periods.find(p => p.is_active) || this.state.periods[0] || {}).id || '');
        if (def) sel.value = def;
    },

    // Выбранный период + 2 предыдущих СУЩЕСТВУЮЩИХ (periods отсортированы
    // по убыванию хронологии — предыдущие просто следующие в списке).
    _resolveTargets() {
        const periods = this.state.periods || [];
        if (!periods.length) { this.state.targetPeriods = []; return; }
        const selId = this.dom.periodSelect?.value;
        let idx = periods.findIndex(p => String(p.id) === String(selId));
        if (idx === -1) idx = Math.max(0, periods.findIndex(p => p.is_active));
        this.state.targetPeriods = periods.slice(idx, idx + 3);   // [выбр, prev1, prev2]
    },

    _updatePeriodWarn() {
        const sel = this.state.targetPeriods[0];
        this.dom.periodWarn.style.display = (sel && !sel.is_active) ? 'block' : 'none';
    },

    // -------- ПОИСК (autocomplete) -----------------------------------------

    _onSearchInput(value) {
        const q = value.trim();
        this.dom.searchClear.hidden = (q.length === 0);
        clearTimeout(this.state.searchTimer);
        if (q.length < MIN_QUERY_LEN) { this._hideDropdown(); return; }
        this.state.searchTimer = setTimeout(() => this._searchUsers(q), SEARCH_DEBOUNCE_MS);
        this._renderDropdownLoading(q);
    },

    _onSearchKey(e) {
        if (e.key === 'Escape') {
            if (!this.dom.userList.hidden) { this._hideDropdown(); e.preventDefault(); e.stopPropagation(); }
            return;
        }
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
            if (this.dom.userList.hidden || !this.state.results.length) return;
            e.preventDefault();
            const max = this.state.results.length - 1, cur = this.state.activeIdx;
            const next = e.key === 'ArrowDown' ? (cur < max ? cur + 1 : 0) : (cur > 0 ? cur - 1 : max);
            this._setActiveIdx(next);
            return;
        }
        if (e.key === 'Enter') {
            if (this.dom.userList.hidden) return;
            const idx = this.state.activeIdx >= 0 ? this.state.activeIdx : 0;
            const user = this.state.results[idx];
            if (user) { e.preventDefault(); this._addUser(user); }
        }
    },

    _clearSearch() {
        this.dom.searchInput.value = '';
        this.dom.searchClear.hidden = true;
        this._hideDropdown();
        this.dom.searchInput.focus();
    },

    async _searchUsers(query) {
        const myToken = ++this.state.searchToken;
        try {
            const res = await api.get(`/users?search=${encodeURIComponent(query)}&limit=${DROPDOWN_LIMIT}`);
            if (myToken !== this.state.searchToken) return;
            this.state.results = res.items || [];
            this.state.activeIdx = this.state.results.length ? 0 : -1;
            this._renderDropdownResults(query);
        } catch (e) {
            if (myToken !== this.state.searchToken) return;
            this._renderDropdownError(e.message);
        }
    },

    _showDropdown() { this.dom.userList.hidden = false; },
    _hideDropdown() { if (this.dom?.userList) { this.dom.userList.hidden = true; this.state.activeIdx = -1; } },

    _renderDropdownLoading(query) {
        this.dom.userList.innerHTML = '';
        this.dom.userList.appendChild(el('li', { class: 'is-loading' }, '🔎 Ищу «' + query + '»…'));
        this._showDropdown();
    },
    _renderDropdownError(message) {
        this.dom.userList.innerHTML = '';
        this.dom.userList.appendChild(el('li', { class: 'is-empty', style: { color: 'var(--danger-color)' } }, 'Ошибка: ' + message));
        this._showDropdown();
    },
    _renderDropdownResults(query) {
        this.dom.userList.innerHTML = '';
        if (!this.state.results.length) {
            this.dom.userList.appendChild(el('li', { class: 'is-empty' }, 'Ничего не найдено'));
            this._showDropdown();
            return;
        }
        this.state.results.forEach((user, idx) => {
            const li = document.createElement('li');
            li.setAttribute('role', 'option');
            li.dataset.userId = String(user.id);
            if (idx === this.state.activeIdx) li.classList.add('is-active');
            const added = this.state.users.some(u => u.id === user.id);
            const address = user.room
                ? `${user.room.dormitory_name} / ком. ${user.room.room_number}`
                : 'Без адреса (не привязан к комнате)';
            li.innerHTML = `<strong>${this._highlight(user.username, query)}</strong>` +
                `<span class="addr">${escapeHtml(address)}${added ? ' · уже добавлен' : ''}</span>`;
            this.dom.userList.appendChild(li);
        });
        this._showDropdown();
    },
    _setActiveIdx(idx) {
        this.state.activeIdx = idx;
        Array.from(this.dom.userList.children).forEach((li, i) => li.classList.toggle('is-active', i === idx));
        const a = this.dom.userList.children[idx];
        if (a && a.scrollIntoView) a.scrollIntoView({ block: 'nearest' });
    },
    _highlight(text, query) {
        const safe = escapeHtml(text || '');
        if (!query) return safe;
        const idx = safe.toLowerCase().indexOf(query.toLowerCase());
        if (idx === -1) return safe;
        const end = idx + query.length;
        return safe.slice(0, idx) + '<mark>' + safe.slice(idx, end) + '</mark>' + safe.slice(end);
    },

    // -------- ДОБАВЛЕНИЕ / РЕНДЕР КАРТОЧЕК ----------------------------------

    _updateEmpty() {
        this.dom.empty.style.display = this.state.users.length ? 'none' : '';
    },

    async _addUser(user) {
        this.dom.searchInput.value = '';
        this.dom.searchClear.hidden = true;
        this._hideDropdown();

        // Без комнаты ручной ввод показаний невозможен (бэкенд тоже отклонит).
        if (!user.room) {
            toast('Жилец не привязан к комнате — ручной ввод недоступен', 'warning');
            this.dom.searchInput.focus();
            return;
        }
        if (this.state.users.some(u => u.id === user.id)) {
            const ex = this.dom.grid.querySelector(`[data-uid="${user.id}"]`);
            if (ex) { ex.style.transition = 'background .2s'; ex.style.background = '#fef9c3'; setTimeout(() => { ex.style.background = ''; }, 600); ex.scrollIntoView({ block: 'nearest' }); }
            toast('Жилец уже в списке', 'info');
            this.dom.searchInput.focus();
            return;
        }
        this.state.users.push({ id: user.id, username: user.username, room: user.room });
        this._updateEmpty();
        await this._renderCard(user.id);
        this.dom.searchInput.focus();
    },

    _removeUser(uid) {
        this.state.users = this.state.users.filter(u => u.id !== uid);
        const card = this.dom.grid.querySelector(`[data-uid="${uid}"]`);
        if (card) card.remove();
        this._updateEmpty();
    },

    async _renderAll() {
        if (!this.state.users.length) return;
        for (const u of this.state.users) await this._renderCard(u.id);
    },

    async _renderCard(uid) {
        const targets = this.state.targetPeriods;
        if (!targets.length) return;
        const ids = targets.map(p => p.id).join(',');

        let gs;
        try {
            gs = await api.get(`/admin/readings/manual-grid-state/${uid}?period_ids=${ids}`);
        } catch (e) {
            toast('Ошибка загрузки состояния: ' + e.message, 'error');
            return;
        }
        // Карточку могли убрать (или модалку закрыть), пока грузился
        // grid-state — не рисуем «зомби» поверх пустого состояния.
        if (!this.state.users.some(u => u.id === uid)) return;

        const esc = escapeHtml;
        const meter = { hw: gs.has_hw_meter !== false, cw: gs.has_cw_meter !== false, el: gs.has_el_meter !== false };
        // Вода подаётся ПАРОЙ (ГВС+ХВС, домен + бэкенд-валидация) — гейтим её
        // как единицу: клетки воды открыты, пока есть хотя бы один водяной
        // счётчик, иначе комната с одним счётчиком попадает в тупик
        // («заполните оба», а вторая клетка заблокирована).
        const waterOn = meter.hw || meter.cw;
        const noMeters = !waterOn && !meter.el;
        const pById = {};
        (gs.periods || []).forEach(p => { pById[p.period_id] = p; });

        const area = gs.room?.apartment_area ? Number(gs.room.apartment_area).toFixed(1) : '?';
        const residents = gs.room?.total_room_residents ?? 1;
        const singles = gs.room?.is_singles_apartment
            ? ' <span style="background:#fef3c7;color:#92400e;padding:1px 6px;border-radius:8px;font-size:10px;font-weight:600;">хол.</span>' : '';
        const addr = gs.room ? `${esc(gs.room.dormitory_name || '')} ком.${esc(String(gs.room.room_number || ''))}` : 'без комнаты';

        const cell = (m, on, prevVal, curVal) => {
            if (!on) return '<td style="text-align:center; color:var(--text-tertiary);" title="У помещения нет этого счётчика">—</td>';
            const pv = (prevVal == null ? '0' : String(prevVal));
            return `<td style="padding:4px 8px; text-align:center;">
                <input type="text" inputmode="decimal" data-meter="${m}" value="${curVal != null ? esc(String(curVal)) : ''}"
                       placeholder="${esc(pv)}"
                       style="width:100%; max-width:220px; box-sizing:border-box; padding:7px 8px; font-family:monospace; font-size:14px; text-align:center; border:1px solid var(--border-color); border-radius:6px;">
                <div style="font-size:10px; color:var(--text-tertiary); margin-top:2px;">пред ${esc(pv)}</div>
            </td>`;
        };

        let rows = '';
        targets.forEach(t => {
            const p = pById[t.id] || {};
            const okBadge = p.is_approved ? ' <span title="уже утверждено" style="color:#16a34a;">✓</span>' : '';
            const actBadge = t.is_active ? ' <span style="color:#2563eb; font-size:10px;">актив.</span>' : '';
            rows += `<tr data-pid="${t.id}" data-active="${t.is_active ? 1 : 0}">
                <td style="padding:5px 8px; white-space:nowrap; font-weight:500;">${esc(t.name)}${actBadge}${okBadge}</td>
                ${cell('hot', waterOn, p.prev_hot, p.cur_hot)}
                ${cell('cold', waterOn, p.prev_cold, p.cur_cold)}
                ${cell('elect', meter.el, p.prev_elect, p.cur_elect)}
            </tr>`;
        });

        const card = document.createElement('div');
        card.dataset.uid = String(uid);
        card.style.cssText = 'border:1px solid var(--border-color); border-radius:10px; padding:12px 14px; margin-bottom:12px; background:var(--bg-card);';
        card.innerHTML = `
            <div style="display:flex; justify-content:space-between; align-items:flex-start; gap:10px; margin-bottom:8px; flex-wrap:wrap;">
                <div>
                    <strong style="font-size:14px;">${esc(gs.username || '')}</strong>
                    <span style="color:var(--text-secondary); font-size:12px;"> · ${addr} · ${area}м² · ${esc(String(residents))}чел${singles}</span>
                </div>
                <div style="display:flex; gap:6px;">
                    <button type="button" class="action-btn success-btn" data-act="approve" style="padding:5px 12px; font-size:13px;"><i class="fa-solid fa-check"></i> Утвердить</button>
                    <button type="button" class="icon-btn" data-act="remove" title="Убрать из списка" style="color:var(--danger-color);"><i class="fa-solid fa-xmark"></i></button>
                </div>
            </div>
            ${noMeters ? '<div style="font-size:12px; color:#92400e; background:#fef3c7; border-radius:6px; padding:6px 10px;">У помещения нет счётчиков — ручной ввод показаний недоступен.</div>' : `
            <div style="overflow-x:auto;">
                <table style="width:100%; border-collapse:collapse; font-size:13px; table-layout:fixed;">
                    <colgroup><col style="width:16%"><col><col><col></colgroup>
                    <thead><tr style="color:var(--text-secondary); font-size:11px; text-align:center;">
                        <th style="text-align:left; padding:3px 8px;">Месяц</th>
                        <th>🔥 ГВС</th><th>💧 ХВС</th><th>⚡ Электр.</th>
                    </tr></thead>
                    <tbody>${rows}</tbody>
                </table>
            </div>`}
            <div data-msg style="font-size:12px; margin-top:6px;"></div>`;

        const existing = this.dom.grid.querySelector(`[data-uid="${uid}"]`);
        if (existing) existing.replaceWith(card); else this.dom.grid.appendChild(card);
    },

    // -------- УТВЕРЖДЕНИЕ ---------------------------------------------------

    async _approveUser(card, uid) {
        const msg = card.querySelector('[data-msg]');
        const btn = card.querySelector('[data-act="approve"]');
        const rows = Array.from(card.querySelectorAll('tr[data-pid]'));

        const num = (inp) => {
            if (!inp || inp.value.trim() === '') return null;
            const v = parseFloat(inp.value.replace(',', '.'));
            return Number.isFinite(v) ? v : null;
        };

        // Собираем задания по месяцам. Вода — парой; электр — отдельно.
        const jobs = [];
        for (const row of rows) {
            const pid = parseInt(row.dataset.pid, 10);
            const isActive = row.dataset.active === '1';
            const hot = num(row.querySelector('input[data-meter="hot"]'));
            const cold = num(row.querySelector('input[data-meter="cold"]'));
            const elect = num(row.querySelector('input[data-meter="elect"]'));
            const payload = { user_id: uid, period_id: pid };
            let any = false;
            if (hot != null && cold != null) { payload.hot_water = hot; payload.cold_water = cold; any = true; }
            else if (hot != null || cold != null) { msg.textContent = '⚠ Заполните ГВС и ХВС вместе (или оставьте оба пустыми).'; msg.style.color = '#b45309'; return; }
            if (elect != null) { payload.electricity = elect; any = true; }
            if (any) jobs.push({ pid, isActive, payload });
        }
        if (!jobs.length) { msg.textContent = '⚠ Не заполнено ни одного показания.'; msg.style.color = '#b45309'; return; }

        // Раньше — раньше: сохраняем хронологически с самого старого месяца,
        // чтобы следующий подхватил свежий prev. targetPeriods = [выбр, prev1,
        // prev2]; jobs идут в порядке строк (выбр первым) → переворачиваем.
        jobs.reverse();

        btn.disabled = true;
        const orig = btn.innerHTML;
        btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';
        msg.style.color = 'var(--text-secondary)';
        msg.textContent = 'Сохраняю…';
        let done = 0;
        try {
            for (const job of jobs) {
                const res = await api.post('/admin/readings/manual', job.payload);
                // Закрытый период → save уже утверждает. Активный → отдельный approve.
                if (!res.auto_approved && res.reading_id) {
                    await api.post(`/admin/approve/${res.reading_id}`, {
                        hot_correction: 0, cold_correction: 0, electricity_correction: 0, sewage_correction: 0,
                    });
                }
                done++;
                // Сразу, не после всего цикла: при частичном успехе (второй
                // месяц упал валидацией) реестр всё равно должен обновиться.
                this.state.savedAny = true;
            }
            // Модалку могли закрыть, пока шли POST'ы — карточек уже нет,
            // просто обновляем реестр под ней.
            if (!this.dom.overlay.classList.contains('open')) {
                if (this.state.onSaved) this.state.onSaved();
                return;
            }
            // Перерисовываем карточку — подтянутся свежие prev/утв.-галочки —
            // и пишем итог уже на НОВУЮ карточку (старая заменена).
            await this._renderCard(uid);
            const fresh = this.dom.grid.querySelector(`[data-uid="${uid}"]`);
            const fmsg = fresh?.querySelector('[data-msg]');
            if (fmsg) { fmsg.style.color = '#16a34a'; fmsg.textContent = `✓ Утверждено месяцев: ${done}`; }
        } catch (e) {
            msg.style.color = 'var(--danger-color)';
            msg.textContent = 'Ошибка: ' + (e.message || e) + (done ? ` (сохранено месяцев: ${done})` : '');
            btn.disabled = false;
            btn.innerHTML = orig;
        }
    },
};
