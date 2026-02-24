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

        // Если кнопки нет на странице, прекращаем выполнение, чтобы не было ошибок
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

        // Закрытие по клику вне окна (для админа и финансиста)
        if (modal) {
            modal.addEventListener('click', (e) => {
                if (e.target === modal) this.closeModal();
            });
        }
    },

    async openModal() {
        const modal = document.getElementById('totpModal');
        const img = document.getElementById('totpQrImage');
        const secretText = document.getElementById('totpSecretText');
        const btn = document.getElementById('btnOpenTotp');

        setLoading(btn, true, '...');
        try {
            // Запрашиваем QR и секрет у бэкенда
            const data = await api.post('/auth/setup-2fa', {});

            this.currentSecret = data.secret;
            if (img) {
                img.src = `data:image/png;base64,${data.qr_code}`;
                img.style.display = 'block';
                // Убираем класс hidden для Tailwind (жилец)
                img.classList.remove('hidden');
            }
            if (secretText) secretText.textContent = data.secret;

            if (modal) {
                modal.classList.add('open');
                // Для Tailwind (жилец) нужно убрать hidden
                modal.classList.remove('hidden');
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
            // Возвращаем классы для Tailwind
            modal.classList.add('hidden');
            modal.style.display = ''; // Сброс инлайн стиля
        }

        const codeInput = document.getElementById('totpCodeInput');
        if (codeInput) codeInput.value = '';

        this.currentSecret = null;
    },

    async activate() {
        const codeInput = document.getElementById('totpCodeInput');
        const code = codeInput ? codeInput.value.trim() : '';
        const btn = document.getElementById('btnTotpActivate');

        if (!code || code.length !== 6 || isNaN(code)) {
            toast('Введите корректный 6-значный код', 'error');
            return;
        }

        setLoading(btn, true, 'Проверка...');
        try {
            await api.post('/auth/activate-2fa', {
                code: code,
                secret: this.currentSecret
            });

            toast('Двухфакторная защита успешно включена!', 'success');
            this.closeModal();

            // Скрываем кнопку настройки (опционально)
            // const btnOpen = document.getElementById('btnOpenTotp');
            // if (btnOpen) btnOpen.style.display = 'none';

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