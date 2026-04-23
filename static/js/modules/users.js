// static/js/modules/users.js
import { api } from '../core/api.js';
import { el, toast, setLoading } from '../core/dom.js';
import { TableController } from '../core/table-controller.js';

// Импорт вынесенной логики
import { handleDormChange, handleRoomChange } from './users-ui.js';
import { handleImport, openRelocateModal, handleRelocateSubmit } from './users-actions.js';

export const UsersModule = {
    table: null,
    isInitialized: false,
    tariffs: [],
    dormsCache:[],
    roomsCache: {},

    async init() {
        this.cacheDOM();

        if (!this.isInitialized) {
            this.bindEvents();
            this.isInitialized = true;
        }

        await Promise.all([
            this.loadTariffs(),
            this.loadDormitories()
        ]);

        this.initTable();
        // Аналитика — параллельно с первым запросом таблицы, не блокирует
        this.loadStats();
    },

    cacheDOM() {
        this.dom = {
            addForm: document.getElementById('addUserForm'),
            importInput: document.getElementById('importUsersFile'),
            btnImport: document.getElementById('btnImportUsers'),
            btnRefresh: document.getElementById('btnRefreshUsers'),
            btnDownloadTemplate: document.getElementById('btnDownloadTemplate'),
            btnExport: document.getElementById('btnExportUsers'),
            newTariffSelect: document.getElementById('newTariffId'),
            // Новые поля формы создания
            newResidentType: document.getElementById('newResidentType'),
            newBillingMode: document.getElementById('newBillingMode'),

            newDormSelect: document.getElementById('newDormSelect'),
            newRoomSelect: document.getElementById('newRoomSelect'),
            newRoomInfo: document.getElementById('newRoomInfo'),
            infoArea: document.getElementById('infoArea'),
            infoCap: document.getElementById('infoCap'),
            infoHw: document.getElementById('infoHw'),
            infoCw: document.getElementById('infoCw'),
            infoEl: document.getElementById('infoEl'),
            // Фильтры в toolbar
            filterResidentType: document.getElementById('filterResidentType'),
            filterBillingMode: document.getElementById('filterBillingMode'),
            filterDormitory: document.getElementById('filterDormitory'),
            // KPI + распределение
            statsHost: document.getElementById('usersStats'),
            distributionHost: document.getElementById('usersDistribution'),
        };

        this.modal = {
            window: document.getElementById('userEditModal'),
            form: document.getElementById('editUserForm'),
            inputs: {
                id: document.getElementById('editUserId'),
                username: document.getElementById('editUsername'),
                password: document.getElementById('editPassword'),
                role: document.getElementById('editRole'),
                tariff: document.getElementById('editTariffId'),
                residents: document.getElementById('editResidentsCount'),
                work: document.getElementById('editWorkplace'),

                dormSelect: document.getElementById('editDormSelect'),
                roomSelect: document.getElementById('editRoomSelect'),
                roomInfo: document.getElementById('editRoomInfo'),
                infoArea: document.getElementById('editInfoArea'),
                infoCap: document.getElementById('editInfoCap'),
                infoHw: document.getElementById('editInfoHw'),
                infoCw: document.getElementById('editInfoCw'),
                infoEl: document.getElementById('editInfoEl'),
                residentType: document.getElementById('editResidentType'),
                billingMode: document.getElementById('editBillingMode'),
                btnHistory: document.getElementById('btnShowResidenceHistory'),
                historyContainer: document.getElementById('editResidenceHistory'),
            },
            btnClose: document.querySelector('#userEditModal .close-btn')
        };

        // Новая единая логика выселения и переезда
        this.rel = {
            modal: document.getElementById('relocateModal'),
            form: document.getElementById('relocateForm'),
            userId: document.getElementById('relUserId'),
            userName: document.getElementById('relUserName'),
            currentAddress: document.getElementById('relCurrentAddress'),
            totalDays: document.getElementById('relTotalDays'),
            daysLived: document.getElementById('relDaysLived'),
            hot: document.getElementById('relHot'),
            cold: document.getElementById('relCold'),
            elect: document.getElementById('relElect'),
            destinationBlock: document.getElementById('relDestinationBlock'),
            dormSelect: document.getElementById('relDormSelect'),
            roomSelect: document.getElementById('relRoomSelect'),
            btnClose: document.getElementById('btnCloseRelocate'),
            btnCancel: document.getElementById('btnCancelRelocate'),
            btnSubmit: document.getElementById('btnSubmitRelocate')
        };
    },

    bindEvents() {
        // Таблица и экспорт
        if (this.dom.btnRefresh) this.dom.btnRefresh.addEventListener('click', () => {
            this.table?.refresh();
            this.loadStats();
        });
        if (this.dom.btnDownloadTemplate) {
            this.dom.btnDownloadTemplate.addEventListener('click', (e) => {
                e.preventDefault();
                api.download('/users/export/template', 'Import_Template.xlsx');
            });
        }
        // Excel-экспорт видимого списка с учётом фильтров
        if (this.dom.btnExport) {
            this.dom.btnExport.addEventListener('click', (e) => {
                e.preventDefault();
                const qs = new URLSearchParams();
                const search = document.getElementById('usersSearchInput')?.value.trim();
                if (search) qs.set('search', search);
                if (this.dom.filterResidentType?.value) qs.set('resident_type', this.dom.filterResidentType.value);
                if (this.dom.filterBillingMode?.value) qs.set('billing_mode', this.dom.filterBillingMode.value);
                if (this.dom.filterDormitory?.value) qs.set('dormitory', this.dom.filterDormitory.value);
                api.download('/users/export/list?' + qs.toString(), 'residents_export.xlsx');
            });
        }

        // Формы CRUD
        if (this.dom.addForm) this.dom.addForm.addEventListener('submit', (e) => this.handleAdd(e));
        if (this.modal.form) this.modal.form.addEventListener('submit', (e) => this.handleEditSubmit(e));
        if (this.modal.btnClose) this.modal.btnClose.addEventListener('click', () => this.closeModal());

        // Авто-синхронизация типа жильца ↔ режима оплаты в форме создания
        // (single по умолчанию per_capita; family по умолчанию by_meter).
        if (this.dom.newResidentType && this.dom.newBillingMode) {
            this.dom.newResidentType.addEventListener('change', (e) => {
                this.dom.newBillingMode.value =
                    e.target.value === 'single' ? 'per_capita' : 'by_meter';
            });
        }

        // Фильтры таблицы: резидент тип / режим / общежитие
        [this.dom.filterResidentType, this.dom.filterBillingMode, this.dom.filterDormitory].forEach(el => {
            el?.addEventListener('change', () => this.table?.refresh());
        });

        // Экспорт логики действий в вынесенные модули
        if (this.dom.btnImport) this.dom.btnImport.addEventListener('click', (e) => {
            e.preventDefault();
            handleImport(this.dom.importInput, this.dom.btnImport, this.table);
        });

        // Логика единого окна переселения/выселения
        if (this.rel.form) {
            this.rel.form.addEventListener('submit', (e) => handleRelocateSubmit(e, this.rel, this.table));
            this.rel.btnClose.addEventListener('click', (e) => { e.preventDefault(); this.rel.modal.classList.remove('open'); });
            this.rel.btnCancel.addEventListener('click', (e) => { e.preventDefault(); this.rel.modal.classList.remove('open'); });

            // Переключение радио-кнопок (Выселить / Переселить)
            const radios = this.rel.form.querySelectorAll('input[name="relAction"]');
            radios.forEach(r => r.addEventListener('change', (e) => {
                if (e.target.value === 'move') {
                    this.rel.destinationBlock.style.display = 'block';
                    this.rel.dormSelect.required = true;
                    this.rel.roomSelect.required = true;
                } else {
                    this.rel.destinationBlock.style.display = 'none';
                    this.rel.dormSelect.required = false;
                    this.rel.roomSelect.required = false;
                }
            }));

            // Каскад для выбора общаги в модалке переселения
            this.rel.dormSelect.addEventListener('change', (e) => {
                handleDormChange(e.target.value, this.rel.roomSelect, null, this.roomsCache);
            });
        }

        // ====================================================
        // КАСКАДНЫЕ СЕЛЕКТЫ (Загрузка комнат при выборе общаги)
        // ====================================================

        // Для формы создания
        if (this.dom.newDormSelect) {
            this.dom.newDormSelect.addEventListener('change', (e) => {
                handleDormChange(e.target.value, this.dom.newRoomSelect, this.dom.newRoomInfo, this.roomsCache);
            });
        }
        if (this.dom.newRoomSelect) {
            this.dom.newRoomSelect.addEventListener('change', (e) => {
                handleRoomChange(e.target.value, this.dom.newDormSelect.value, this.dom, this.roomsCache);
            });
        }

        // Для модалки редактирования
        if (this.modal.inputs.dormSelect) {
            this.modal.inputs.dormSelect.addEventListener('change', (e) => {
                handleDormChange(e.target.value, this.modal.inputs.roomSelect, this.modal.inputs.roomInfo, this.roomsCache);
            });
        }
        if (this.modal.inputs.roomSelect) {
            this.modal.inputs.roomSelect.addEventListener('change', (e) => {
                handleRoomChange(e.target.value, this.modal.inputs.dormSelect.value, this.modal.inputs, this.roomsCache);
            });
        }
    },

    async loadTariffs() {
        try {
            const cached = sessionStorage.getItem('tariffs_cache');
            if (cached) {
                this.tariffs = JSON.parse(cached);
                this.populateTariffSelects();
                return;
            }

            this.tariffs = await api.get('/tariffs');
            sessionStorage.setItem('tariffs_cache', JSON.stringify(this.tariffs));
            this.populateTariffSelects();
        } catch (error) {
            toast('Не удалось загрузить список тарифов', 'error');
        }
    },

    populateTariffSelects() {
        const optionsHtml = '<option value="">Базовый тариф (По умолчанию)</option>' +
            this.tariffs.map(t => `<option value="${t.id}">${t.name}</option>`).join('');

        if (this.dom.newTariffSelect) this.dom.newTariffSelect.innerHTML = optionsHtml;
        if (this.modal.inputs.tariff) this.modal.inputs.tariff.innerHTML = optionsHtml;
    },

    async loadDormitories() {
        try {
            this.dormsCache = await api.get('/rooms/dormitories');
            const options = '<option value="">-- Выберите общежитие --</option>' +
                            this.dormsCache.map(d => `<option value="${d}">${d}</option>`).join('');

            if (this.dom.newDormSelect) this.dom.newDormSelect.innerHTML = options;
            if (this.modal.inputs.dormSelect) this.modal.inputs.dormSelect.innerHTML = '<option value="">Не привязан к комнате</option>' + options;
        } catch (error) {
            toast('Ошибка загрузки списка общежитий', 'error');
        }
    },

    initTable() {
        const self = this;
        this.table = new TableController({
            endpoint: '/users',
            dom: {
                tableBody: 'usersTableBody',
                searchInput: 'usersSearchInput',
                limitSelect: 'usersLimitSelect',
                prevBtn: 'btnPrevUsers',
                nextBtn: 'btnNextUsers',
                pageInfo: 'usersPageInfo'
            },

            // Передаём дополнительные фильтры в каждый запрос.
            // TableController ожидает getExtraParams() — если в базовом
            // контроллере этого нет, он будет вызван и проигнорирован.
            getExtraParams: () => {
                const p = {};
                const rt = self.dom.filterResidentType?.value;
                const bm = self.dom.filterBillingMode?.value;
                const dn = self.dom.filterDormitory?.value;
                if (rt) p.resident_type = rt;
                if (bm) p.billing_mode = bm;
                if (dn) p.dormitory = dn;
                return p;
            },

            renderRow: (user) => {
                const address = user.room ? `${user.room.dormitory_name} / ком. ${user.room.room_number}` : '-';
                const area = user.room && user.room.apartment_area ? Number(user.room.apartment_area).toFixed(1) : '-';
                const totalResidents = user.room ? user.room.total_room_residents : 1;

                // Бейджи нового доменного слоя: тип жильца + режим оплаты.
                // family/single → иконка семьи или человека;
                // by_meter/per_capita → счётчик или койко-место.
                const rt = user.resident_type || 'family';
                const bm = user.billing_mode || 'by_meter';
                const typeChip = rt === 'single'
                    ? el('span', { title: 'Холостяк', class: 'chip', style: { background: '#fef3c7', color: '#92400e', padding: '2px 8px', borderRadius: '10px', fontSize: '11px', fontWeight: '600' } }, '👤 Холост.')
                    : el('span', { title: 'Семейный', class: 'chip', style: { background: '#dbeafe', color: '#1e40af', padding: '2px 8px', borderRadius: '10px', fontSize: '11px', fontWeight: '600' } }, '👨‍👩‍👧 Семья');
                const modeChip = bm === 'per_capita'
                    ? el('span', { title: 'Койко-место (фикс. сумма)', class: 'chip', style: { background: '#ede9fe', color: '#5b21b6', padding: '2px 8px', borderRadius: '10px', fontSize: '11px', fontWeight: '600' } }, '🛏 Койко')
                    : el('span', { title: 'По счётчикам', class: 'chip', style: { background: '#d1fae5', color: '#065f46', padding: '2px 8px', borderRadius: '10px', fontSize: '11px', fontWeight: '600' } }, '📊 Счёт.');

                return el('tr', { class: 'hover:bg-gray-50 transition-colors' },
                    el('td', { class: 'text-gray-500 text-sm' }, `#${user.id}`),
                    el('td', {}, el('div', { style: { fontWeight: '600' } }, user.username)),
                    el('td', {}, el('span', { class: `role-badge ${user.role}` }, user.role)),
                    el('td', { class: 'text-center' }, typeChip),
                    el('td', { class: 'text-center' }, modeChip),
                    el('td', {}, address),
                    el('td', {}, area),
                    el('td', { class: 'text-center text-sm' }, `${user.residents_count} / ${totalResidents}`),
                    el('td', {}, user.workplace || '-'),
                    el('td', { class: 'text-center' },
                        // Единая кнопка: Выселение / Переезд
                        el('button', {
                            class: 'btn-icon',
                            title: 'Выселение / Переезд',
                            style: { marginRight: '5px', background: '#eff6ff', color: '#1e40af', borderColor: '#bfdbfe' },
                            onclick: () => openRelocateModal(user, this.rel, this.dormsCache)
                        }, '🚚'),
                        el('button', {
                            class: 'btn-icon',
                            title: 'Договоры найма',
                            style: { marginRight: '5px', background: '#f5f3ff', color: '#7c3aed', borderColor: '#ddd6fe' },
                            onclick: () => this.openContractsModal(user)
                        }, '📄'),
                        el('button', {
                            class: 'btn-icon btn-edit',
                            title: 'Редактировать',
                            style: { marginRight: '5px' },
                            onclick: () => this.openEditModal(user.id)
                        }, '✎'),
                        el('button', {
                            class: 'btn-icon btn-delete',
                            title: 'Удалить',
                            onclick: () => this.deleteUser(user.id)
                        }, '🗑')
                    )
                );
            }
        });

        this.table.init();
    },

    // ============ АНАЛИТИКА ============
    /**
     * Загружает /users/stats и рендерит KPI-карточки + распределения.
     * Вызывается один раз при открытии вкладки и при refresh/создании.
     */
    async loadStats() {
        if (!this.dom.statsHost) return;
        try {
            const s = await api.get('/users/stats');
            this._renderStats(s);
            this._renderDistribution(s);
            // Заполняем фильтр общежитий (один раз)
            if (this.dom.filterDormitory && this.dom.filterDormitory.options.length <= 1) {
                const opts = (s.by_dormitory || [])
                    .map(d => `<option value="${this._escape(d.name)}">${this._escape(d.name)} (${d.count})</option>`)
                    .join('');
                this.dom.filterDormitory.insertAdjacentHTML('beforeend', opts);
            }
        } catch (e) {
            this.dom.statsHost.innerHTML = `<div style="color:var(--danger-color); padding:14px; grid-column: 1/-1;">Статистика недоступна: ${this._escape(e.message)}</div>`;
        }
    },

    _escape(s) {
        if (s == null) return '';
        return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    },

    _fmtMoney(v) {
        return Number(v || 0).toLocaleString('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + ' ₽';
    },

    _renderStats(s) {
        const card = (label, value, color, hint) => `
            <div style="background:var(--bg-card); border:1px solid var(--border-color); border-radius:10px; padding:14px;">
                <div style="font-size:11px; color:var(--text-secondary); text-transform:uppercase; letter-spacing:.5px;">${this._escape(label)}</div>
                <div style="font-size:22px; font-weight:700; color:${color}; margin:4px 0 2px;">${value}</div>
                ${hint ? `<div style="font-size:11px; color:var(--text-tertiary);">${this._escape(hint)}</div>` : ''}
            </div>`;

        const types = s.by_resident_type || {};
        const modes = s.by_billing_mode || {};
        this.dom.statsHost.innerHTML = [
            card('Всего жильцов', s.total_users || 0, '#2563eb', `${s.with_room} с комнатой · ${s.without_room} без`),
            card('Семейных', types.family || 0, '#1e40af', 'платят по счётчикам'),
            card('Холостяков', types.single || 0, '#92400e', 'койко-место'),
            card('По счётчикам', modes.by_meter || 0, '#065f46', 'by_meter'),
            card('За койко-место', modes.per_capita || 0, '#5b21b6', 'per_capita'),
            card('Общий долг', this._fmtMoney(s.total_debt), s.total_debt > 0 ? '#dc2626' : '#10b981', 'из последних начислений'),
            card('Переплаты', this._fmtMoney(s.total_overpayment), s.total_overpayment > 0 ? '#7c3aed' : '#9ca3af', 'авансы'),
        ].join('');
    },

    _renderDistribution(s) {
        if (!this.dom.distributionHost) return;

        // Блок «По общежитиям» (горизонтальный прогресс-бар)
        const dorms = (s.by_dormitory || []).slice(0, 8);
        const dormTotal = dorms.reduce((a, d) => a + d.count, 0) || 1;
        const dormRows = dorms.map(d => {
            const pct = Math.round(d.count / dormTotal * 100);
            return `
                <div style="display:flex; align-items:center; gap:10px; margin-bottom:6px; font-size:12px;">
                    <div style="width:140px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;" title="${this._escape(d.name)}">${this._escape(d.name)}</div>
                    <div style="flex:1; background:#f3f4f6; border-radius:4px; height:14px; overflow:hidden;">
                        <div style="width:${pct}%; height:100%; background:#3b82f6;"></div>
                    </div>
                    <div style="width:40px; text-align:right; font-weight:600;">${d.count}</div>
                </div>`;
        }).join('');

        // Блок «Топ должников»
        const debRows = (s.top_debtors || []).map(r => `
            <div style="display:flex; justify-content:space-between; padding:6px 0; border-bottom:1px solid var(--border-color); font-size:12px;">
                <div>
                    <div style="font-weight:600;">${this._escape(r.username)}</div>
                    <div style="color:var(--text-secondary); font-size:11px;">${this._escape(r.room || '—')}</div>
                </div>
                <div style="color:#dc2626; font-weight:700; white-space:nowrap;">${this._fmtMoney(r.amount)}</div>
            </div>`).join('') || '<div style="color:var(--text-secondary); font-size:12px; padding:8px 0;">— нет —</div>';

        // Блок «Топ переплат»
        const overRows = (s.top_overpaid || []).map(r => `
            <div style="display:flex; justify-content:space-between; padding:6px 0; border-bottom:1px solid var(--border-color); font-size:12px;">
                <div>
                    <div style="font-weight:600;">${this._escape(r.username)}</div>
                    <div style="color:var(--text-secondary); font-size:11px;">${this._escape(r.room || '—')}</div>
                </div>
                <div style="color:#7c3aed; font-weight:700; white-space:nowrap;">${this._fmtMoney(r.amount)}</div>
            </div>`).join('') || '<div style="color:var(--text-secondary); font-size:12px; padding:8px 0;">— нет —</div>';

        this.dom.distributionHost.innerHTML = `
            <div style="background:var(--bg-card); border:1px solid var(--border-color); border-radius:10px; padding:14px;">
                <h4 style="margin:0 0 10px 0; font-size:13px; color:var(--text-secondary); text-transform:uppercase;">
                    <i class="fa-solid fa-building"></i> Распределение по общежитиям
                </h4>
                ${dormRows || '<div style="color:var(--text-secondary); font-size:12px;">Нет данных</div>'}
            </div>
            <div style="background:var(--bg-card); border:1px solid var(--border-color); border-radius:10px; padding:14px;">
                <h4 style="margin:0 0 10px 0; font-size:13px; color:#991b1b; text-transform:uppercase;">
                    <i class="fa-solid fa-arrow-trend-down"></i> Топ должников
                </h4>
                ${debRows}
            </div>
            <div style="background:var(--bg-card); border:1px solid var(--border-color); border-radius:10px; padding:14px;">
                <h4 style="margin:0 0 10px 0; font-size:13px; color:#5b21b6; text-transform:uppercase;">
                    <i class="fa-solid fa-coins"></i> Топ переплат
                </h4>
                ${overRows}
            </div>`;
    },

    async handleAdd(event) {
        event.preventDefault();
        const button = this.dom.addForm.querySelector('button');
        const tariffIdVal = this.dom.newTariffSelect ? this.dom.newTariffSelect.value : null;
        const roomIdVal = this.dom.newRoomSelect ? this.dom.newRoomSelect.value : null;

        const data = {
            username: document.getElementById('newUsername').value.trim(),
            password: document.getElementById('newPassword').value,
            role: document.getElementById('newRole').value,
            tariff_id: tariffIdVal ? parseInt(tariffIdVal) : null,
            room_id: roomIdVal ? parseInt(roomIdVal) : null,
            residents_count: parseInt(document.getElementById('residentsCount').value) || 1,
            workplace: document.getElementById('workplace').value.trim(),
            // Новые поля: определяют способ расчёта (family/by_meter vs single/per_capita).
            // При single валидатор в модели сам выставит residents_count=1 и billing_mode=per_capita,
            // но мы всё равно шлём оба явно — чтобы не было сюрпризов если валидатор не сработает.
            resident_type: this.dom.newResidentType?.value || 'family',
            billing_mode: this.dom.newBillingMode?.value || 'by_meter',
        };

        setLoading(button, true, 'Создание...');

        try {
            await api.post('/users', data);
            toast('Пользователь успешно создан', 'success');
            this.dom.addForm.reset();
            // Обновляем статистику после создания
            this.loadStats();

            if (this.dom.newRoomSelect) {
                this.dom.newRoomSelect.disabled = true;
                this.dom.newRoomSelect.innerHTML = '<option value="">Сначала выберите общежитие</option>';
            }
            if (this.dom.newRoomInfo) {
                this.dom.newRoomInfo.style.display = 'none';
            }

            this.table.refresh();
        } catch (error) {
            toast(error.message, 'error');
        } finally {
            setLoading(button, false);
        }
    },

    async deleteUser(id) {
        if (!confirm('Вы действительно хотите удалить этого пользователя? (Мягкое удаление. История показаний сохранится за комнатой.)')) return;

        try {
            await api.delete(`/users/${id}`);
            toast('Пользователь удален', 'success');
            this.table.refresh();
        } catch (error) {
            toast(error.message, 'error');
        }
    },

    // =====================================================================
    // ДОГОВОРЫ НАЙМА — отдельная модалка на жильца (волна 4 фичи справок).
    // Показывает список договоров + форму загрузки. При заказе ФЛС поля
    // «дата/№ договора» автоматически берутся из активного договора.
    // =====================================================================
    _contractsUserId: null,

    openContractsModal(user) {
        this._contractsUserId = user.id;
        const modal = document.getElementById('contractsModal');
        if (!modal) return;
        document.getElementById('contractsUserLabel').textContent = user.username;
        // Сброс формы загрузки
        const form = document.getElementById('contractUploadForm');
        form?.reset();
        if (document.getElementById('contractActivate')) {
            document.getElementById('contractActivate').checked = true;
        }

        // Привязываем обработчики один раз — флаг сохраняется на модалке
        if (!modal.dataset.contractsBound) {
            modal.addEventListener('click', (e) => {
                if (e.target.closest('[data-contracts-close]') || e.target === modal) {
                    modal.classList.remove('open');
                }
            });
            form?.addEventListener('submit', (e) => this._submitContractUpload(e));
            document.getElementById('contractsList')?.addEventListener('click', (e) => {
                const dl = e.target.closest('[data-contract-download]');
                const act = e.target.closest('[data-contract-activate]');
                const del = e.target.closest('[data-contract-delete]');
                if (dl)  this._downloadContract(Number(dl.dataset.contractDownload));
                if (act) this._activateContract(Number(act.dataset.contractActivate));
                if (del) this._deleteContract(Number(del.dataset.contractDelete));
            });
            modal.dataset.contractsBound = '1';
        }

        modal.classList.add('open');
        this._loadContracts();
    },

    async _loadContracts() {
        const listEl = document.getElementById('contractsList');
        if (!listEl || !this._contractsUserId) return;
        listEl.innerHTML = '<div style="padding:14px; text-align:center; color:var(--text-secondary);"><i class="fa-solid fa-spinner fa-spin"></i> Загрузка…</div>';
        try {
            const rows = await api.get(`/admin/users/${this._contractsUserId}/rental-contracts`);
            this._renderContracts(rows);
        } catch (e) {
            listEl.innerHTML = `<div style="padding:14px; color:var(--danger-color);">Ошибка: ${String(e.message).replace(/</g,'&lt;')}</div>`;
        }
    },

    _renderContracts(rows) {
        const listEl = document.getElementById('contractsList');
        if (!rows || !rows.length) {
            listEl.innerHTML = `
                <div style="padding:20px; text-align:center; color:var(--text-secondary); font-size:13px;">
                    У жильца пока нет загруженных договоров. Заполните форму выше — при заказе справки
                    поля «дата/№ договора» будут автоматически подставляться.
                </div>`;
            return;
        }
        const esc = (s) => {
            if (s == null) return '';
            const d = document.createElement('div'); d.textContent = String(s); return d.innerHTML;
        };
        const fmtDate = (iso) => iso ? new Date(iso).toLocaleDateString('ru-RU') : '—';
        const fmtSize = (n) => {
            if (!n) return '';
            const mb = n / 1024 / 1024;
            return mb >= 1 ? `${mb.toFixed(1)} МБ` : `${Math.round(n / 1024)} КБ`;
        };

        // Отсортируем: активный сверху, дальше по дате подписания.
        const sorted = [...rows].sort((a, b) => {
            if (a.is_active !== b.is_active) return a.is_active ? -1 : 1;
            return String(b.signed_date || '').localeCompare(String(a.signed_date || ''));
        });

        listEl.innerHTML = sorted.map(c => {
            const hasFile = !!c.file_name;
            return `
            <div style="display:flex; gap:12px; align-items:center; padding:10px 12px; margin-bottom:8px;
                        background:${c.is_active ? '#f0fdf4' : 'var(--bg-card)'};
                        border:1px solid ${c.is_active ? '#bbf7d0' : 'var(--border-color)'};
                        border-radius:8px;">
                <div style="width:36px; height:36px; border-radius:8px;
                            background:${c.is_active ? '#10b981' : '#9ca3af'}22;
                            color:${c.is_active ? '#10b981' : '#6b7280'};
                            display:flex; align-items:center; justify-content:center; flex-shrink:0;">
                    <i class="fa-solid ${hasFile ? 'fa-file-pdf' : 'fa-file-lines'}"></i>
                </div>
                <div style="flex:1; min-width:0;">
                    <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
                        <b>№ ${esc(c.number || '—')}</b>
                        <span style="font-size:12px; color:var(--text-secondary);">от ${esc(fmtDate(c.signed_date))}</span>
                        ${c.valid_until ? `<span style="font-size:11px; color:var(--text-tertiary);">до ${esc(fmtDate(c.valid_until))}</span>` : ''}
                        ${c.is_active ? '<span style="font-size:10px; background:#d1fae5; color:#065f46; padding:2px 7px; border-radius:10px; font-weight:600;">АКТИВНЫЙ</span>' : ''}
                        ${hasFile ? '' : '<span style="font-size:10px; background:#fef3c7; color:#92400e; padding:2px 7px; border-radius:10px; font-weight:600;">без файла</span>'}
                    </div>
                    <div style="font-size:11px; color:var(--text-secondary); margin-top:2px;">
                        ${hasFile ? esc(c.file_name) + (c.file_size ? ' · ' + esc(fmtSize(c.file_size)) : '') : 'Файл не прикреплён'}
                        ${c.note ? ' · ' + esc(c.note) : ''}
                    </div>
                </div>
                <div style="display:flex; gap:4px; flex-shrink:0;">
                    ${hasFile ? `
                        <button class="icon-btn" data-contract-download="${c.id}" title="Скачать">
                            <i class="fa-solid fa-download"></i>
                        </button>` : ''}
                    ${c.is_active ? '' : `
                        <button class="icon-btn" data-contract-activate="${c.id}" title="Сделать активным"
                                style="color:#10b981;">
                            <i class="fa-solid fa-check"></i>
                        </button>`}
                    <button class="icon-btn" data-contract-delete="${c.id}" title="Удалить"
                            style="color:var(--danger-color);">
                        <i class="fa-solid fa-trash"></i>
                    </button>
                </div>
            </div>
        `;
        }).join('');
    },

    async _submitContractUpload(e) {
        e.preventDefault();
        if (!this._contractsUserId) return;

        const num = document.getElementById('contractNumber').value.trim();
        const signed = document.getElementById('contractSignedDate').value;
        if (!num) return toast('Укажите № договора', 'error');
        if (!signed) return toast('Укажите дату подписания', 'error');

        const until = document.getElementById('contractValidUntil').value;
        const note = document.getElementById('contractNote').value.trim();
        const activate = document.getElementById('contractActivate').checked;
        const file = document.getElementById('contractFile')?.files?.[0] || null;

        // Всё через multipart/form-data: номер/дата идут как Form-поля,
        // файл — опциональный; без файла можно сохранить только метаданные.
        const fd = new FormData();
        fd.append('number', num);
        fd.append('signed_date', signed);
        if (until) fd.append('valid_until', until);
        if (note) fd.append('note', note);
        fd.append('activate', activate ? 'true' : 'false');
        if (file) fd.append('file', file);

        const btn = e.target.querySelector('button[type="submit"]');
        const orig = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Загрузка…';
        try {
            await api.post(
                `/admin/users/${this._contractsUserId}/rental-contracts`,
                fd,
            );
            toast(file ? 'Договор загружен' : 'Данные договора сохранены', 'success');
            e.target.reset();
            document.getElementById('contractActivate').checked = true;
            this._loadContracts();
        } catch (err) {
            toast('Ошибка загрузки: ' + err.message, 'error');
        } finally {
            btn.disabled = false;
            btn.innerHTML = orig;
        }
    },

    async _downloadContract(contractId) {
        try {
            await api.download(`/admin/rental-contracts/${contractId}/download`, `contract_${contractId}.pdf`);
        } catch (e) {
            toast('Ошибка скачивания: ' + e.message, 'error');
        }
    },

    async _activateContract(contractId) {
        try {
            await api.post(`/admin/rental-contracts/${contractId}/activate`);
            toast('Договор активирован — остальные стали архивными', 'success');
            this._loadContracts();
        } catch (e) {
            toast('Ошибка: ' + e.message, 'error');
        }
    },

    async _deleteContract(contractId) {
        if (!confirm('Удалить договор безвозвратно? Файл будет стёрт из хранилища.')) return;
        try {
            await api.delete(`/admin/rental-contracts/${contractId}`);
            toast('Договор удалён', 'success');
            this._loadContracts();
        } catch (e) {
            toast('Ошибка: ' + e.message, 'error');
        }
    },

    async openEditModal(id) {
        try {
            const user = await api.get(`/users/${id}`);
            const inputs = this.modal.inputs;

            if (inputs.id) inputs.id.value = user.id;
            if (inputs.username) inputs.username.value = user.username;
            if (inputs.password) inputs.password.value = '';
            if (inputs.role) inputs.role.value = user.role;
            if (inputs.tariff) inputs.tariff.value = user.tariff_id || '';
            if (inputs.residents) inputs.residents.value = user.residents_count;
            if (inputs.work) inputs.work.value = user.workplace || '';
            if (inputs.residentType) inputs.residentType.value = user.resident_type || 'family';
            if (inputs.billingMode) inputs.billingMode.value = user.billing_mode || 'by_meter';

            // Авто-подбор billing_mode при смене типа: single → per_capita.
            // Админ может оставить выбор вручную (но прозрачно, не молча).
            if (inputs.residentType && inputs.billingMode) {
                inputs.residentType.onchange = () => {
                    inputs.billingMode.value =
                        inputs.residentType.value === 'single' ? 'per_capita' : 'by_meter';
                };
            }

            // История проживания — по кнопке (lazy load).
            if (inputs.btnHistory && inputs.historyContainer) {
                inputs.historyContainer.style.display = 'none';
                inputs.historyContainer.innerHTML = '';
                inputs.btnHistory.onclick = async () => {
                    const c = inputs.historyContainer;
                    if (c.style.display !== 'none') {
                        c.style.display = 'none';
                        return;
                    }
                    c.style.display = 'block';
                    c.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Загрузка…';
                    try {
                        const data = await api.get(`/users/${user.id}/residence-history`);
                        if (!data.items?.length) {
                            c.innerHTML = '<span style="color:var(--text-secondary);">История пока пустая.</span>';
                            return;
                        }
                        const fmt = (s) => s ? new Date(s).toLocaleDateString('ru-RU') : null;
                        c.innerHTML = data.items.map(it => `
                            <div style="display:flex; justify-content:space-between; align-items:center; padding:6px 0; border-bottom:1px solid var(--border-color);">
                                <div>
                                    <strong>${it.room || `комната #${it.room_id}`}</strong>
                                    ${it.is_current ? '<span style="margin-left:8px; padding:1px 6px; border-radius:8px; background:#d1fae5; color:#065f46; font-size:10px; font-weight:600;">сейчас</span>' : ''}
                                    ${it.note ? `<div style="color:var(--text-secondary); font-size:11px;">${it.note}</div>` : ''}
                                </div>
                                <div style="font-size:11px; color:var(--text-secondary); text-align:right;">
                                    ${fmt(it.moved_in_at) || '—'}
                                    ${it.moved_out_at ? ` → ${fmt(it.moved_out_at)}` : ' → сейчас'}
                                </div>
                            </div>`).join('');
                    } catch (e) {
                        c.innerHTML = `<span style="color:var(--danger-color);">Ошибка: ${e.message}</span>`;
                    }
                };
            }

            if (user.room) {
                if (inputs.dormSelect) {
                    inputs.dormSelect.value = user.room.dormitory_name;
                    await handleDormChange(user.room.dormitory_name, inputs.roomSelect, inputs.roomInfo, this.roomsCache);

                    if (inputs.roomSelect) {
                        inputs.roomSelect.value = user.room.id;
                        handleRoomChange(user.room.id, user.room.dormitory_name, inputs, this.roomsCache);
                    }
                }
            } else {
                if (inputs.dormSelect) inputs.dormSelect.value = '';
                if (inputs.roomSelect) {
                    inputs.roomSelect.innerHTML = '<option value="">Сначала выберите общежитие</option>';
                    inputs.roomSelect.disabled = true;
                }
                if (inputs.roomInfo) inputs.roomInfo.style.display = 'none';
            }

            this.modal.window.classList.add('open');

        } catch (error) {
            toast('Ошибка загрузки данных: ' + error.message, 'error');
        }
    },

    closeModal() {
        this.modal.window.classList.remove('open');
    },

    async handleEditSubmit(event) {
        event.preventDefault();
        const button = this.modal.form.querySelector('.confirm-btn');
        const id = this.modal.inputs.id.value;
        const tariffIdVal = this.modal.inputs.tariff ? this.modal.inputs.tariff.value : null;
        const roomIdVal = this.modal.inputs.roomSelect ? this.modal.inputs.roomSelect.value : null;

        const data = {
            username: this.modal.inputs.username.value.trim(),
            role: this.modal.inputs.role.value,
            tariff_id: tariffIdVal ? parseInt(tariffIdVal) : null,
            room_id: roomIdVal ? parseInt(roomIdVal) : null,
            residents_count: parseInt(this.modal.inputs.residents.value),
            workplace: this.modal.inputs.work.value.trim(),
            resident_type: this.modal.inputs.residentType?.value || 'family',
            billing_mode: this.modal.inputs.billingMode?.value || 'by_meter',
        };

        if (this.modal.inputs.password.value) {
            data.password = this.modal.inputs.password.value;
        }

        setLoading(button, true, 'Сохранение...');

        try {
            await api.put(`/users/${id}`, data);
            toast('Данные обновлены успешно', 'success');
            this.closeModal();
            this.table.refresh();
        } catch (error) {
            toast(error.message, 'error');
        } finally {
            setLoading(button, false);
        }
    }
};