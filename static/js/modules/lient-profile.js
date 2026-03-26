// static/js/modules/client-profile.js
import { api } from '../core/api.js';
import { toast, setLoading } from '../core/dom.js';
import { Auth } from '../core/auth.js';

export const ClientProfile = {
    init() {
        this.cacheDOM();
        this.bindEvents();
        this.loadProfile();
    },

    cacheDOM() {
        this.dom = {
            headerAddress: document.getElementById('headerAddress'),

            // Информация профиля
            user: document.getElementById('pUser'),
            address: document.getElementById('pAddress'),
            area: document.getElementById('pArea'),
            residents: document.getElementById('pResidents'),

            // Серийные номера счетчиков (над полями ввода)
            serials: {
                hot: document.getElementById('lblHwSerial'),
                cold: document.getElementById('lblCwSerial'),
                elect: document.getElementById('lblElSerial')
            },

            // Смена пароля
            cpForm: document.getElementById('changePasswordForm'),
            cpOld: document.getElementById('cpOld'),
            cpNew: document.getElementById('cpNew'),
            cpNewConfirm: document.getElementById('cpNewConfirm'),
            btnCp: document.getElementById('btnChangePassword'),

            // Первичная настройка
            fsModal: document.getElementById('firstSetupModal'),
            fsCurrentLogin: document.getElementById('fsCurrentLogin'),
            fsForm: document.getElementById('firstSetupForm'),
            fsNewLogin: document.getElementById('fsNewLogin'),
            fsNewPassword: document.getElementById('fsNewPassword'),
            btnFsSave: document.getElementById('btnFsSave'),
            btnFsShowForm: document.getElementById('btnFsShowForm'),
            btnFsSkip: document.getElementById('btnFsSkip'),
            fsActionButtons: document.getElementById('fsActionButtons')
        };
    },

    bindEvents() {
        if (this.dom.cpForm) {
            this.dom.cpForm.addEventListener('submit', (e) => this.handleChangePassword(e));
        }
        if (this.dom.btnFsSkip) {
            this.dom.btnFsSkip.addEventListener('click', () => this.skipFirstSetup());
        }
        if (this.dom.btnFsShowForm) {
            this.dom.btnFsShowForm.addEventListener('click', () => {
                this.dom.fsActionButtons.classList.add('hide');
                this.dom.fsForm.classList.remove('hide');
            });
        }
        if (this.dom.fsForm) {
            this.dom.fsForm.addEventListener('submit', (e) => this.saveFirstSetup(e));
        }
    },

    async loadProfile() {
        try {
            const user = await api.get('/users/me');

            // Формируем красивый адрес из объекта комнаты
            let addressDisplay = 'Адрес не указан';
            if (user.room) {
                addressDisplay = `${user.room.dormitory_name}, комната ${user.room.room_number}`;
            } else if (user.dormitory) {
                addressDisplay = user.dormitory; // fallback для старых данных
            }

            // Общая информация
            if (this.dom.user) this.dom.user.textContent = user.username;
            if (this.dom.address) this.dom.address.textContent = addressDisplay;
            if (this.dom.headerAddress) this.dom.headerAddress.textContent = addressDisplay;

            const area = user.room ? user.room.apartment_area : user.apartment_area;
            if (this.dom.area) this.dom.area.textContent = `${Number(area || 0).toFixed(1)} м²`;
            if (this.dom.residents) this.dom.residents.textContent = user.residents_count;

            // Серийные номера счетчиков
            if (user.room) {
                if (this.dom.serials.hot) this.dom.serials.hot.textContent = user.room.hw_meter_serial || 'Не указан';
                if (this.dom.serials.cold) this.dom.serials.cold.textContent = user.room.cw_meter_serial || 'Не указан';
                if (this.dom.serials.elect) this.dom.serials.elect.textContent = user.room.el_meter_serial || 'Не указан';
            }

            // Проверка первичной настройки
            if (user.is_initial_setup_done === false && this.dom.fsModal) {
                if (this.dom.fsCurrentLogin) this.dom.fsCurrentLogin.textContent = user.username;
                this.dom.fsModal.classList.add('open');
            }

        } catch (e) {
            console.warn('Ошибка загрузки профиля:', e);
        }
    },

    async skipFirstSetup() {
        setLoading(this.dom.btnFsSkip, true, 'Загрузка...');
        try {
            await api.post('/users/me/setup', {});
            this.dom.fsModal.classList.remove('open');
            toast('Настройка завершена!', 'success');
        } catch (error) {
            toast(error.message, 'error');
            setLoading(this.dom.btnFsSkip, false, 'Оставить как есть');
        }
    },

    async saveFirstSetup(e) {
        e.preventDefault();
        const newLogin = this.dom.fsNewLogin.value.trim();
        const newPassword = this.dom.fsNewPassword.value;

        setLoading(this.dom.btnFsSave, true, 'Сохранение...');
        try {
            const payload = {};
            if (newLogin) payload.new_username = newLogin;
            if (newPassword) payload.new_password = newPassword;

            await api.post('/users/me/setup', payload);

            alert('Ваши данные успешно обновлены. Пожалуйста, войдите в систему с новыми данными.');
            Auth.logout();

        } catch (error) {
            toast(error.message, 'error');
            setLoading(this.dom.btnFsSave, false, 'Сохранить новые данные');
        }
    },

    async handleChangePassword(e) {
        e.preventDefault();
        const oldPass = this.dom.cpOld.value;
        const newPass = this.dom.cpNew.value;
        const newPassConfirm = this.dom.cpNewConfirm.value;

        if (newPass !== newPassConfirm) {
            toast('Новые пароли не совпадают!', 'error');
            return;
        }

        setLoading(this.dom.btnCp, true, 'Обновление...');
        try {
            await api.post('/users/me/change-password', {
                old_password: oldPass,
                new_password: newPass
            });

            toast('Пароль успешно изменен!', 'success');
            this.dom.cpForm.reset();
        } catch (error) {
            toast(error.message, 'error');
        } finally {
            setLoading(this.dom.btnCp, false, 'Обновить пароль');
        }
    }
};