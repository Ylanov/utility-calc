// static/js/modules/housing.js
import { api } from '../core/api.js';
import { el, toast, setLoading } from '../core/dom.js';
import { TableController } from '../core/table-controller.js';

function escapeHtml(str) {
    if (str === null || str === undefined) return '';
    const div = document.createElement('div');
    div.textContent = String(str);
    return div.innerHTML;
}

export const HousingModule = {
    table: null,
    isInitialized: false,
    tariffs: [],
    expanded: new Set(),

    async init() {
        this.cacheDOM();

        if (!this.isInitialized) {
            this.bindEvents();
            this.isInitialized = true;
        }

        await Promise.all([
            this.loadDormitories(),
            this.loadTariffs(),
        ]);
        this.initTable();
        this.loadStats();
    },

    cacheDOM() {
        this.dom = {
            dormFilterSelect: document.getElementById('dormFilterSelect'),
            dormList: document.getElementById('dormList'),
            btnOpenAdd: document.getElementById('btnOpenAddRoom'),
            btnRefresh: document.getElementById('btnRefreshRooms'),
            btnExport: document.getElementById('btnExportRooms'),

            // Новые фильтры
            filterOccupancy: document.getElementById('filterOccupancy'),
            filterMissingMeter: document.getElementById('filterMissingMeter'),

            // KPI
            statsHost: document.getElementById('housingStats'),

            // Анализатор жилфонда переехал в «Центр анализа» (см. analyzer.js),
            // этот модуль с ним больше не общается. DOM-ссылок здесь нет.

            // НОВОЕ: Начальные показания — импорт
            btnDownloadReadingsTemplate: document.getElementById('btnDownloadReadingsTemplate'),
            btnImportInitialReadings: document.getElementById('btnImportInitialReadings'),
            importInitialReadingsFile: document.getElementById('importInitialReadingsFile'),
        };

        this.modal = {
            window: document.getElementById('roomModal'),
            title: document.getElementById('roomModalTitle'),
            form: document.getElementById('roomForm'),
            btnClose: document.querySelector('#roomModal .close-btn'),
            btnCancel: document.querySelector('#roomModal .secondary-btn'),
            inputs: {
                id: document.getElementById('roomEditId'),
                dorm: document.getElementById('roomDormitory'),
                num: document.getElementById('roomNumber'),
                area: document.getElementById('roomArea'),
                cap: document.getElementById('roomCapacity'),
                tariff: document.getElementById('roomTariffId'),
                hw: document.getElementById('roomHwSerial'),
                cw: document.getElementById('roomCwSerial'),
                el: document.getElementById('roomElSerial')
            }
        };

        // Замена счетчика
        this.meter = {
            modal: document.getElementById('replaceMeterModal'),
            form: document.getElementById('replaceMeterForm'),
            roomId: document.getElementById('meterRoomId'),
            roomName: document.getElementById('meterRoomName'),
            type: document.getElementById('meterType'),
            newSerial: document.getElementById('meterNewSerial'),
            finalOld: document.getElementById('meterFinalOld'),
            initialNew: document.getElementById('meterInitialNew'),
            btnClose: document.getElementById('btnCloseMeter'),
            btnCancel: document.getElementById('btnCancelMeter'),
            btnSubmit: document.getElementById('btnSubmitMeter')
        };

        // НОВОЕ: Начальные показания — модалка
        this.initial = {
            modal: document.getElementById('initialReadingsModal'),
            form: document.getElementById('initialReadingsForm'),
            roomId: document.getElementById('initialRoomId'),
            roomName: document.getElementById('initialRoomName'),
            hot: document.getElementById('initialHot'),
            cold: document.getElementById('initialCold'),
            elect: document.getElementById('initialElect'),
            btnClose: document.getElementById('btnCloseInitial'),
            btnCancel: document.getElementById('btnCancelInitial'),
            btnSubmit: document.getElementById('btnSubmitInitial'),
        };
    },

    bindEvents() {
        if (this.dom.btnRefresh) this.dom.btnRefresh.addEventListener('click', () => {
            this.table.refresh();
            this.loadStats();
        });

        const refilter = () => {
            this.expanded.clear();
            this.table.state.page = 1;
            this.table.load();
            this.loadStats();
        };

        if (this.dom.dormFilterSelect) this.dom.dormFilterSelect.addEventListener('change', refilter);
        if (this.dom.filterOccupancy) this.dom.filterOccupancy.addEventListener('change', refilter);
        if (this.dom.filterMissingMeter) this.dom.filterMissingMeter.addEventListener('change', refilter);

        if (this.dom.btnExport) {
            this.dom.btnExport.addEventListener('click', () => this.exportExcel());
        }

        if (this.dom.btnOpenAdd) {
            this.dom.btnOpenAdd.addEventListener('click', () => this.openModal());
        }

        if (this.modal.form) {
            this.modal.form.addEventListener('submit', (e) => this.handleSave(e));
        }

        [this.modal.btnClose, this.modal.btnCancel].forEach(btn => {
            if (btn) btn.addEventListener('click', () => {
                this.modal.window.classList.remove('open');
            });
        });

        // Замена счетчика
        if (this.meter.form) {
            this.meter.form.addEventListener('submit', (e) => this.handleMeterSubmit(e));
            this.meter.btnClose.addEventListener('click', (e) => { e.preventDefault(); this.meter.modal.classList.remove('open'); });
            this.meter.btnCancel.addEventListener('click', (e) => { e.preventDefault(); this.meter.modal.classList.remove('open'); });
        }

        // НОВОЕ: Начальные показания — модалка
        if (this.initial.form) {
            this.initial.form.addEventListener('submit', (e) => this.handleInitialSubmit(e));
            if (this.initial.btnClose) this.initial.btnClose.addEventListener('click', (e) => { e.preventDefault(); this.initial.modal.classList.remove('open'); });
            if (this.initial.btnCancel) this.initial.btnCancel.addEventListener('click', (e) => { e.preventDefault(); this.initial.modal.classList.remove('open'); });
        }

        // НОВОЕ: Начальные показания — шаблон и импорт
        if (this.dom.btnDownloadReadingsTemplate) {
            this.dom.btnDownloadReadingsTemplate.addEventListener('click', () => this.downloadReadingsTemplate());
        }
        if (this.dom.btnImportInitialReadings) {
            this.dom.btnImportInitialReadings.addEventListener('click', () => this.importInitialReadings());
        }
    },

    // Блок «Анализатор жилфонда» удалён — переехал в analyzer.js
    // («Операции → Центр анализа → таб Жилфонд»). Backend-endpoint
    // /rooms/analyze остался, его теперь дёргает AnalyzerModule.

    // ==========================================
    // СТАНДАРТНАЯ ЛОГИКА ЖИЛФОНДА
    // ==========================================
    async loadDormitories() {
        try {
            const dorms = await api.get('/rooms/dormitories');
            // ИСПРАВЛЕНО: XSS через название общежития.
            // Раньше подстановка `${d}` в innerHTML была уязвима: если в БД
            // попадёт общежитие с HTML-тегами, любой админ схлопнёт на onerror.
            // Заменили на DOM-конструирование через createElement — браузер
            // сам экранирует текст через textContent.
            const sel = this.dom.dormFilterSelect;
            sel.innerHTML = '';
            sel.appendChild(new Option('Все объекты', ''));
            dorms.forEach(d => sel.appendChild(new Option(String(d), String(d))));

            const list = this.dom.dormList;
            list.innerHTML = '';
            dorms.forEach(d => {
                const opt = document.createElement('option');
                opt.value = String(d);
                list.appendChild(opt);
            });
        } catch (e) {
            toast('Ошибка загрузки списка общежитий', 'error');
        }
    },

    initTable() {
        this.table = new TableController({
            endpoint: '/rooms',
            dom: {
                tableBody: 'roomsTableBody',
                searchInput: 'roomsSearchInput',
                limitSelect: 'roomsLimitSelect',
                prevBtn: 'btnPrevRooms',
                nextBtn: 'btnNextRooms',
                pageInfo: 'roomsPageInfo'
            },
            getExtraParams: () => {
                const params = {};
                if (this.dom.dormFilterSelect?.value) params.dormitory = this.dom.dormFilterSelect.value;
                if (this.dom.filterOccupancy?.value) params.occupancy = this.dom.filterOccupancy.value;
                if (this.dom.filterMissingMeter?.checked) params.missing_meter = 'true';
                return params;
            },
            renderRow: (room) => this.renderRoomRow(room),
        });

        this.table.init();
    },

    renderRoomRow(room) {
        // Заполненность рассчитывается на клиенте из total_room_residents + total_residents.
        // По домену: total_room_residents = вместимость комнаты (мест),
        // residents_count каждого жильца = сколько он платит; резиденты мы тут
        // не получаем (их видно через expand), поэтому в таблице даём подсказку
        // через tint фона и чип с кол-вом мест.
        const cap = Number(room.total_room_residents || 0);
        const missing = !room.hw_meter_serial || !room.cw_meter_serial || !room.el_meter_serial;

        const bgTint = missing ? 'background: #fffbeb;' : '';

        const isOpen = this.expanded.has(room.id);
        const chevron = el('button', {
            class: 'btn-icon',
            style: { background: 'transparent', border: 'none', cursor: 'pointer', fontSize: '13px', color: 'var(--text-secondary)' },
            title: isOpen ? 'Свернуть' : 'Показать жильцов',
            onclick: (e) => { e.stopPropagation(); this.toggleExpand(room); },
        }, isOpen ? '▼' : '▶');

        const row = el('tr', {
            class: 'hover:bg-gray-50 transition-colors',
            'data-room-id': String(room.id),
            style: bgTint ? { background: '#fffbeb' } : {},
        },
            el('td', { class: 'text-center' }, chevron),
            el('td', { class: 'text-gray-500 text-sm' }, `#${room.id}`),
            el('td', { style: { fontWeight: 'bold', color: '#1f2937' } }, room.dormitory_name),
            el('td', { style: { fontWeight: 'bold', color: '#374151' } }, room.room_number),
            el('td', {}, `${Number(room.apartment_area).toFixed(1)} м²`),
            el('td', { class: 'text-center' }, String(cap)),
            el('td', { class: 'text-sm font-mono', style: { color: room.hw_meter_serial ? '#dc2626' : '#9ca3af' } }, room.hw_meter_serial || '—'),
            el('td', { class: 'text-sm font-mono', style: { color: room.cw_meter_serial ? '#2563eb' : '#9ca3af' } }, room.cw_meter_serial || '—'),
            el('td', { class: 'text-sm font-mono', style: { color: room.el_meter_serial ? '#d97706' : '#9ca3af' } }, room.el_meter_serial || '—'),
            el('td', { class: 'text-center' },
                el('button', {
                    class: 'btn-icon', title: 'Начальные показания',
                    style: { marginRight: '5px', background: '#eef2ff', color: '#4338ca', borderColor: '#c7d2fe' },
                    onclick: () => this.openInitialModal(room)
                }, '📊'),
                el('button', {
                    class: 'btn-icon', title: 'Замена счетчика',
                    style: { marginRight: '5px', background: '#f0fdf4', color: '#166534', borderColor: '#bbf7d0' },
                    onclick: () => this.openMeterModal(room)
                }, '🔄'),
                el('button', {
                    class: 'btn-icon btn-edit', title: 'Редактировать',
                    style: { marginRight: '5px' },
                    onclick: () => this.openModal(room)
                }, '✎'),
                el('button', {
                    class: 'btn-icon btn-delete', title: 'Удалить',
                    onclick: () => this.deleteRoom(room.id)
                }, '🗑')
            )
        );

        return row;
    },

    async toggleExpand(room) {
        const tbody = document.getElementById('roomsTableBody');
        if (!tbody) return;
        const row = tbody.querySelector(`tr[data-room-id="${room.id}"]`);
        if (!row) return;

        // Если уже раскрыта — сворачиваем (удаляем detail-строку)
        const nextRow = row.nextElementSibling;
        if (nextRow && nextRow.classList.contains('room-details-row')) {
            nextRow.remove();
            this.expanded.delete(room.id);
            // Перерисовать шеврон
            const btn = row.querySelector('.btn-icon');
            if (btn) btn.textContent = '▶';
            return;
        }

        this.expanded.add(room.id);
        const btn = row.querySelector('.btn-icon');
        if (btn) btn.textContent = '▼';

        // Вставляем строку-заглушку
        const detailRow = document.createElement('tr');
        detailRow.className = 'room-details-row';
        detailRow.innerHTML = `<td colspan="10" style="background:#f9fafb; padding:16px 24px; color:var(--text-secondary);"><i class="fa-solid fa-spinner fa-spin"></i> Загрузка…</td>`;
        row.after(detailRow);

        try {
            const data = await api.get(`/rooms/${room.id}/residents`);
            detailRow.innerHTML = `<td colspan="10" style="background:#f9fafb; padding:14px 24px; border-left:3px solid var(--primary-color);">${this.renderRoomDetails(data)}</td>`;
        } catch (e) {
            detailRow.innerHTML = `<td colspan="10" style="background:#fef2f2; padding:12px 24px; color:var(--danger-color);">Ошибка загрузки: ${escapeHtml(e.message)}</td>`;
        }
    },

    renderRoomDetails(data) {
        const residents = (data.residents || []).map(r => {
            const type = r.resident_type === 'single' ? '👤 Холост.' : '👨‍👩‍👧 Семья';
            const mode = r.billing_mode === 'per_capita' ? '🛏 Койко' : '📊 Счёт.';
            const tariff = r.tariff_name ? `<span style="color:#6366f1;">(${escapeHtml(r.tariff_name)})</span>` : '<span style="color:#9ca3af;">(дефолт)</span>';
            return `
                <li style="padding:6px 0; border-bottom:1px solid #e5e7eb; display:flex; gap:10px; flex-wrap:wrap; align-items:center;">
                    <b style="color:#1f2937;">${escapeHtml(r.username)}</b>
                    <span style="font-size:11px; background:#e0e7ff; color:#3730a3; padding:2px 7px; border-radius:10px;">${type}</span>
                    <span style="font-size:11px; background:#fef3c7; color:#92400e; padding:2px 7px; border-radius:10px;">${mode}</span>
                    <span style="font-size:12px; color:var(--text-secondary);">платят за: <b>${r.residents_count}</b> чел.</span>
                    ${tariff}
                    ${r.workplace ? `<span style="font-size:12px; color:var(--text-secondary);">• ${escapeHtml(r.workplace)}</span>` : ''}
                </li>
            `;
        }).join('');

        const lr = data.last_reading;
        const lrBlock = lr ? `
            <div style="background:white; border:1px solid var(--border-color); border-radius:6px; padding:10px 14px; margin-top:10px; font-size:13px;">
                <b style="color:#1f2937;">📈 Последнее показание</b>
                <span style="color:var(--text-secondary); margin-left:8px;">(${lr.created_at ? new Date(lr.created_at).toLocaleDateString('ru-RU') : '—'})</span>
                <div style="margin-top:6px; display:flex; gap:18px; flex-wrap:wrap;">
                    <span style="color:#dc2626;">🔥 ГВС: <b>${lr.hot_water ?? '—'}</b></span>
                    <span style="color:#2563eb;">💧 ХВС: <b>${lr.cold_water ?? '—'}</b></span>
                    <span style="color:#d97706;">⚡ Электр.: <b>${lr.electricity ?? '—'}</b></span>
                </div>
            </div>
        ` : `<div style="margin-top:10px; color:var(--text-secondary); font-size:12px;">Нет утверждённых показаний.</div>`;

        const emptyList = `<div style="padding:10px 0; color:var(--text-secondary); font-style:italic;">В этой комнате нет зарегистрированных жильцов.</div>`;

        return `
            <div style="font-size:13px;">
                <b style="color:#1f2937; margin-right:10px;">Жильцы (${data.residents?.length || 0}):</b>
                ${residents ? `<ul style="list-style:none; padding:0; margin:8px 0 0;">${residents}</ul>` : emptyList}
                ${lrBlock}
            </div>
        `;
    },

    async loadStats() {
        if (!this.dom.statsHost) return;
        const dormitory = this.dom.dormFilterSelect?.value || '';
        const qs = dormitory ? `?dormitory=${encodeURIComponent(dormitory)}` : '';
        try {
            const s = await api.get(`/rooms/stats${qs}`);
            this.renderStats(s);
        } catch (e) {
            this.dom.statsHost.innerHTML = `<div style="padding:14px; color:var(--danger-color); grid-column:1/-1;">Ошибка загрузки аналитики: ${escapeHtml(e.message)}</div>`;
        }
    },

    renderStats(s) {
        const card = (bg, color, icon, value, label) => `
            <div style="background:${bg}; border-radius:10px; padding:14px 12px; border:1px solid ${color}33;">
                <div style="display:flex; align-items:center; gap:8px; color:${color}; font-size:12px; margin-bottom:4px;">
                    <span style="font-size:16px;">${icon}</span>${label}
                </div>
                <div style="font-size:22px; font-weight:700; color:#111827;">${value}</div>
            </div>
        `;
        this.dom.statsHost.innerHTML = [
            card('#eff6ff', '#2563eb', '🏠', s.total_rooms, 'Всего комнат'),
            card('#f3f4f6', '#6b7280', '🚪', s.empty, 'Пустых'),
            card('#fef9c3', '#a16207', '🟡', s.partial, 'Частичных'),
            card('#ecfdf5', '#10b981', '✅', s.full, 'Полных'),
            card('#fef2f2', '#dc2626', '🔴', s.overcrowded, 'Переполнено'),
            card('#eef2ff', '#4338ca', '👥', `${s.total_residents}/${s.total_capacity}`, `Жильцов / мест (${s.occupancy_pct}%)`),
            card('#fff7ed', '#ea580c', '⚠️', s.missing_meters_count, 'Без счётчика'),
        ].join('');
    },

    async loadTariffs() {
        try {
            this.tariffs = await api.get('/tariffs');
        } catch {
            this.tariffs = [];
        }
    },

    fillTariffSelect(selectedId) {
        const sel = this.modal.inputs.tariff;
        if (!sel) return;
        sel.innerHTML = '<option value="">— Без переопределения —</option>';
        (this.tariffs || []).filter(t => t.is_active).forEach(t => {
            const opt = document.createElement('option');
            opt.value = String(t.id);
            opt.textContent = t.name;
            if (selectedId != null && Number(selectedId) === t.id) opt.selected = true;
            sel.appendChild(opt);
        });
    },

    async exportExcel() {
        const params = new URLSearchParams();
        if (this.dom.dormFilterSelect?.value) params.append('dormitory', this.dom.dormFilterSelect.value);
        if (this.dom.filterOccupancy?.value) params.append('occupancy', this.dom.filterOccupancy.value);
        if (this.dom.filterMissingMeter?.checked) params.append('missing_meter', 'true');
        const qs = params.toString() ? `?${params.toString()}` : '';
        setLoading(this.dom.btnExport, true);
        try {
            await api.download(`/rooms/export${qs}`, `housing_${Date.now()}.xlsx`);
            toast('Экспорт готов', 'success');
        } catch (e) {
            toast('Ошибка экспорта: ' + e.message, 'error');
        } finally {
            setLoading(this.dom.btnExport, false);
        }
    },

    openModal(room = null) {
        this.modal.form.reset();
        if (room) {
            this.modal.title.textContent = 'Редактировать помещение';
            this.modal.inputs.id.value = room.id;
            this.modal.inputs.dorm.value = room.dormitory_name;
            this.modal.inputs.num.value = room.room_number;
            this.modal.inputs.area.value = room.apartment_area;
            this.modal.inputs.cap.value = room.total_room_residents;
            this.modal.inputs.hw.value = room.hw_meter_serial || '';
            this.modal.inputs.cw.value = room.cw_meter_serial || '';
            this.modal.inputs.el.value = room.el_meter_serial || '';
            this.fillTariffSelect(room.tariff_id);
        } else {
            this.modal.title.textContent = 'Добавить помещение';
            this.modal.inputs.id.value = '';
            if (this.dom.dormFilterSelect.value) {
                this.modal.inputs.dorm.value = this.dom.dormFilterSelect.value;
            }
            this.fillTariffSelect(null);
        }
        this.modal.window.classList.add('open');
    },

    async handleSave(e) {
        e.preventDefault();
        const btn = this.modal.form.querySelector('.confirm-btn');
        const id = this.modal.inputs.id.value;
        const tariffVal = this.modal.inputs.tariff?.value;
        const data = {
            dormitory_name: this.modal.inputs.dorm.value.trim(),
            room_number: this.modal.inputs.num.value.trim(),
            apartment_area: parseFloat(this.modal.inputs.area.value),
            total_room_residents: parseInt(this.modal.inputs.cap.value),
            tariff_id: tariffVal ? parseInt(tariffVal) : null,
            hw_meter_serial: this.modal.inputs.hw.value.trim(),
            cw_meter_serial: this.modal.inputs.cw.value.trim(),
            el_meter_serial: this.modal.inputs.el.value.trim(),
        };

        setLoading(btn, true, 'Сохранение...');
        try {
            if (id) {
                await api.put(`/rooms/${id}`, data);
                toast('Помещение обновлено', 'success');
            } else {
                await api.post('/rooms', data);
                toast('Помещение добавлено', 'success');
            }
            this.modal.window.classList.remove('open');
            this.table.refresh();
            this.loadDormitories();
            this.loadStats();
        } catch (err) {
            toast(err.message, 'error');
        } finally {
            setLoading(btn, false);
        }
    },

    async deleteRoom(id) {
        if (!confirm('ВНИМАНИЕ! Вы уверены, что хотите удалить помещение? Это возможно только если к нему не привязаны жильцы и нет истории показаний.')) return;
        try {
            await api.delete(`/rooms/${id}`);
            toast('Помещение удалено', 'success');
            this.table.refresh();
            this.loadStats();
        } catch (e) {
            toast(e.message, 'error');
        }
    },

    // ==========================================
    // ЗАМЕНА СЧЕТЧИКА
    // ==========================================
    openMeterModal(room) {
        if (!this.meter.modal) return;
        this.meter.form.reset();
        this.meter.roomId.value = room.id;
        this.meter.roomName.textContent = `${room.dormitory_name}, ком. ${room.room_number}`;
        this.meter.initialNew.value = "0";
        this.meter.modal.classList.add('open');
    },

    async handleMeterSubmit(e) {
        e.preventDefault();
        const payload = {
            meter_type: this.meter.type.value,
            new_serial: this.meter.newSerial.value.trim(),
            final_old_value: parseFloat(this.meter.finalOld.value.replace(',', '.')),
            initial_new_value: parseFloat(this.meter.initialNew.value.replace(',', '.'))
        };
        if (!payload.new_serial) return toast('Укажите новый номер счетчика', 'error');
        if (isNaN(payload.final_old_value)) return toast('Введите финальное показание', 'error');
        if (!confirm('Вы уверены? Система рассчитает потребление по старому счетчику и установит новые базовые значения. Это действие нельзя отменить.')) return;

        setLoading(this.meter.btnSubmit, true, 'Оформление...');
        try {
            await api.post(`/rooms/${this.meter.roomId.value}/replace-meter`, payload);
            toast('Счетчик успешно заменен!', 'success');
            this.meter.modal.classList.remove('open');
            this.table.refresh();
        } catch (err) {
            toast(err.message, 'error');
        } finally {
            setLoading(this.meter.btnSubmit, false, 'Оформить замену');
        }
    },

    // ==========================================
    // НОВОЕ: НАЧАЛЬНЫЕ ПОКАЗАНИЯ (ПОШТУЧНО)
    // ==========================================
    async openInitialModal(room) {
        if (!this.initial.modal) return;
        this.initial.form.reset();
        this.initial.roomId.value = room.id;
        this.initial.roomName.textContent = `${room.dormitory_name}, ком. ${room.room_number}`;

        // Загружаем текущие показания комнаты
        try {
            const data = await api.get(`/rooms/${room.id}/current-readings`);
            this.initial.hot.value = parseFloat(data.hot_water) || '';
            this.initial.cold.value = parseFloat(data.cold_water) || '';
            this.initial.elect.value = parseFloat(data.electricity) || '';
        } catch (e) {
            // Если ошибка — поля останутся пустыми
        }

        this.initial.modal.classList.add('open');
    },

    async handleInitialSubmit(e) {
        e.preventDefault();
        const roomId = this.initial.roomId.value;
        const hot = parseFloat(this.initial.hot.value);
        const cold = parseFloat(this.initial.cold.value);
        const elect = parseFloat(this.initial.elect.value);

        if (isNaN(hot) || isNaN(cold) || isNaN(elect)) {
            return toast('Заполните все три поля показаний', 'error');
        }

        setLoading(this.initial.btnSubmit, true, 'Сохранение...');
        try {
            await api.post(`/rooms/${roomId}/initial-readings?hot_water=${hot}&cold_water=${cold}&electricity=${elect}`);
            toast('Начальные показания сохранены!', 'success');
            this.initial.modal.classList.remove('open');
            this.table.refresh();
        } catch (err) {
            toast(err.message, 'error');
        } finally {
            setLoading(this.initial.btnSubmit, false, 'Сохранить показания');
        }
    },

    // ==========================================
    // НОВОЕ: НАЧАЛЬНЫЕ ПОКАЗАНИЯ (МАССОВЫЙ ИМПОРТ)
    // ==========================================
    async downloadReadingsTemplate() {
        setLoading(this.dom.btnDownloadReadingsTemplate, true, 'Генерация...');
        try {
            await api.download('/rooms/initial-readings/template', 'Initial_Readings_Template.xlsx');
            toast('Шаблон скачан. Заполните показания и загрузите обратно.', 'success');
        } catch (e) {
            toast('Ошибка скачивания шаблона: ' + e.message, 'error');
        } finally {
            setLoading(this.dom.btnDownloadReadingsTemplate, false, 'Скачать шаблон');
        }
    },

    async importInitialReadings() {
        const file = this.dom.importInitialReadingsFile?.files[0];
        if (!file) return toast('Выберите файл Excel', 'info');
        if (!file.name.match(/\.(xlsx|xls)$/)) return toast('Только файлы Excel (.xlsx)', 'error');

        const formData = new FormData();
        formData.append('file', file);

        setLoading(this.dom.btnImportInitialReadings, true, 'Загрузка...');
        try {
            const result = await api.post('/rooms/import-initial-readings', formData);

            let msg = `Обновлено комнат: ${result.updated}`;
            if (result.skipped > 0) msg += `, пропущено: ${result.skipped}`;

            if (result.errors && result.errors.length > 0) {
                toast(msg + '. Есть предупреждения — см. консоль.', 'warning');
                console.warn('Import errors:', result.errors);
            } else {
                toast(msg, 'success');
            }

            this.dom.importInitialReadingsFile.value = '';
            this.table.refresh();
        } catch (e) {
            toast('Ошибка импорта: ' + e.message, 'error');
        } finally {
            setLoading(this.dom.btnImportInitialReadings, false, 'Загрузить');
        }
    }
};