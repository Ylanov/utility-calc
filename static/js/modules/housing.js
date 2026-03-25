// static/js/modules/housing.js
import { api } from '../core/api.js';
import { el, toast, setLoading } from '../core/dom.js';
import { TableController } from '../core/table-controller.js';

export const HousingModule = {
    table: null,
    isInitialized: false,

    async init() {
        this.cacheDOM();

        if (!this.isInitialized) {
            this.bindEvents();
            this.isInitialized = true;
        }

        await this.loadDormitories();
        this.initTable();
    },

    cacheDOM() {
        this.dom = {
            dormFilterSelect: document.getElementById('dormFilterSelect'),
            dormList: document.getElementById('dormList'),
            btnOpenAdd: document.getElementById('btnOpenAddRoom'),
            btnRefresh: document.getElementById('btnRefreshRooms')
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
                hw: document.getElementById('roomHwSerial'),
                cw: document.getElementById('roomCwSerial'),
                el: document.getElementById('roomElSerial')
            }
        };
    },

    bindEvents() {
        if (this.dom.btnRefresh) this.dom.btnRefresh.addEventListener('click', () => this.table.refresh());

        if (this.dom.dormFilterSelect) {
            this.dom.dormFilterSelect.addEventListener('change', () => {
                this.table.state.page = 1;
                this.table.load();
            });
        }

        if (this.dom.btnOpenAdd) {
            this.dom.btnOpenAdd.addEventListener('click', () => this.openModal());
        }

        if (this.modal.form) {
            this.modal.form.addEventListener('submit', (e) => this.handleSave(e));
        }[this.modal.btnClose, this.modal.btnCancel].forEach(btn => {
            if (btn) btn.addEventListener('click', () => {
                this.modal.window.classList.remove('open');
            });
        });
    },

    async loadDormitories() {
        try {
            const dorms = await api.get('/rooms/dormitories');

            // Заполняем фильтр
            let filterHtml = '<option value="">Все объекты</option>';
            // Заполняем datalist для автодополнения в форме
            let datalistHtml = '';

            dorms.forEach(d => {
                filterHtml += `<option value="${d}">${d}</option>`;
                datalistHtml += `<option value="${d}">`;
            });

            this.dom.dormFilterSelect.innerHTML = filterHtml;
            this.dom.dormList.innerHTML = datalistHtml;

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
                return {
                    dormitory: this.dom.dormFilterSelect.value
                };
            },
            renderRow: (room) => {
                return el('tr', { class: 'hover:bg-gray-50' },
                    el('td', { class: 'text-gray-500 text-sm' }, `#${room.id}`),
                    el('td', { style: { fontWeight: 'bold' } }, room.dormitory_name),
                    el('td', { style: { fontWeight: 'bold' } }, room.room_number),
                    el('td', {}, `${Number(room.apartment_area).toFixed(1)} м²`),
                    el('td', { class: 'text-center' }, room.total_room_residents),
                    el('td', { class: 'text-sm font-mono', style: {color: '#dc2626'} }, room.hw_meter_serial || '-'),
                    el('td', { class: 'text-sm font-mono', style: {color: '#2563eb'} }, room.cw_meter_serial || '-'),
                    el('td', { class: 'text-sm font-mono', style: {color: '#d97706'} }, room.el_meter_serial || '-'),
                    el('td', { class: 'text-center' },
                        el('button', {
                            class: 'btn-icon btn-edit', title: 'Редактировать', style: { marginRight: '5px' },
                            onclick: () => this.openModal(room)
                        }, '✎'),
                        el('button', {
                            class: 'btn-icon btn-delete', title: 'Удалить',
                            onclick: () => this.deleteRoom(room.id)
                        }, '🗑')
                    )
                );
            }
        });

        this.table.init();
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
        } else {
            this.modal.title.textContent = 'Добавить помещение';
            this.modal.inputs.id.value = '';
            // Если выбран фильтр, подставляем его в создание
            if (this.dom.dormFilterSelect.value) {
                this.modal.inputs.dorm.value = this.dom.dormFilterSelect.value;
            }
        }

        this.modal.window.classList.add('open');
    },

    async handleSave(e) {
        e.preventDefault();
        const btn = this.modal.form.querySelector('.confirm-btn');
        const id = this.modal.inputs.id.value;

        const data = {
            dormitory_name: this.modal.inputs.dorm.value.trim(),
            room_number: this.modal.inputs.num.value.trim(),
            apartment_area: parseFloat(this.modal.inputs.area.value),
            total_room_residents: parseInt(this.modal.inputs.cap.value),
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
            // Обновляем список общежитий на случай, если добавилось новое
            this.loadDormitories();

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
        } catch (e) {
            toast(e.message, 'error');
        }
    }
};