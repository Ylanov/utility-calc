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
    },

    cacheDOM() {
        this.dom = {
            addForm: document.getElementById('addUserForm'),
            importInput: document.getElementById('importUsersFile'),
            btnImport: document.getElementById('btnImportUsers'),
            btnRefresh: document.getElementById('btnRefreshUsers'),
            btnDownloadTemplate: document.getElementById('btnDownloadTemplate'),
            newTariffSelect: document.getElementById('newTariffId'),

            newDormSelect: document.getElementById('newDormSelect'),
            newRoomSelect: document.getElementById('newRoomSelect'),
            newRoomInfo: document.getElementById('newRoomInfo'),
            infoArea: document.getElementById('infoArea'),
            infoCap: document.getElementById('infoCap'),
            infoHw: document.getElementById('infoHw'),
            infoCw: document.getElementById('infoCw'),
            infoEl: document.getElementById('infoEl')
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
        if (this.dom.btnRefresh) this.dom.btnRefresh.addEventListener('click', () => this.table?.refresh());
        if (this.dom.btnDownloadTemplate) {
            this.dom.btnDownloadTemplate.addEventListener('click', (e) => {
                e.preventDefault();
                api.download('/users/export/template', 'Import_Template.xlsx');
            });
        }

        // Формы CRUD
        if (this.dom.addForm) this.dom.addForm.addEventListener('submit', (e) => this.handleAdd(e));
        if (this.modal.form) this.modal.form.addEventListener('submit', (e) => this.handleEditSubmit(e));
        if (this.modal.btnClose) this.modal.btnClose.addEventListener('click', () => this.closeModal());

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

            renderRow: (user) => {
                const address = user.room ? `${user.room.dormitory_name} / ком. ${user.room.room_number}` : '-';
                const area = user.room && user.room.apartment_area ? Number(user.room.apartment_area).toFixed(1) : '-';
                const totalResidents = user.room ? user.room.total_room_residents : 1;

                return el('tr', { class: 'hover:bg-gray-50 transition-colors' },
                    el('td', { class: 'text-gray-500 text-sm' }, `#${user.id}`),
                    el('td', {}, el('div', { style: { fontWeight: '600' } }, user.username)),
                    el('td', {}, el('span', { class: `role-badge ${user.role}` }, user.role)),
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
            workplace: document.getElementById('workplace').value.trim()
        };

        setLoading(button, true, 'Создание...');

        try {
            await api.post('/users', data);
            toast('Пользователь успешно создан', 'success');
            this.dom.addForm.reset();

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