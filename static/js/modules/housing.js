// static/js/modules/housing.js
import { api } from '../core/api.js';
import { el, toast, setLoading, showConfirm } from '../core/dom.js';
import { TableController } from '../core/table-controller.js';
import { formatRoomAddress } from '../core/format-address.js';

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
            this.loadStreets(),
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
            btnNormalizeList: document.getElementById('btnNormalizeSerialsList'),
            btnDormSettings: document.getElementById('btnDormSettings'),
            dormSettingsModal: document.getElementById('dormSettingsModal'),
            dormSettingsTitle: document.getElementById('dormSettingsTitle'),
            dormSettingsBuilding: document.getElementById('dormSettingsBuilding'),
            dormSettingsStats: document.getElementById('dormSettingsStats'),
            dormSettingsTariff: document.getElementById('dormSettingsTariff'),
            dormSettingsMeters: document.getElementById('dormSettingsMeters'),

            // Новые фильтры
            filterOccupancy: document.getElementById('filterOccupancy'),
            filterMissingMeter: document.getElementById('filterMissingMeter'),
            filterNoMeters: document.getElementById('filterNoMeters'),

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
            // housing_001: секции для скрытия/показа в зависимости от типа.
            sections: {
                dorm: document.getElementById('dormFields'),
                house: document.getElementById('houseFields'),
                meters: document.getElementById('meterFields'),
                singles: document.getElementById('singlesFields'),
            },
            // housing_001: datalist для автокомплита улиц.
            streetList: document.getElementById('streetList'),
            inputs: {
                id: document.getElementById('roomEditId'),
                // housing_001: radio тип помещения.
                placeTypeDorm: document.getElementById('roomPlaceTypeDormitory'),
                placeTypeHouse: document.getElementById('roomPlaceTypeHouse'),
                dorm: document.getElementById('roomDormitory'),
                num: document.getElementById('roomNumber'),
                // housing_001: новые «домовые» адресные поля.
                street: document.getElementById('roomStreet'),
                houseNumber: document.getElementById('roomHouseNumber'),
                apartmentNumber: document.getElementById('roomApartmentNumber'),
                area: document.getElementById('roomArea'),
                cap: document.getElementById('roomCapacity'),
                tariff: document.getElementById('roomTariffId'),
                hw: document.getElementById('roomHwSerial'),
                cw: document.getElementById('roomCwSerial'),
                el: document.getElementById('roomElSerial'),
                hasHw: document.getElementById('roomHasHw'),
                hasCw: document.getElementById('roomHasCw'),
                hasEl: document.getElementById('roomHasEl'),
                bulkMeterBtn: document.getElementById('roomBulkMeterBtn'),
                // Нормализация серийников счётчиков к шаблону.
                normalizeBtn: document.getElementById('roomNormalizeSerialsBtn'),
                normalizeHouseBtn: document.getElementById('roomNormalizeHouseBtn'),
                // Bug AS: новые поля «холостяцкая квартира» + макс. вместимость.
                isSingles: document.getElementById('roomIsSingles'),
                maxCapacity: document.getElementById('roomMaxCapacity'),
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
        if (this.dom.filterNoMeters) this.dom.filterNoMeters.addEventListener('change', refilter);

        if (this.dom.btnExport) {
            this.dom.btnExport.addEventListener('click', () => this.exportExcel());
        }
        if (this.dom.btnNormalizeList) {
            this.dom.btnNormalizeList.addEventListener('click', () => this.normalizeSerialsByFilter());
        }
        if (this.dom.btnDormSettings) {
            this.dom.btnDormSettings.addEventListener('click', () => this.openDormSettings());
        }
        if (this.dom.dormSettingsModal) {
            this.dom.dormSettingsModal.addEventListener('click', (e) => {
                if (e.target === this.dom.dormSettingsModal || e.target.closest('[data-dorm-close]')) {
                    this.dom.dormSettingsModal.classList.remove('open');
                }
            });
        }

        if (this.dom.btnOpenAdd) {
            this.dom.btnOpenAdd.addEventListener('click', () => this.openModal());
        }

        if (this.modal.form) {
            this.modal.form.addEventListener('submit', (e) => this.handleSave(e));
        }

        // housing_001: переключение типа помещения в форме.
        // При смене типа — перестраиваем секции и перезагружаем тарифы
        // отфильтрованные по applicable_to.
        [this.modal.inputs.placeTypeDorm, this.modal.inputs.placeTypeHouse].forEach(rad => {
            if (rad) rad.addEventListener('change', () => this._applyPlaceTypeUI());
        });

        // Холостяцкая квартира: «Кол-во проживающих» считается автоматически
        // (= число прописанных жильцов), поэтому поле блокируем на ввод.
        if (this.modal.inputs.isSingles) {
            this.modal.inputs.isSingles.addEventListener('change', () => this._applySinglesUI());
        }
        if (this.modal.inputs.bulkMeterBtn) {
            this.modal.inputs.bulkMeterBtn.addEventListener('click', () => this.bulkMeterToHouse());
        }
        if (this.modal.inputs.normalizeBtn) {
            this.modal.inputs.normalizeBtn.addEventListener('click', () => this.normalizeSerialsInForm());
        }
        if (this.modal.inputs.normalizeHouseBtn) {
            this.modal.inputs.normalizeHouseBtn.addEventListener('click', () => this.normalizeSerialsHouse());
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
            // Левый «Фильтр по зданию»: и общаги, И дома. Раньше брали
            // /rooms/dormitories (только общаги) — дома в фильтр не попадали.
            // Значение опции: общага → dormitory_name (как раньше, чтобы не
            // ломать существующие фильтры/префиллы); дом → «house:::street:::house».
            // new Option(text, value) — браузер экранирует text через textContent.
            const buildings = await api.get('/rooms/buildings');
            const sel = this.dom.dormFilterSelect;
            const prevValue = sel.value;
            sel.innerHTML = '';
            sel.appendChild(new Option('Все объекты', ''));
            const values = [''];
            (buildings || []).forEach(b => {
                if (b.type === 'house' && b.street && b.house_number) {
                    const val = `house:::${b.street}:::${b.house_number}`;
                    sel.appendChild(new Option(b.label || `🏠 ${b.street}, ${b.house_number}`, val));
                    values.push(val);
                } else if (b.dormitory_name) {
                    sel.appendChild(new Option(b.label || b.dormitory_name, b.dormitory_name));
                    values.push(b.dormitory_name);
                }
            });
            if (prevValue && values.includes(prevValue)) sel.value = prevValue;

            // datalist автокомплита поля «Общежитие» в форме — только общаги.
            const list = this.dom.dormList;
            if (list) {
                list.innerHTML = '';
                (buildings || [])
                    .filter(b => b.type !== 'house' && b.dormitory_name)
                    .forEach(b => {
                        const opt = document.createElement('option');
                        opt.value = String(b.dormitory_name);
                        list.appendChild(opt);
                    });
            }
        } catch (e) {
            toast('Ошибка загрузки списка зданий', 'error');
        }
    },

    /** Параметры фильтра по выбранному зданию (общага или дом) для /rooms*.
     *  Общага → {dormitory}; дом → {place_type, street, house_number}. */
    _buildingFilterParams() {
        const v = (this.dom.dormFilterSelect?.value || '').trim();
        if (!v) return {};
        if (v.startsWith('house:::')) {
            const p = v.split(':::');
            return { place_type: 'house', street: p[1] || '', house_number: p[2] || '' };
        }
        return { dormitory: v };
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
                const params = { ...this._buildingFilterParams() };
                if (this.dom.filterOccupancy?.value) params.occupancy = this.dom.filterOccupancy.value;
                if (this.dom.filterMissingMeter?.checked) params.missing_meter = 'true';
                if (this.dom.filterNoMeters?.checked) params.no_meters = 'true';
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
        const isHouse = room.place_type === 'house';
        // У домов счётчиков нет — отсутствие серийников НЕ повод подсвечивать.
        const missing = !isHouse
            && (!room.hw_meter_serial || !room.cw_meter_serial || !room.el_meter_serial);

        const bgTint = missing ? 'background: #fffbeb;' : '';

        const isOpen = this.expanded.has(room.id);
        const chevron = el('button', {
            class: 'btn-icon',
            style: { background: 'transparent', border: 'none', cursor: 'pointer', fontSize: '13px', color: 'var(--text-secondary)' },
            title: isOpen ? 'Свернуть' : 'Показать жильцов',
            onclick: (e) => { e.stopPropagation(); this.toggleExpand(room); },
        }, isOpen ? '▼' : '▶');

        // housing_001: адрес отображается по-разному в зависимости от типа.
        //   dormitory:  «4дв.стр.9» / «203»
        //   house:      «ул. Ленина, 5» / «кв. 12»
        const placeLabel = isHouse
            ? `ул. ${room.street || '—'}, д. ${room.house_number || '—'}`
            : (room.dormitory_name || '—');
        const numberLabel = isHouse
            ? `кв. ${room.apartment_number || '—'}`
            : (room.room_number || '—');

        // Кликабельный номер — открывает Hub-модалку с действиями.
        const numberCell = el('td', {},
            el('button', {
                class: 'link-btn',
                title: 'Действия по помещению',
                onclick: () => this.openActionsHub(room),
                style: {
                    background: 'transparent',
                    border: 'none',
                    padding: '0',
                    fontWeight: 'bold',
                    color: 'var(--primary-color)',
                    cursor: 'pointer',
                    fontSize: '14px',
                },
            }, numberLabel)
        );

        // Для дома вместо серийников показываем компактный плейсхолдер
        // (счётчики не нужны), плюс рядом с адресом — иконка типа.
        const typeIcon = isHouse
            ? el('span', {
                title: 'Дом / квартира',
                style: { color: '#9333ea', marginRight: '6px', fontSize: '11px' },
              }, '🏠')
            : el('span', {
                title: 'Общежитие',
                style: { color: '#2563eb', marginRight: '6px', fontSize: '11px' },
              }, '🏢');

        // Компактный текстовый бейдж типа помещения рядом с адресом.
        const typeBadge = isHouse
            ? el('span', {
                style: {
                    background: '#f3e8ff', color: '#6b21a8',
                    fontSize: '11px', padding: '2px 6px',
                    borderRadius: '4px', marginLeft: '6px',
                },
              }, 'Дом')
            : el('span', {
                style: {
                    background: '#dbeafe', color: '#1e40af',
                    fontSize: '11px', padding: '2px 6px',
                    borderRadius: '4px', marginLeft: '6px',
                },
              }, 'Общага');

        const dashCell = (color) => el('td', {
            class: 'text-sm font-mono',
            style: { color: color, textAlign: 'center' },
            title: 'Не применимо для домов/квартир',
        }, '—');

        const row = el('tr', {
            class: 'hover:bg-gray-50 transition-colors',
            'data-room-id': String(room.id),
            style: bgTint ? { background: '#fffbeb' } : {},
        },
            el('td', { class: 'text-center' }, chevron),
            el('td', { class: 'text-gray-500 text-sm' }, `#${room.id}`),
            el('td', { style: { fontWeight: 'bold', color: '#1f2937' } }, typeIcon, placeLabel, typeBadge),
            numberCell,
            el('td', {}, `${Number(room.apartment_area).toFixed(1)} м²`),
            el('td', { class: 'text-center' }, String(cap)),
            isHouse
                ? dashCell('#d1d5db')
                : el('td', { class: 'text-sm font-mono', style: { color: room.hw_meter_serial ? '#dc2626' : '#9ca3af' } }, room.hw_meter_serial || '—'),
            isHouse
                ? dashCell('#d1d5db')
                : el('td', { class: 'text-sm font-mono', style: { color: room.cw_meter_serial ? '#2563eb' : '#9ca3af' } }, room.cw_meter_serial || '—'),
            isHouse
                ? dashCell('#d1d5db')
                : el('td', { class: 'text-sm font-mono', style: { color: room.el_meter_serial ? '#d97706' : '#9ca3af' } }, room.el_meter_serial || '—'),
            el('td', { class: 'text-center' },
                el('button', {
                    class: 'btn-icon btn-delete', title: 'Удалить',
                    onclick: () => this.deleteRoom(room.id)
                }, '🗑')
            )
        );

        return row;
    },

    // =====================================================================
    // HUB ДЕЙСТВИЙ ПО КОМНАТЕ — открывается кликом по номеру комнаты.
    // Внутри — сводка + 3 крупные кнопки (Редактировать / Начальные показания /
    // Замена счётчика). Удаление сюда не выносим намеренно.
    // =====================================================================
    _roomHubBound: false,
    _roomHubCurrent: null,

    openActionsHub(room) {
        const modal = document.getElementById('roomActionsModal');
        if (!modal) return;
        this._roomHubCurrent = room;

        // Шапка — formatRoomAddress справится с обоими типами (общага/дом).
        document.getElementById('roomHubTitle').textContent = formatRoomAddress(room);

        // Сводка по комнате
        const area = Number(room.apartment_area || 0).toFixed(1);
        const cap = Number(room.total_room_residents || 0);
        const meterBadge = (label, val, color) => {
            const filled = !!val;
            return `<span style="display:inline-block; padding:2px 8px; border-radius:10px; font-size:11px; font-weight:600; margin-right:4px; background:${filled ? color + '22' : '#f3f4f6'}; color:${filled ? color : '#9ca3af'};">
                ${label}: ${filled ? `<span style="font-family:monospace;">${room.hw_meter_serial && label === 'ГВС' ? room.hw_meter_serial : (room.cw_meter_serial && label === 'ХВС' ? room.cw_meter_serial : (room.el_meter_serial && label === 'Свет' ? room.el_meter_serial : val))}</span>` : '—'}
            </span>`;
        };
        const esc = s => { if (s == null) return ''; const d = document.createElement('div'); d.textContent = String(s); return d.innerHTML; };

        document.getElementById('roomHubSummary').innerHTML = `
            <div style="display:flex; gap:14px; flex-wrap:wrap; justify-content:space-between; margin-bottom:10px;">
                <div>
                    <div style="font-size:11px; color:var(--text-secondary);">Площадь</div>
                    <div style="font-weight:600;">${area} м²</div>
                </div>
                <div>
                    <div style="font-size:11px; color:var(--text-secondary);">Вместимость</div>
                    <div style="font-weight:600;">${cap} мест</div>
                </div>
                <div>
                    <div style="font-size:11px; color:var(--text-secondary);">ID</div>
                    <div style="font-weight:600; font-family:monospace;">#${room.id}</div>
                </div>
            </div>
            <div style="font-size:11px; color:var(--text-secondary); margin-bottom:4px;">Серийные номера счётчиков:</div>
            <div style="display:flex; flex-wrap:wrap; gap:4px;">
                <span style="display:inline-block; padding:3px 10px; border-radius:10px; font-size:11px; font-weight:600; background:${room.hw_meter_serial ? '#fee2e2' : '#f3f4f6'}; color:${room.hw_meter_serial ? '#991b1b' : '#9ca3af'};">
                    ГВС: <span style="font-family:monospace;">${esc(room.hw_meter_serial || '—')}</span>
                </span>
                <span style="display:inline-block; padding:3px 10px; border-radius:10px; font-size:11px; font-weight:600; background:${room.cw_meter_serial ? '#dbeafe' : '#f3f4f6'}; color:${room.cw_meter_serial ? '#1e40af' : '#9ca3af'};">
                    ХВС: <span style="font-family:monospace;">${esc(room.cw_meter_serial || '—')}</span>
                </span>
                <span style="display:inline-block; padding:3px 10px; border-radius:10px; font-size:11px; font-weight:600; background:${room.el_meter_serial ? '#fef3c7' : '#f3f4f6'}; color:${room.el_meter_serial ? '#92400e' : '#9ca3af'};">
                    Свет: <span style="font-family:monospace;">${esc(room.el_meter_serial || '—')}</span>
                </span>
            </div>
        `;

        // Биндим обработчики один раз
        if (!this._roomHubBound) {
            modal.addEventListener('click', (e) => {
                if (e.target.closest('[data-room-hub-close]') || e.target === modal) {
                    modal.classList.remove('open');
                    return;
                }
                const btn = e.target.closest('[data-room-action]');
                if (!btn) return;
                const action = btn.dataset.roomAction;
                const r = this._roomHubCurrent;
                if (!r) return;
                // Закрываем хаб и сразу открываем нужную модалку
                modal.classList.remove('open');
                if (action === 'edit')    this.openModal(r);
                if (action === 'initial') this.openInitialModal(r);
                if (action === 'meter')   this.openMeterModal(r);
                if (action === 'qr')      this.showRoomQr(r);
            });
            this._roomHubBound = true;
        }

        modal.classList.add('open');
    },

    // QR-код квартиры: get-or-create токен → показать QR (через /api/qr) +
    // ссылку + печать + перевыпуск. Токен во фрагменте URL (#...) — на сервер
    // не уходит. Жилец сканирует и подаёт показания без логина.
    async showRoomQr(room) {
        let data;
        try {
            data = await api.post(`/rooms/${room.id}/qr`);
        } catch (e) {
            toast('Не удалось получить QR: ' + (e.message || e), 'error');
            return;
        }
        const url = location.origin + data.portal_path;
        const addr = formatRoomAddress(room);
        const qrSrc = '/api/qr?text=' + encodeURIComponent(url) + '&box_size=8&border=2';

        const esc = s => { const d = document.createElement('div'); d.textContent = String(s == null ? '' : s); return d.innerHTML; };
        const ov = document.createElement('div');
        ov.className = 'modal-overlay open';
        ov.style.zIndex = '4000';
        ov.innerHTML = `
            <div class="modal-window" style="max-width:380px; text-align:center;">
                <div class="modal-header"><h3><i class="fa-solid fa-qrcode"></i> QR-код квартиры</h3>
                    <button class="close-btn close-icon" data-qr-close>&times;</button></div>
                <div class="modal-body">
                    <div style="font-weight:600; margin-bottom:10px;">${esc(addr)}</div>
                    <img src="${qrSrc}" alt="QR" style="width:240px; height:240px; border:1px solid #e2e8f0; border-radius:8px;">
                    <div style="font-size:11px; color:var(--text-secondary); word-break:break-all; margin:10px 0;">${esc(url)}</div>
                    <div style="font-size:12px; color:var(--text-secondary);">Наклеить внутри квартиры у счётчика. Жилец сканирует → подаёт показания без логина.</div>
                </div>
                <div class="modal-footer" style="display:flex; gap:8px; justify-content:center;">
                    <button class="action-btn primary-btn" data-qr-print><i class="fa-solid fa-print"></i> Печать</button>
                    <button class="action-btn secondary-btn" data-qr-regen><i class="fa-solid fa-rotate"></i> Перевыпустить</button>
                    <button class="action-btn secondary-btn" data-qr-close>Закрыть</button>
                </div>
            </div>`;
        document.body.appendChild(ov);

        const close = () => ov.remove();
        ov.addEventListener('click', (e) => { if (e.target === ov || e.target.closest('[data-qr-close]')) close(); });

        ov.querySelector('[data-qr-print]').addEventListener('click', () => {
            const w = window.open('', '_blank', 'width=420,height=560');
            if (!w) { toast('Разрешите всплывающие окна для печати', 'warning'); return; }
            w.document.write(`<html><head><title>QR — ${esc(addr)}</title></head>
                <body style="font-family:sans-serif; text-align:center; padding:24px;">
                <h2 style="font-size:18px;">${esc(addr)}</h2>
                <p style="color:#555;">Подача показаний счётчиков</p>
                <img src="${qrSrc}" style="width:300px;height:300px;" onload="window.focus();window.print();">
                <p style="font-size:12px;color:#777;margin-top:14px;">Отсканируйте камерой телефона</p>
                </body></html>`);
            w.document.close();
        });

        ov.querySelector('[data-qr-regen]').addEventListener('click', async () => {
            if (!await showConfirm('Перевыпустить QR? Старый код перестанет работать — нужно будет распечатать и наклеить новый.', { title: 'Перевыпуск QR', confirmText: 'Перевыпустить' })) return;
            try {
                const nd = await api.post(`/rooms/${room.id}/qr/regenerate`);
                close();
                toast('QR перевыпущен', 'success');
                this.showRoomQr(room);   // покажем новый
                void nd;
            } catch (e) { toast('Ошибка перевыпуска: ' + (e.message || e), 'error'); }
        });
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
            const tariff = r.tariff_name ? `<span style="color:#6366f1;">(${escapeHtml(r.tariff_name)})</span>` : '<span style="color:#9ca3af;">(дефолт)</span>';
            return `
                <li style="padding:6px 0; border-bottom:1px solid #e5e7eb; display:flex; gap:10px; flex-wrap:wrap; align-items:center;">
                    <b style="color:#1f2937;">${escapeHtml(r.username)}</b>
                    <span style="font-size:11px; background:#e0e7ff; color:#3730a3; padding:2px 7px; border-radius:10px;">${type}</span>
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
        const bp = this._buildingFilterParams();
        const qs = Object.keys(bp).length ? `?${new URLSearchParams(bp).toString()}` : '';
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

    /**
     * housing_001: загрузка тарифов с фильтром по applicable_to.
     *   applicable_to='dormitory' → тарифы для общаг + универсальные ('both').
     *   applicable_to='house'     → тарифы для домов + универсальные.
     *   без параметра             → все активные (для первичной загрузки).
     */
    async loadTariffs(applicableTo = null) {
        try {
            const q = applicableTo ? `?applicable_to=${encodeURIComponent(applicableTo)}` : '';
            // Без getCached: ключ зависит от applicable_to, кеширование
            // на 5 минут даёт промахи при каждом переключении радио.
            this.tariffs = await api.get(`/tariffs${q}`);
        } catch {
            this.tariffs = [];
        }
    },

    /**
     * housing_001: список улиц для автокомплита формы «Дом / квартира».
     * Аналог loadDormitories. Заполняет datalist#streetList.
     */
    async loadStreets() {
        try {
            const streets = await api.get('/rooms/streets');
            const list = this.modal.streetList;
            if (!list) return;
            list.innerHTML = '';
            streets.forEach(s => {
                const opt = document.createElement('option');
                opt.value = String(s);
                list.appendChild(opt);
            });
        } catch {
            // Тихо — улицы не критичны для работы формы.
        }
    },

    fillTariffSelect(selectedId) {
        const sel = this.modal.inputs.tariff;
        if (!sel) return;
        sel.innerHTML = '<option value="">— от дома (по умолчанию) —</option>';
        // Сервер (/api/tariffs) уже возвращает ТОЛЬКО активные (WHERE is_active),
        // а response_model=TariffSchema не включает поле is_active в ответ —
        // поэтому фильтр t.is_active отбрасывал ВСЕ тарифы (undefined → falsy)
        // и селектор был пуст. Не фильтруем повторно.
        (this.tariffs || []).forEach(t => {
            const opt = document.createElement('option');
            opt.value = String(t.id);
            opt.textContent = t.name;
            if (selectedId != null && Number(selectedId) === t.id) opt.selected = true;
            sel.appendChild(opt);
        });
    },

    async exportExcel() {
        const params = new URLSearchParams(this._buildingFilterParams());
        if (this.dom.filterOccupancy?.value) params.append('occupancy', this.dom.filterOccupancy.value);
        if (this.dom.filterMissingMeter?.checked) params.append('missing_meter', 'true');
        if (this.dom.filterNoMeters?.checked) params.append('no_meters', 'true');
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

    /**
     * housing_001: Перестраивает форму под выбранный тип помещения.
     *   — dormitory: показывает dormFields, meterFields, singlesFields.
     *   — house: скрывает счётчики и singles, показывает houseFields.
     *
     * Также управляет required-атрибутами полей (HTML5-валидация формы),
     * чтобы pristine-форма не валилась на required-полях другого типа,
     * и перезагружает список тарифов с фильтром applicable_to.
     */
    _applyPlaceTypeUI() {
        const isHouse = !!this.modal.inputs.placeTypeHouse?.checked;
        const sec = this.modal.sections;
        if (sec.dorm)    sec.dorm.style.display    = isHouse ? 'none' : '';
        if (sec.house)   sec.house.style.display   = isHouse ? '' : 'none';
        if (sec.meters)  sec.meters.style.display  = isHouse ? 'none' : '';
        if (sec.singles) sec.singles.style.display = isHouse ? 'none' : '';
        // Дом снимает галочку холостяка — обновляем состояние поля «кол-во».
        if (isHouse && this.modal.inputs.isSingles) this.modal.inputs.isSingles.checked = false;
        this._applySinglesUI();

        // Required-атрибуты — браузер сам не пускает submit пустых полей.
        const setReq = (el, req) => {
            if (!el) return;
            if (req) el.setAttribute('required', '');
            else el.removeAttribute('required');
        };
        setReq(this.modal.inputs.dorm, !isHouse);
        setReq(this.modal.inputs.num, !isHouse);
        setReq(this.modal.inputs.street, isHouse);
        setReq(this.modal.inputs.houseNumber, isHouse);
        setReq(this.modal.inputs.apartmentNumber, isHouse);

        // Когда переключаемся на дом — обнуляем «общажные» значения
        // (если до этого что-то ввели). Аналогично в обратную сторону.
        if (isHouse) {
            if (this.modal.inputs.dorm) this.modal.inputs.dorm.value = '';
            if (this.modal.inputs.num) this.modal.inputs.num.value = '';
            if (this.modal.inputs.hw) this.modal.inputs.hw.value = '';
            if (this.modal.inputs.cw) this.modal.inputs.cw.value = '';
            if (this.modal.inputs.el) this.modal.inputs.el.value = '';
            if (this.modal.inputs.isSingles) this.modal.inputs.isSingles.checked = false;
        } else {
            if (this.modal.inputs.street) this.modal.inputs.street.value = '';
            if (this.modal.inputs.houseNumber) this.modal.inputs.houseNumber.value = '';
            if (this.modal.inputs.apartmentNumber) this.modal.inputs.apartmentNumber.value = '';
        }

        // Перезагружаем тарифы с фильтром applicable_to. Сохраняем
        // currently-selected tariff_id если он подходит обоим типам.
        const currentTariff = this.modal.inputs.tariff?.value || null;
        this.loadTariffs(isHouse ? 'house' : 'dormitory')
            .then(() => this.fillTariffSelect(currentTariff));
    },

    /**
     * Холостяцкая квартира: «Кол-во проживающих» = число прописанных жильцов,
     * считается на бэке автоматически (recount_singles_residents). Поэтому
     * при включённой галочке блокируем поле на ввод и снимаем required
     * (иначе пустое readonly-поле заблокирует submit).
     */
    _applySinglesUI() {
        const cap = this.modal.inputs.cap;
        if (!cap) return;
        const isSingles = !!this.modal.inputs.isSingles?.checked;
        if (isSingles) {
            cap.setAttribute('readonly', '');
            cap.removeAttribute('required');
            cap.title = 'Считается автоматически = число прописанных жильцов квартиры';
            cap.style.background = '#f1f5f9';
            cap.style.cursor = 'not-allowed';
        } else {
            cap.removeAttribute('readonly');
            cap.setAttribute('required', '');
            cap.title = '';
            cap.style.background = '';
            cap.style.cursor = '';
        }
    },

    openModal(room = null) {
        this.modal.form.reset();
        const isHouse = !!(room && room.place_type === 'house');

        // Выставляем radio до показа модалки, чтобы _applyPlaceTypeUI
        // увидел правильный тип.
        if (this.modal.inputs.placeTypeHouse) this.modal.inputs.placeTypeHouse.checked = isHouse;
        if (this.modal.inputs.placeTypeDorm)  this.modal.inputs.placeTypeDorm.checked = !isHouse;
        this._applyPlaceTypeUI();

        if (room) {
            this.modal.title.textContent = 'Редактировать помещение';
            this.modal.inputs.id.value = room.id;
            // Общажные поля (заполнятся только если place_type='dormitory').
            this.modal.inputs.dorm.value = room.dormitory_name || '';
            this.modal.inputs.num.value = room.room_number || '';
            // Домовые поля (заполнятся только если place_type='house').
            if (this.modal.inputs.street) this.modal.inputs.street.value = room.street || '';
            if (this.modal.inputs.houseNumber) this.modal.inputs.houseNumber.value = room.house_number || '';
            if (this.modal.inputs.apartmentNumber) this.modal.inputs.apartmentNumber.value = room.apartment_number || '';

            this.modal.inputs.area.value = room.apartment_area;
            this.modal.inputs.cap.value = room.total_room_residents;
            this.modal.inputs.hw.value = room.hw_meter_serial || '';
            this.modal.inputs.cw.value = room.cw_meter_serial || '';
            this.modal.inputs.el.value = room.el_meter_serial || '';
            // Наличие счётчиков комнаты (meters_002). undefined → считаем «есть».
            if (this.modal.inputs.hasHw) this.modal.inputs.hasHw.checked = room.has_hw_meter !== false;
            if (this.modal.inputs.hasCw) this.modal.inputs.hasCw.checked = room.has_cw_meter !== false;
            if (this.modal.inputs.hasEl) this.modal.inputs.hasEl.checked = room.has_el_meter !== false;
            // Bug AS
            if (this.modal.inputs.isSingles) {
                this.modal.inputs.isSingles.checked = !!room.is_singles_apartment;
            }
            if (this.modal.inputs.maxCapacity) {
                this.modal.inputs.maxCapacity.value = room.max_capacity ?? '';
            }
            this.fillTariffSelect(room.tariff_id);
        } else {
            this.modal.title.textContent = 'Добавить помещение';
            this.modal.inputs.id.value = '';
            // Дефолты счётчиков нового помещения — все есть.
            if (this.modal.inputs.hasHw) this.modal.inputs.hasHw.checked = true;
            if (this.modal.inputs.hasCw) this.modal.inputs.hasCw.checked = true;
            if (this.modal.inputs.hasEl) this.modal.inputs.hasEl.checked = true;
            // Bug AS: дефолты для нового помещения.
            if (this.modal.inputs.isSingles) this.modal.inputs.isSingles.checked = false;
            if (this.modal.inputs.maxCapacity) this.modal.inputs.maxCapacity.value = '';
            // Префилл названия общаги из активного фильтра (для удобства массового
            // создания комнат в одном общежитии). Применимо только если тип = dormitory.
            if (!isHouse && this.dom.dormFilterSelect && this.dom.dormFilterSelect.value
                && !this.dom.dormFilterSelect.value.startsWith('house:::')) {
                this.modal.inputs.dorm.value = this.dom.dormFilterSelect.value;
            }
            this.fillTariffSelect(null);
        }
        // Состояние поля «кол-во проживающих» зависит от галочки холостяка
        // (уже выставлена выше для room / new).
        this._applySinglesUI();
        this.modal.window.classList.add('open');
    },

    /** Применить галочки счётчиков ко ВСЕМ комнатам текущего дома/общежития. */
    async bulkMeterToHouse() {
        const isHouse = !!this.modal.inputs.placeTypeHouse?.checked;
        const body = {
            has_hw_meter: !!this.modal.inputs.hasHw?.checked,
            has_cw_meter: !!this.modal.inputs.hasCw?.checked,
            has_el_meter: !!this.modal.inputs.hasEl?.checked,
        };
        let target;
        if (isHouse) {
            body.street = (this.modal.inputs.street?.value || '').trim();
            body.house_number = (this.modal.inputs.houseNumber?.value || '').trim();
            if (!body.street || !body.house_number) {
                return toast('Укажите улицу и номер дома', 'error');
            }
            target = `дому ${body.street}, ${body.house_number}`;
        } else {
            body.dormitory_name = (this.modal.inputs.dorm?.value || '').trim();
            if (!body.dormitory_name) {
                return toast('Укажите общежитие', 'error');
            }
            target = `общежитию «${body.dormitory_name}»`;
        }
        const on = [
            body.has_hw_meter && 'ГВС', body.has_cw_meter && 'ХВС',
            body.has_el_meter && 'электр',
        ].filter(Boolean).join(', ') || 'нет счётчиков';
        if (!await showConfirm(`Применить счётчики (${on}) ко ВСЕМ комнатам ${target}?\n\nЖильцы наследуют автоматически.`, { confirmText: 'Применить' })) return;
        try {
            const res = await api.post('/rooms/bulk-meter-config', body);
            toast(`Обновлено комнат: ${res.updated_rooms}`, 'success');
        } catch (e) {
            toast('Ошибка: ' + (e.message || ''), 'error');
        }
    },

    // ── Нормализация серийников к шаблону «<тип>-<дом>-<комната>» ──────────
    // Тип берётся по полю (hw=гвс / cw=хвс / el=эл), ядро «дом-комната» =
    // числовые сегменты текущего серийника; мусорный буквенный префикс
    // (КВС/РРР/…) и тип-суффикс отбрасываются. Напр. КВС-4.8-101-ХВС → хвс-4.8-101.
    _normalizeSerial(old, kind) {
        const PREFIX = { hw: 'гвс', cw: 'хвс', el: 'эл' };
        if (!old) return old;
        const s = String(old).trim();
        if (!s) return s;
        const segs = s.replace(/\s+/g, '-').split('-').filter(Boolean);
        const core = segs.filter(x => /\d/.test(x)).join('-');
        if (!core) return s;  // ядро не определили — не трогаем
        return `${PREFIX[kind]}-${core}`;
    },

    // Кнопка в форме: приводим 3 поля серийников этой квартиры к шаблону.
    // Сохранение — обычной кнопкой формы после проверки.
    normalizeSerialsInForm() {
        const map = [
            [this.modal.inputs.hw, 'hw'],
            [this.modal.inputs.cw, 'cw'],
            [this.modal.inputs.el, 'el'],
        ];
        let changed = 0;
        map.forEach(([input, kind]) => {
            if (!input) return;
            const next = this._normalizeSerial(input.value, kind);
            if (next && next !== input.value) { input.value = next; changed++; }
        });
        if (changed) {
            toast(`Приведено к шаблону полей: ${changed}. Проверьте и нажмите «Сохранить».`, 'success');
        } else {
            toast('Нечего приводить (поля пусты или уже в формате).', 'info');
        }
    },

    // Bulk по дому/общежитию: предпросмотр (dry_run) → подтверждение → применение.
    async normalizeSerialsHouse() {
        const isHouse = !!this.modal.inputs.placeTypeHouse?.checked;
        const params = new URLSearchParams();
        let target;
        if (isHouse) {
            const street = (this.modal.inputs.street?.value || '').trim();
            const house = (this.modal.inputs.houseNumber?.value || '').trim();
            if (!street || !house) return toast('Укажите улицу и номер дома', 'error');
            params.set('street', street);
            params.set('house_number', house);
            target = `дому ${street}, ${house}`;
        } else {
            const dorm = (this.modal.inputs.dorm?.value || '').trim();
            if (!dorm) return toast('Укажите общежитие', 'error');
            params.set('dormitory_name', dorm);
            target = `общежитию «${dorm}»`;
        }
        let preview;
        try {
            preview = await api.post(`/rooms/normalize-serials?${params}&dry_run=true`);
        } catch (e) {
            return toast('Ошибка предпросмотра: ' + (e.message || ''), 'error');
        }
        if (!preview.changed_rooms) {
            return toast(`По ${target} нечего нормализовать (всё уже в формате).`, 'info');
        }
        const sample = (preview.changes || []).slice(0, 5).map(c => {
            const parts = Object.values(c.fields).map(v => `${v.old} → ${v.new}`);
            return `  комн. ${c.room_number}: ${parts.join('; ')}`;
        }).join('\n');
        const more = preview.changed_rooms > 5 ? `\n  …и ещё ${preview.changed_rooms - 5} комнат` : '';
        if (!await showConfirm(
            `Нормализовать серийники по ${target}?\n\n` +
            `Изменится комнат: ${preview.changed_rooms} из ${preview.total_rooms}.\n\n` +
            `Примеры:\n${sample}${more}`,
            { confirmText: 'Нормализовать' }
        )) return;
        try {
            const res = await api.post(`/rooms/normalize-serials?${params}&dry_run=false`);
            toast(`Нормализовано комнат: ${res.changed_rooms}`, 'success');
            this.table.load();
        } catch (e) {
            toast('Ошибка применения: ' + (e.message || ''), 'error');
        }
    },

    // Bulk-нормализация серийников ИЗ СПИСКА (кнопка в тулбаре): берёт дом из
    // фильтра слева — не нужно открывать комнату. Предпросмотр → подтверждение.
    async normalizeSerialsByFilter() {
        const bp = this._buildingFilterParams();
        if (!bp.dormitory && !bp.street) {
            return toast('Выберите дом/общежитие в фильтре слева, затем нажмите «Норм. серийники».', 'warning');
        }
        const label = bp.dormitory || `${bp.street}, д. ${bp.house_number}`;
        const params = new URLSearchParams(
            bp.dormitory
                ? { dormitory_name: bp.dormitory }
                : { street: bp.street, house_number: bp.house_number });
        let preview;
        try {
            preview = await api.post(`/rooms/normalize-serials?${params}&dry_run=true`);
        } catch (e) {
            return toast('Ошибка предпросмотра: ' + (e.message || ''), 'error');
        }
        if (!preview.changed_rooms) {
            return toast(`По «${label}» нечего нормализовать (всё уже в формате).`, 'info');
        }
        const sample = (preview.changes || []).slice(0, 5).map(c => {
            const parts = Object.values(c.fields).map(v => `${v.old} → ${v.new}`);
            return `  комн. ${c.room_number}: ${parts.join('; ')}`;
        }).join('\n');
        const more = preview.changed_rooms > 5 ? `\n  …и ещё ${preview.changed_rooms - 5} комнат` : '';
        if (!await showConfirm(
            `Нормализовать серийники по «${label}»?\n\n` +
            `Изменится комнат: ${preview.changed_rooms} из ${preview.total_rooms}.\n\n` +
            `Примеры:\n${sample}${more}`,
            { title: 'Нормализация серийников', confirmText: 'Нормализовать' }
        )) return;
        try {
            const res = await api.post(`/rooms/normalize-serials?${params}&dry_run=false`);
            toast(`Нормализовано комнат: ${res.changed_rooms}`, 'success');
            this.table.load();
        } catch (e) {
            toast('Ошибка применения: ' + (e.message || ''), 'error');
        }
    },

    // ── НАСТРОЙКИ ДОМА: тариф + счётчики + статистика, на весь дом ──────────
    // Открыть «Настройки дома»: грузим список ВСЕХ зданий (общаги + дома),
    // даём выбрать любое и применить тариф/счётчики на всё здание сразу.
    async openDormSettings() {
        this.dom.dormSettingsModal?.classList.add('open');
        const sel = this.dom.dormSettingsBuilding;
        const spin = '<div style="padding:14px; text-align:center; color:var(--text-secondary);"><i class="fa-solid fa-spinner fa-spin"></i> Загрузка…</div>';
        [this.dom.dormSettingsStats, this.dom.dormSettingsTariff, this.dom.dormSettingsMeters].forEach(b => { if (b) b.innerHTML = spin; });
        if (sel) sel.innerHTML = '<option>Загрузка…</option>';
        try {
            const buildings = await api.get('/rooms/buildings');
            this._dormBuildings = buildings || [];
            if (!this._dormBuildings.length) {
                if (sel) sel.innerHTML = '<option value="">Нет зданий</option>';
                if (this.dom.dormSettingsStats) this.dom.dormSettingsStats.innerHTML = '<div style="padding:14px; color:var(--text-secondary);">В Жилфонде ещё нет зданий.</div>';
                if (this.dom.dormSettingsTariff) this.dom.dormSettingsTariff.innerHTML = '';
                if (this.dom.dormSettingsMeters) this.dom.dormSettingsMeters.innerHTML = '';
                return;
            }
            if (sel) {
                sel.innerHTML = this._dormBuildings.map((b, i) => `<option value="${i}">${escapeHtml(b.label)} (${b.rooms})</option>`).join('');
                // По умолчанию — здание, выбранное в фильтре слева (если это общага).
                const v = (this.dom.dormFilterSelect?.value || '').trim();
                let idx = 0;
                if (v.startsWith('house:::')) {
                    const p = v.split(':::');
                    const f = this._dormBuildings.findIndex(b => b.type === 'house' && b.street === p[1] && b.house_number === p[2]);
                    if (f >= 0) idx = f;
                } else if (v) {
                    const f = this._dormBuildings.findIndex(b => b.type === 'dormitory' && b.dormitory_name === v);
                    if (f >= 0) idx = f;
                }
                sel.value = String(idx);
                sel.onchange = () => this._loadDormOverview(this._dormBuildings[Number(sel.value)]);
                await this._loadDormOverview(this._dormBuildings[idx]);
            }
        } catch (e) {
            if (sel) sel.innerHTML = '<option value="">Ошибка загрузки зданий</option>';
            if (this.dom.dormSettingsStats) this.dom.dormSettingsStats.innerHTML = `<div style="color:var(--danger-color); padding:14px;">Ошибка: ${escapeHtml(e.message)}</div>`;
        }
    },

    // Сводка по выбранному зданию (дом или общага) + рендер тарифа/счётчиков.
    async _loadDormOverview(building) {
        if (!building) return;
        this._dormSettingsBuilding = building;
        if (this.dom.dormSettingsTitle) this.dom.dormSettingsTitle.textContent = building.label;
        const spin = '<div style="padding:14px; text-align:center; color:var(--text-secondary);"><i class="fa-solid fa-spinner fa-spin"></i> Загрузка…</div>';
        [this.dom.dormSettingsStats, this.dom.dormSettingsTariff, this.dom.dormSettingsMeters].forEach(b => { if (b) b.innerHTML = spin; });
        const params = building.type === 'house'
            ? `street=${encodeURIComponent(building.street)}&house_number=${encodeURIComponent(building.house_number)}`
            : `dormitory_name=${encodeURIComponent(building.dormitory_name)}`;
        try {
            const data = await api.get(`/rooms/dormitory-overview?${params}`);
            this._renderDormSettings(data);
        } catch (e) {
            if (this.dom.dormSettingsStats) this.dom.dormSettingsStats.innerHTML = `<div style="color:var(--danger-color); padding:14px;">Ошибка: ${escapeHtml(e.message)}</div>`;
            if (this.dom.dormSettingsTariff) this.dom.dormSettingsTariff.innerHTML = '';
            if (this.dom.dormSettingsMeters) this.dom.dormSettingsMeters.innerHTML = '';
        }
    },

    _renderDormSettings(data) {
        const s = data.stats || {};
        const tile = (label, val, color) => `<div style="flex:1; min-width:88px; background:var(--bg-page); border-radius:8px; padding:8px 10px; text-align:center;"><div style="font-size:18px; font-weight:700; color:${color};">${val}</div><div style="font-size:10px; color:var(--text-secondary);">${label}</div></div>`;
        this.dom.dormSettingsStats.innerHTML = `
            <div style="display:flex; gap:8px; flex-wrap:wrap; margin-bottom:8px;">
                ${tile('Комнат', s.total_rooms, '#2563eb')}
                ${tile('Жильцов', `${s.total_residents}/${s.total_capacity}`, '#4338ca')}
                ${tile('Заполнено', s.occupancy_pct + '%', '#059669')}
                ${tile('Семейных', s.family, '#7c3aed')}
                ${tile('Холостяков', s.single, '#ea580c')}
                ${tile('Хол. квартир', s.singles_apartments, '#ea580c')}
            </div>
            <div style="font-size:12px; color:var(--text-secondary);">Пустых: ${s.empty} · частичных: ${s.partial} · полных: ${s.full} · переполнено: ${s.overcrowded}</div>
            ${(data.by_tariff || []).length ? `<div style="margin-top:8px; font-size:12px;"><b>По тарифам:</b> ${data.by_tariff.map(t => `${escapeHtml(t.tariff_name)} — ${t.rooms}`).join(' · ')}</div>` : ''}
            <div style="margin-top:4px; font-size:12px;"><b>Счётчики:</b> ГВС ${data.by_meter.hw} · ХВС ${data.by_meter.cw} · эл ${data.by_meter.el} · без счётчиков ${data.by_meter.none} (из ${data.by_meter.total})</div>
        `;
        // Тариф
        const tariffs = data.available_tariffs || [];
        const opts = ['<option value="">— Не менять —</option>']
            .concat(tariffs.map(t => `<option value="${t.id}" ${data.current_tariff_id === t.id ? 'selected' : ''}>${escapeHtml(t.name)}</option>`)).join('');
        const curMixed = data.current_tariff_id === null && (data.by_tariff || []).length > 1;
        this.dom.dormSettingsTariff.innerHTML = `
            <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
                <select id="dormTariffSelect" style="flex:1; min-width:220px; padding:6px 8px;">${opts}</select>
                <button id="btnApplyDormTariff" class="action-btn primary-btn" style="padding:6px 12px; font-size:12px;">Применить тариф</button>
            </div>
            ${curMixed ? '<div style="font-size:11px; color:#b45309; margin-top:4px;">⚠ Сейчас у комнат РАЗНЫЕ тарифы.</div>' : ''}
            <div id="dormTariffCharges" style="margin-top:8px;"></div>
        `;
        const sel = document.getElementById('dormTariffSelect');
        const chargesBox = document.getElementById('dormTariffCharges');
        const renderCharges = () => {
            const t = tariffs.find(x => String(x.id) === sel.value);
            if (!t) { chargesBox.innerHTML = ''; return; }
            const chips = (t.charges || []).length
                ? t.charges.map(c => `<span style="background:#ede9fe; color:#5b21b6; padding:2px 8px; border-radius:8px; font-size:11px; margin-right:4px;">${escapeHtml(c)}</span>`).join('')
                : '<span style="color:var(--text-secondary);">ничего не начисляет</span>';
            chargesBox.innerHTML = `<div style="font-size:12px;">Начисляет: ${chips}</div>`;
        };
        sel.addEventListener('change', renderCharges);
        renderCharges();
        document.getElementById('btnApplyDormTariff').addEventListener('click', () => this.applyDormTariff(sel.value));
        // Счётчики
        const m = data.current_meters || { has_hw_meter: true, has_cw_meter: true, has_el_meter: true };
        const mixedMeters = !data.current_meters;
        const cb = (id, label, checked) => `<label style="display:inline-flex; align-items:center; gap:6px; cursor:pointer; font-size:13px;"><input type="checkbox" id="${id}" ${checked ? 'checked' : ''}> ${label}</label>`;
        this.dom.dormSettingsMeters.innerHTML = `
            <div style="display:flex; gap:18px; flex-wrap:wrap; align-items:center; margin-bottom:8px;">
                ${cb('dormHasHw', '🔥 ГВС', m.has_hw_meter)}
                ${cb('dormHasCw', '❄ ХВС', m.has_cw_meter)}
                ${cb('dormHasEl', '⚡ Электр.', m.has_el_meter)}
                <button id="btnApplyDormMeters" class="action-btn primary-btn" style="margin-left:auto; padding:6px 12px; font-size:12px;">Применить счётчики</button>
            </div>
            <div style="display:flex; gap:6px; flex-wrap:wrap;">
                <button type="button" class="action-btn secondary-btn dorm-meter-preset" data-preset="all" style="padding:3px 8px; font-size:11px;">Все</button>
                <button type="button" class="action-btn secondary-btn dorm-meter-preset" data-preset="water" style="padding:3px 8px; font-size:11px;">Только вода</button>
                <button type="button" class="action-btn secondary-btn dorm-meter-preset" data-preset="none" style="padding:3px 8px; font-size:11px;">Ничего</button>
            </div>
            ${mixedMeters ? '<div style="font-size:11px; color:#b45309; margin-top:4px;">⚠ Сейчас у комнат РАЗНЫЙ набор счётчиков.</div>' : ''}
        `;
        this.dom.dormSettingsMeters.querySelectorAll('.dorm-meter-preset').forEach(btn => {
            btn.addEventListener('click', () => {
                const p = btn.dataset.preset;
                document.getElementById('dormHasHw').checked = (p === 'all' || p === 'water');
                document.getElementById('dormHasCw').checked = (p === 'all' || p === 'water');
                document.getElementById('dormHasEl').checked = (p === 'all');
            });
        });
        document.getElementById('btnApplyDormMeters').addEventListener('click', () => this.applyDormMeters());
    },

    async applyDormTariff(tariffIdStr) {
        const b = this._dormSettingsBuilding;
        if (!b) return;
        if (!tariffIdStr) return toast('Выберите тариф', 'info');
        const tariffId = Number(tariffIdStr);
        if (!await showConfirm(
            `Назначить выбранный тариф ВСЕМ помещениям «${b.label}»?\nПрименится к каждому жильцу автоматически.`,
            { title: 'Тариф дома', confirmText: 'Применить' }
        )) return;
        try {
            const body = b.type === 'house'
                ? { street: b.street, house_number: b.house_number, tariff_id: tariffId }
                : { dormitory_name: b.dormitory_name, tariff_id: tariffId };
            const res = await api.post('/tariffs/assign-to-dormitory', body);
            toast(`Тариф назначен. Помещений: ${res.rooms_affected ?? '—'}`, 'success');
            this._loadDormOverview(b);
            this.table.load();
        } catch (e) { toast('Ошибка: ' + (e.message || ''), 'error'); }
    },

    async applyDormMeters() {
        const b = this._dormSettingsBuilding;
        if (!b) return;
        const meters = {
            has_hw_meter: document.getElementById('dormHasHw').checked,
            has_cw_meter: document.getElementById('dormHasCw').checked,
            has_el_meter: document.getElementById('dormHasEl').checked,
        };
        const body = b.type === 'house'
            ? { street: b.street, house_number: b.house_number, ...meters }
            : { dormitory_name: b.dormitory_name, ...meters };
        const on = [meters.has_hw_meter && 'ГВС', meters.has_cw_meter && 'ХВС', meters.has_el_meter && 'электр']
            .filter(Boolean).join(', ') || 'нет счётчиков';
        if (!await showConfirm(`Применить счётчики (${on}) ко ВСЕМ помещениям «${b.label}»?`, { title: 'Счётчики дома', confirmText: 'Применить' })) return;
        try {
            const res = await api.post('/rooms/bulk-meter-config', body);
            toast(`Обновлено помещений: ${res.updated_rooms}`, 'success');
            this._loadDormOverview(b);
            this.table.load();
        } catch (e) { toast('Ошибка: ' + (e.message || ''), 'error'); }
    },

    async handleSave(e) {
        e.preventDefault();
        const btn = this.modal.form.querySelector('.confirm-btn');
        const id = this.modal.inputs.id.value;
        const maxCapVal = this.modal.inputs.maxCapacity?.value;
        const isHouse = !!this.modal.inputs.placeTypeHouse?.checked;

        // housing_001: payload зависит от типа помещения. Backend
        // схема RoomCreate проверит обязательность нужных полей и
        // вернёт человечную ошибку при пробелах/null.
        const data = {
            place_type: isHouse ? 'house' : 'dormitory',
            apartment_area: parseFloat(this.modal.inputs.area.value),
            total_room_residents: parseInt(this.modal.inputs.cap.value),
            // Тариф НЕ шлём из формы комнаты — он свойство ДОМА (здесь read-only),
            // ставится только через «Настройки дома» (на всё здание сразу).
            // Конфигурация счётчиков комнаты (наследуется жильцами, meters_002).
            has_hw_meter: this.modal.inputs.hasHw ? !!this.modal.inputs.hasHw.checked : true,
            has_cw_meter: this.modal.inputs.hasCw ? !!this.modal.inputs.hasCw.checked : true,
            has_el_meter: this.modal.inputs.hasEl ? !!this.modal.inputs.hasEl.checked : true,
        };

        if (isHouse) {
            data.street = (this.modal.inputs.street?.value || '').trim();
            data.house_number = (this.modal.inputs.houseNumber?.value || '').trim();
            data.apartment_number = (this.modal.inputs.apartmentNumber?.value || '').trim();
            // Серийники счётчиков и is_singles — не для домов. Шлём null/false
            // явно, чтобы при редактировании старая комната-общага сбросила
            // эти значения после смены типа.
            data.hw_meter_serial = null;
            data.cw_meter_serial = null;
            data.el_meter_serial = null;
            data.is_singles_apartment = false;
            data.max_capacity = maxCapVal ? parseInt(maxCapVal) : null;
        } else {
            data.dormitory_name = (this.modal.inputs.dorm.value || '').trim();
            data.room_number = (this.modal.inputs.num.value || '').trim();
            data.hw_meter_serial = (this.modal.inputs.hw.value || '').trim() || null;
            data.cw_meter_serial = (this.modal.inputs.cw.value || '').trim() || null;
            data.el_meter_serial = (this.modal.inputs.el.value || '').trim() || null;
            data.is_singles_apartment = !!this.modal.inputs.isSingles?.checked;
            data.max_capacity = maxCapVal ? parseInt(maxCapVal) : null;
        }

        setLoading(btn, true, 'Сохранение...');
        try {
            if (id) {
                await api.put(`/rooms/${id}`, data);
                toast('Помещение обновлено', 'success');
            } else {
                await api.post('/rooms', data);
                toast(isHouse ? 'Дом / квартира добавлена' : 'Помещение добавлено', 'success');
            }
            this.modal.window.classList.remove('open');
            this.table.refresh();
            this.loadDormitories();
            this.loadStreets();
            this.loadStats();
        } catch (err) {
            toast(err.message, 'error');
        } finally {
            setLoading(btn, false);
        }
    },

    async deleteRoom(id) {
        if (!await showConfirm('ВНИМАНИЕ! Вы уверены, что хотите удалить помещение? Это возможно только если к нему не привязаны жильцы и нет истории показаний.', { danger: true, confirmText: 'Удалить' })) return;
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
        this.meter.roomName.textContent = formatRoomAddress(room);
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
        if (!await showConfirm('Вы уверены? Система рассчитает потребление по старому счетчику и установит новые базовые значения. Это действие нельзя отменить.')) return;

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
        this.initial.roomName.textContent = formatRoomAddress(room);

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