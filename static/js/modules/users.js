// static/js/modules/users.js
import { api } from '../core/api.js';
import { el, toast, setLoading } from '../core/dom.js';
import { TableController } from '../core/table-controller.js';

export const UsersModule = {
    table: null,
    isInitialized: false,
    tariffs: [],
    dormsCache:[], // Кэш названий общежитий
    roomsCache: {}, // Кэш комнат по названию общежития { "Общежитие 1":[room1, room2] }

    async init() {
        this.cacheDOM();

        if (!this.isInitialized) {
            this.bindEvents();
            this.isInitialized = true;
        }

        // Параллельно загружаем тарифы и список общежитий из нового API Жилфонда
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

            // Новые селекты и блоки информации (Создание жильца)
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

                // Новые селекты и блоки информации (Редактирование жильца)
                dormSelect: document.getElementById('editDormSelect'),
                roomSelect: document.getElementById('editRoomSelect'),
                roomInfo: document.getElementById('editRoomInfo'),
                infoArea: document.getElementById('editInfoArea'),
                infoCap: document.getElementById('editInfoCap'),
                infoHw: document.getElementById('editInfoHw'),
                infoCw: document.getElementById('editInfoCw'),
                infoEl: document.getElementById('editInfoEl')
            },
            btnClose: document.querySelector('#userEditModal .close-btn')
        };

        this.otc = {
            modal: document.getElementById('oneTimeChargeModal'),
            form: document.getElementById('oneTimeChargeForm'),
            userId: document.getElementById('otcUserId'),
            userName: document.getElementById('otcUserName'),
            totalDays: document.getElementById('otcTotalDays'),
            daysLived: document.getElementById('otcDaysLived'),
            hot: document.getElementById('otcHot'),
            cold: document.getElementById('otcCold'),
            elect: document.getElementById('otcElect'),
            isMovingOut: document.getElementById('otcIsMovingOut'),
            btnClose: document.getElementById('btnCloseOneTime'),
            btnCancel: document.getElementById('btnCancelOneTime'),
            btnSubmit: document.getElementById('btnSubmitOneTime')
        };
    },

    bindEvents() {
        if (this.dom.btnRefresh) this.dom.btnRefresh.addEventListener('click', () => this.table?.refresh());
        if (this.dom.addForm) this.dom.addForm.addEventListener('submit', (e) => this.handleAdd(e));
        if (this.dom.btnImport) this.dom.btnImport.addEventListener('click', (e) => { e.preventDefault(); this.handleImport(this.dom.btnImport); });

        if (this.modal.form) this.modal.form.addEventListener('submit', (e) => this.handleEditSubmit(e));
        if (this.modal.btnClose) this.modal.btnClose.addEventListener('click', () => this.closeModal());

        if (this.otc.form) {
            this.otc.form.addEventListener('submit', (e) => this.handleOneTimeSubmit(e));
            this.otc.btnClose.addEventListener('click', (e) => { e.preventDefault(); this.otc.modal.classList.remove('open'); });
            this.otc.btnCancel.addEventListener('click', (e) => { e.preventDefault(); this.otc.modal.classList.remove('open'); });
        }

        // Обработчик скачивания шаблона Excel
        if (this.dom.btnDownloadTemplate) {
            this.dom.btnDownloadTemplate.addEventListener('click', (e) => {
                e.preventDefault();
                api.download('/users/export/template', 'Import_Template.xlsx');
            });
        }

        // ====================================================
        // КАСКАДНЫЕ СЕЛЕКТЫ (Загрузка комнат при выборе общаги)
        // ====================================================

        // Для формы создания
        if (this.dom.newDormSelect) {
            this.dom.newDormSelect.addEventListener('change', (e) => {
                this.handleDormChange(e.target.value, this.dom.newRoomSelect, this.dom.newRoomInfo);
            });
        }
        if (this.dom.newRoomSelect) {
            this.dom.newRoomSelect.addEventListener('change', (e) => {
                this.handleRoomChange(e.target.value, this.dom.newDormSelect.value, this.dom);
            });
        }

        // Для модалки редактирования
        if (this.modal.inputs.dormSelect) {
            this.modal.inputs.dormSelect.addEventListener('change', (e) => {
                this.handleDormChange(e.target.value, this.modal.inputs.roomSelect, this.modal.inputs.roomInfo);
            });
        }
        if (this.modal.inputs.roomSelect) {
            this.modal.inputs.roomSelect.addEventListener('change', (e) => {
                this.handleRoomChange(e.target.value, this.modal.inputs.dormSelect.value, this.modal.inputs);
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
            console.error('Ошибка загрузки профилей тарифов:', error);
            toast('Не удалось загрузить список тарифов', 'error');
        }
    },

    populateTariffSelects() {
        const optionsHtml = '<option value="">Базовый тариф (По умолчанию)</option>' +
            this.tariffs.map(t => `<option value="${t.id}">${t.name}</option>`).join('');

        if (this.dom.newTariffSelect) this.dom.newTariffSelect.innerHTML = optionsHtml;
        if (this.modal.inputs.tariff) this.modal.inputs.tariff.innerHTML = optionsHtml;
    },

    // Загрузка уникальных названий общежитий
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

    // Обработчик выбора общежития -> Загружает список комнат
    async handleDormChange(dormName, roomSelectEl, infoBoxEl) {
        if (infoBoxEl) infoBoxEl.style.display = 'none'; // Прячем инфо

        if (!dormName) {
            if (roomSelectEl) {
                roomSelectEl.innerHTML = '<option value="">Сначала выберите общежитие</option>';
                roomSelectEl.disabled = true;
            }
            return;
        }

        if (roomSelectEl) {
            roomSelectEl.innerHTML = '<option value="">Загрузка комнат...</option>';
            roomSelectEl.disabled = true;
        }

        try {
            // Кэшируем комнаты
            if (!this.roomsCache[dormName]) {
                const res = await api.get(`/rooms?dormitory=${encodeURIComponent(dormName)}&limit=1000`);
                this.roomsCache[dormName] = res.items;
            }

            const rooms = this.roomsCache[dormName];

            if (!roomSelectEl) return;

            if (rooms.length === 0) {
                roomSelectEl.innerHTML = '<option value="">В этом общежитии нет комнат</option>';
                return;
            }

            roomSelectEl.innerHTML = '<option value="">-- Выберите комнату --</option>' +
                rooms.map(r => `<option value="${r.id}">${r.room_number}</option>`).join('');
            roomSelectEl.disabled = false;

        } catch (e) {
            toast('Ошибка загрузки комнат', 'error');
        }
    },

    // Обработчик выбора комнаты -> Показывает площадь, вместимость и счетчики
    handleRoomChange(roomIdStr, dormName, domContext) {
        const infoBox = domContext.newRoomInfo || domContext.roomInfo;

        if (!roomIdStr || !dormName || !this.roomsCache[dormName]) {
            if (infoBox) infoBox.style.display = 'none';
            return;
        }

        const roomId = parseInt(roomIdStr);
        const room = this.roomsCache[dormName].find(r => r.id === roomId);

        if (room && infoBox) {
            infoBox.style.display = 'block';

            // Используем textContent для span'ов и b-тегов
            if (domContext.infoArea) domContext.infoArea.textContent = Number(room.apartment_area).toFixed(1);
            if (domContext.infoCap) domContext.infoCap.textContent = room.total_room_residents;
            if (domContext.infoHw) domContext.infoHw.textContent = room.hw_meter_serial || '-';
            if (domContext.infoCw) domContext.infoCw.textContent = room.cw_meter_serial || '-';
            if (domContext.infoEl) domContext.infoEl.textContent = room.el_meter_serial || '-';
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
                        el('button', {
                            class: 'btn-icon',
                            title: 'Разовое начисление / Выселение',
                            style: { marginRight: '5px', background: '#fef3c7', color: '#d97706', borderColor: '#fde68a' },
                            onclick: () => this.openOneTimeModal(user)
                        }, '⏳'),
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
        event.preventDefault(); // Защита от перезагрузки страницы при отправке формы
        const button = this.dom.addForm.querySelector('button');
        const tariffIdVal = this.dom.newTariffSelect ? this.dom.newTariffSelect.value : null;
        const roomIdVal = this.dom.newRoomSelect ? this.dom.newRoomSelect.value : null;

        const data = {
            username: document.getElementById('newUsername').value.trim(),
            password: document.getElementById('newPassword').value,
            role: document.getElementById('newRole').value,
            tariff_id: tariffIdVal ? parseInt(tariffIdVal) : null,
            room_id: roomIdVal ? parseInt(roomIdVal) : null, // Сохраняем только ID комнаты
            residents_count: parseInt(document.getElementById('residentsCount').value) || 1,
            workplace: document.getElementById('workplace').value.trim()
        };

        setLoading(button, true, 'Создание...');

        try {
            await api.post('/users', data);
            toast('Пользователь успешно создан', 'success');
            this.dom.addForm.reset();

            // Сбрасываем каскадные селекты
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

    async handleImport(button) {
        const file = this.dom.importInput.files[0];

        if (!file) {
            toast('Выберите файл Excel', 'info');
            return;
        }

        if (!file.name.match(/\.(xlsx|xls)$/)) {
            toast('Разрешены только файлы Excel (.xlsx, .xls)', 'error');
            return;
        }

        const formData = new FormData();
        formData.append('file', file);

        setLoading(button, true, 'Загрузка...');

        try {
            const result = await api.post('/users/import_excel', formData);
            this.showImportResultModal(result);
            this.dom.importInput.value = '';
            this.table.refresh();
        } catch (error) {
            toast(error.message, 'error');
        } finally {
            setLoading(button, false);
        }
    },

    showImportResultModal(result) {
        const hasErrors = result.errors && result.errors.length > 0;

        const overlay = el('div', { class: 'modal-overlay open', style: { zIndex: 10000 } });
        const headerTitle = hasErrors ? '⚠️ Результат импорта (Есть ошибки)' : '✅ Импорт успешно завершен';
        const headerColor = hasErrors ? '#d97706' : '#059669';

        const closeBtn = el('button', { class: 'close-icon' }, '×');
        closeBtn.onclick = () => document.body.removeChild(overlay);

        const content = el('div', { class: 'modal-form' },
            el('ul', { style: { marginBottom: '15px', paddingLeft: '20px', fontSize: '15px', color: '#374151' } },
                el('li', { style: { marginBottom: '5px' } }, `Добавлено новых жильцов: `, el('strong', { style: { color: '#059669'} }, String(result.added_users))),
                el('li', {}, `Обновлено существующих: `, el('strong', { style: { color: '#2563eb'} }, String(result.updated_users))),
                el('li', { style: { marginTop: '5px' } }, `Добавлено комнат: `, el('strong', { style: { color: '#059669'} }, String(result.added_rooms))),
                el('li', {}, `Обновлено комнат: `, el('strong', { style: { color: '#2563eb'} }, String(result.updated_rooms)))
            )
        );

        if (hasErrors) {
            const errorBox = el('div', {
                style: {
                    maxHeight: '250px', overflowY: 'auto', background: '#fef2f2',
                    border: '1px solid #fecaca', borderRadius: '8px', padding: '12px',
                    fontSize: '13px', color: '#991b1b', fontFamily: 'monospace'
                }
            });

            result.errors.forEach(err => {
                errorBox.appendChild(el('div', {
                    style: { marginBottom: '6px', borderBottom: '1px dashed #fca5a5', paddingBottom: '6px' }
                }, String(err)));
            });

            content.appendChild(el('h4', { style: { marginBottom: '10px', color: '#dc2626', fontSize: '14px' } }, `Ошибки (${result.errors.length}):`));
            content.appendChild(errorBox);
        }

        const btnOk = el('button', { class: 'action-btn primary-btn full-width', style: { marginTop: '20px' } }, 'Понятно, закрыть');
        btnOk.onclick = () => document.body.removeChild(overlay);
        content.appendChild(btnOk);

        const modalWindow = el('div', { class: 'modal-window', style: { width: '550px' } },
            el('div', { class: 'modal-header' },
                el('h3', { style: { color: headerColor } }, headerTitle),
                closeBtn
            ),
            content
        );

        overlay.appendChild(modalWindow);
        document.body.appendChild(overlay);
    },

    async deleteUser(id) {
        if (!confirm('Вы действительно хотите удалить этого пользователя? (Будет произведено мягкое удаление. История показаний сохранится за комнатой.)')) return;

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

            // Восстанавливаем каскадные селекты для редактирования
            if (user.room) {
                if (inputs.dormSelect) {
                    inputs.dormSelect.value = user.room.dormitory_name;
                    // Ждем загрузки комнат
                    await this.handleDormChange(user.room.dormitory_name, inputs.roomSelect, inputs.roomInfo);

                    if (inputs.roomSelect) {
                        inputs.roomSelect.value = user.room.id;
                        this.handleRoomChange(user.room.id, user.room.dormitory_name, inputs);
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
            toast('Ошибка загрузки данных пользователя: ' + error.message, 'error');
        }
    },

    closeModal() {
        this.modal.window.classList.remove('open');
    },

    async handleEditSubmit(event) {
        event.preventDefault(); // Защита от перезагрузки
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
            workplace: this.modal.inputs.work.value.trim()
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
    },

    async openOneTimeModal(user) {
        if (!this.otc.modal) return;

        const address = user.room ? `${user.room.dormitory_name} ком. ${user.room.room_number}` : 'без адреса';

        this.otc.userId.value = user.id;
        this.otc.userName.textContent = `${user.username} (${address})`;

        const date = new Date();
        const totalDays = new Date(date.getFullYear(), date.getMonth() + 1, 0).getDate();
        this.otc.totalDays.value = totalDays;
        this.otc.daysLived.value = date.getDate();

        this.otc.hot.value = '';
        this.otc.cold.value = '';
        this.otc.elect.value = '';
        this.otc.isMovingOut.checked = false;

        this.otc.hot.placeholder = `Загрузка...`;
        this.otc.cold.placeholder = `Загрузка...`;
        this.otc.elect.placeholder = `Загрузка...`;

        this.otc.modal.classList.add('open');

        try {
            const state = await api.get(`/admin/readings/manual-state/${user.id}`);
            this.otc.hot.placeholder = `Пред: ${state.prev_hot}`;
            this.otc.cold.placeholder = `Пред: ${state.prev_cold}`;
            this.otc.elect.placeholder = `Пред: ${state.prev_elect}`;
        } catch (e) {
            console.warn(e);
            this.otc.hot.placeholder = `Ошибка`;
            this.otc.cold.placeholder = `Ошибка`;
            this.otc.elect.placeholder = `Ошибка`;
        }
    },

    async handleOneTimeSubmit(e) {
        e.preventDefault();

        const payload = {
            user_id: parseInt(this.otc.userId.value),
            total_days_in_month: parseInt(this.otc.totalDays.value),
            days_lived: parseInt(this.otc.daysLived.value),
            hot_water: parseFloat(this.otc.hot.value),
            cold_water: parseFloat(this.otc.cold.value),
            electricity: parseFloat(this.otc.elect.value),
            is_moving_out: this.otc.isMovingOut.checked
        };

        if (payload.is_moving_out) {
            if (!confirm('ВНИМАНИЕ! Вы выбрали выселение. Пользователь будет помечен как удаленный, комната будет освобождена. Продолжить?')) return;
        }

        setLoading(this.otc.btnSubmit, true, 'Расчет...');
        try {
            await api.post('/admin/readings/one-time', payload);
            toast('Разовое начисление успешно проведено и утверждено!', 'success');
            this.otc.modal.classList.remove('open');
            this.table.refresh();
        } catch (err) {
            toast(err.message, 'error');
        } finally {
            setLoading(this.otc.btnSubmit, false, 'Сформировать квитанцию');
        }
    }
};