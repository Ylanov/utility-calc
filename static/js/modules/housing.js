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
            btnRefresh: document.getElementById('btnRefreshRooms'),

            // Элементы анализатора
            btnAnalyze: document.getElementById('btnAnalyzeHousing'),
            analyzerModal: document.getElementById('analyzerModal'),
            analyzerResults: document.getElementById('analyzerResults'),
            btnAnalyzerClose: document.querySelectorAll('#analyzerModal .close-btn')
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

        // Элементы модалки замены счетчика
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
        }

        [this.modal.btnClose, this.modal.btnCancel].forEach(btn => {
            if (btn) btn.addEventListener('click', () => {
                this.modal.window.classList.remove('open');
            });
        });

        // События Анализатора
        if (this.dom.btnAnalyze) {
            this.dom.btnAnalyze.addEventListener('click', () => this.runAnalysis());
        }
        if (this.dom.btnAnalyzerClose) {
            this.dom.btnAnalyzerClose.forEach(btn => btn.addEventListener('click', () => {
                this.dom.analyzerModal.classList.remove('open');
            }));
        }

        // События замены счетчика
        if (this.meter.form) {
            this.meter.form.addEventListener('submit', (e) => this.handleMeterSubmit(e));
            this.meter.btnClose.addEventListener('click', (e) => { e.preventDefault(); this.meter.modal.classList.remove('open'); });
            this.meter.btnCancel.addEventListener('click', (e) => { e.preventDefault(); this.meter.modal.classList.remove('open'); });
        }
    },

    // ==========================================
    // ЛОГИКА АНАЛИЗАТОРА ЖИЛФОНДА
    // ==========================================
    async runAnalysis() {
        this.dom.analyzerModal.classList.add('open');
        this.dom.analyzerResults.innerHTML = `
            <div style="text-align:center; padding: 40px; color:#666;">
                <div class="spinner" style="border-color: #f59e0b; border-top-color: transparent; width: 30px; height: 30px; margin: 0 auto 15px auto;"></div>
                Сканируем базу данных... ⏳
            </div>`;

        try {
            const data = await api.get('/rooms/analyze');
            this.renderAnalysis(data);
        } catch(e) {
            this.dom.analyzerResults.innerHTML = `<div style="color:red; text-align:center; padding: 20px;">Ошибка получения данных: ${e.message}</div>`;
        }
    },

    renderAnalysis(data) {
        let html = '';

        // Конфигурация блоков аномалий
        const sections =[
            { key: 'unattached_users', icon: '👻', title: 'Жильцы без комнаты (Ошибки привязки)', color: '#dc2626', bg: '#fef2f2' },
            { key: 'shared_billing', icon: '👥', title: 'Совместное проживание (Раздельные Л/С в одной комнате)', color: '#3b82f6', bg: '#eff6ff' },
            { key: 'overcrowded', icon: '⚠️', title: 'Перенаселение (Платят за большее кол-во человек, чем есть мест)', color: '#ea580c', bg: '#fff7ed' },
            { key: 'zero_area', icon: '📏', title: 'Нулевая площадь (Ошибка заполнения)', color: '#b45309', bg: '#fef3c7' },
            { key: 'underpopulated', icon: '🛏️', title: 'Свободные места (Платят за меньшее кол-во человек, чем мест)', color: '#10b981', bg: '#ecfdf5' },
            { key: 'empty_rooms', icon: '🚪', title: 'Пустые комнаты (Никто не прописан)', color: '#6b7280', bg: '#f3f4f6' }
        ];

        let totalIssues = 0;

        sections.forEach(sec => {
            const items = data[sec.key];
            if (items && items.length > 0) {
                totalIssues += items.length;
                html += `
                    <div style="margin-bottom: 20px; border: 1px solid ${sec.color}40; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 4px rgba(0,0,0,0.02);">
                        <div style="background: ${sec.bg}; padding: 12px 15px; border-bottom: 1px solid ${sec.color}40; font-weight: bold; color: ${sec.color}; display: flex; align-items: center; gap: 10px;">
                            <span style="font-size: 20px;">${sec.icon}</span> ${sec.title} (${items.length})
                        </div>
                        <ul style="list-style: none; padding: 0; margin: 0; background: white; max-height: 250px; overflow-y: auto;">
                            ${items.map(item => `
                                <li style="padding: 12px 15px; border-bottom: 1px solid #f3f4f6; font-size: 13px;">
                                    <strong style="color: #1f2937; font-size: 14px;">${item.title}</strong>
                                    <div style="color: #6b7280; margin-top: 4px; line-height: 1.4;">${item.desc}</div>
                                </li>
                            `).join('')}
                        </ul>
                    </div>
                `;
            }
        });

        if (totalIssues === 0) {
            html = `
                <div style="text-align:center; padding: 60px 20px; background: white; border-radius: 8px; border: 1px solid #e5e7eb;">
                    <div style="font-size: 40px; margin-bottom: 15px;">✅</div>
                    <div style="color:#10b981; font-size: 18px; font-weight: bold;">Аномалий не обнаружено!</div>
                    <div style="color:#6b7280; font-size: 14px; margin-top: 5px;">Жилфонд и пользователи в идеальном состоянии.</div>
                </div>`;
        }

        this.dom.analyzerResults.innerHTML = html;
    },

    // ==========================================
    // СТАНДАРТНАЯ ЛОГИКА ЖИЛФОНДА
    // ==========================================
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
                return el('tr', { class: 'hover:bg-gray-50 transition-colors' },
                    el('td', { class: 'text-gray-500 text-sm' }, `#${room.id}`),
                    el('td', { style: { fontWeight: 'bold', color: '#1f2937' } }, room.dormitory_name),
                    el('td', { style: { fontWeight: 'bold', color: '#374151' } }, room.room_number),
                    el('td', {}, `${Number(room.apartment_area).toFixed(1)} м²`),
                    el('td', { class: 'text-center' }, room.total_room_residents),
                    el('td', { class: 'text-sm font-mono', style: {color: '#dc2626'} }, room.hw_meter_serial || '-'),
                    el('td', { class: 'text-sm font-mono', style: {color: '#2563eb'} }, room.cw_meter_serial || '-'),
                    el('td', { class: 'text-sm font-mono', style: {color: '#d97706'} }, room.el_meter_serial || '-'),
                    el('td', { class: 'text-center' },
                        // НОВАЯ КНОПКА ЗАМЕНЫ СЧЕТЧИКА
                        el('button', {
                            class: 'btn-icon', title: 'Замена счетчика', style: { marginRight: '5px', background: '#f0fdf4', color: '#166534', borderColor: '#bbf7d0' },
                            onclick: () => this.openMeterModal(room)
                        }, '🔄'),
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
    },

    // ==========================================
    // ЛОГИКА ЗАМЕНЫ СЧЕТЧИКА
    // ==========================================
    openMeterModal(room) {
        if (!this.meter.modal) return;
        this.meter.form.reset();
        this.meter.roomId.value = room.id;
        this.meter.roomName.textContent = `${room.dormitory_name}, ком. ${room.room_number}`;
        this.meter.initialNew.value = "0"; // Обычно новый счетчик начинается с нуля
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
    }
};