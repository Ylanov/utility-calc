// static/js/core/totp.js
import { api } from './api.js';
import { toast, setLoading } from './dom.js';

export const TotpSetup = {
    currentSecret: null,

    init() {
        const btnOpen = document.getElementById('btnOpenTotp');
        const modal = document.getElementById('totpModal');
        const btnClose = document.getElementById('btnTotpClose');
        const btnActivate = document.getElementById('btnTotpActivate');

        // Если кнопки нет на странице, прекращаем выполнение (например, на странице логина)
        if (!btnOpen) return;

        // Привязка событий
        btnOpen.addEventListener('click', (e) => {
            e.preventDefault();
            this.openModal();
        });

        if (btnClose) {
            btnClose.addEventListener('click', (e) => {
                e.preventDefault();
                this.closeModal();
            });
        }

        if (btnActivate) {
            btnActivate.addEventListener('click', (e) => {
                e.preventDefault();
                this.activate();
            });
        }

        // Закрытие по клику вне окна
        if (modal) {
            modal.addEventListener('mousedown', (e) => {
                if (e.target === modal) this.closeModal();
            });
        }
    },

    async openModal() {
        const modal = document.getElementById('totpModal');
        const img = document.getElementById('totpQrImage');
        const secretText = document.getElementById('totpSecretText');
        const btn = document.getElementById('btnOpenTotp');

        setLoading(btn, true, 'Генерация...');
        try {
            // Запрашиваем QR и секрет у бэкенда
            const data = await api.post('/auth/setup-2fa', {});

            this.currentSecret = data.secret;
            if (img) {
                img.src = `data:image/png;base64,${data.qr_code}`;
                img.style.display = 'block';
                img.classList.remove('hide', 'hidden'); // Поддержка разных CSS систем
            }
            if (secretText) {
                secretText.textContent = data.secret;
            }

            if (modal) {
                modal.classList.add('open');
                modal.classList.remove('hide', 'hidden');
                modal.style.display = 'flex';
            }
        } catch (e) {
            toast('Ошибка генерации 2FA: ' + e.message, 'error');
        } finally {
            setLoading(btn, false);
        }
    },

    closeModal() {
        const modal = document.getElementById('totpModal');
        if (modal) {
            modal.classList.remove('open');
            modal.style.display = '';
        }

        const codeInput = document.getElementById('totpCodeInput');
        if (codeInput) codeInput.value = '';

        this.currentSecret = null;
    },

    async activate() {
        const codeInput = document.getElementById('totpCodeInput');
        const code = codeInput ? codeInput.value.trim() : '';
        const btn = document.getElementById('btnTotpActivate');

        // Валидация на клиенте (только 6 цифр)
        if (!code || !/^\d{6}$/.test(code)) {
            toast('Введите корректный 6-значный код (только цифры)', 'error');
            return;
        }

        setLoading(btn, true, 'Проверка...');
        try {
            await api.post('/auth/activate-2fa', {
                code: code,
                secret: this.currentSecret
            });

            toast('Двухфакторная защита успешно включена! 🔐', 'success');
            this.closeModal();

            // Скрываем кнопку настройки, так как 2FA уже включена
            const btnOpen = document.getElementById('btnOpenTotp');
            if (btnOpen) {
                btnOpen.disabled = true;
                btnOpen.innerText = '2FA подключена';
                btnOpen.style.backgroundColor = '#10b981'; // Зеленый цвет
                btnOpen.style.borderColor = '#10b981';
            }

        } catch (e) {
            toast(e.message, 'error');
            if (codeInput) {
                codeInput.value = '';
                codeInput.focus();
            }
        } finally {
            setLoading(btn, false);
        }
    }
};