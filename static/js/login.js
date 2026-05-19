// static/js/login.js
import { Auth } from './core/auth.js';
import { toast, setLoading } from './core/dom.js';

document.addEventListener('DOMContentLoaded', () => {

    // --- ЭЛЕМЕНТЫ ЛОГИНА ---
    const loginForm = document.getElementById('loginForm');
    const usernameInput = document.getElementById('username');
    const passwordInput = document.getElementById('password');
    const btnLogin = document.getElementById('btnLogin');

    // --- ЭЛЕМЕНТЫ ИНФО-МОДАЛКИ «ЗАБЫЛИ ПАРОЛЬ?» ---
    // Форма с площадью помещения удалена (may 2026): теперь только
    // показываем info-сообщение, никаких обращений к серверу.
    const btnForgotPass = document.getElementById('btnForgotPass');
    const resetModal = document.getElementById('resetModal');
    const btnCloseIconReset = document.getElementById('btnCloseIconReset');
    const btnCloseSuccess = document.getElementById('btnCloseSuccess');

    // ==========================================
    // ЛОГИКА ВХОДА В СИСТЕМУ
    // ==========================================
    loginForm.addEventListener('submit', async (e) => {
        e.preventDefault();

        const username = usernameInput.value.trim();
        const password = passwordInput.value;

        if (!username || !password) {
            toast('Введите логин и пароль', 'warning');
            return;
        }

        setLoading(btnLogin, true, 'Вход...');

        try {
            // Формируем x-www-form-urlencoded данные для OAuth2
            const formData = new URLSearchParams();
            formData.append('username', username);
            formData.append('password', password);
            formData.append('grant_type', 'password');

            const response = await fetch('/api/token', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/x-www-form-urlencoded'
                },
                body: formData.toString()
            });

            if (!response.ok) {
                // Пытаемся вытащить сообщение об ошибке от FastAPI
                const errData = await response.json().catch(() => ({}));
                throw new Error(errData.detail || 'Неверный логин или пароль');
            }

            const data = await response.json();

            // Сохраняем сессию — токен хранится в sessionStorage этой вкладки,
            // что исключает смешивание сессий разных пользователей
            Auth.setSession(data.role, username, data.access_token);

            // Перенаправляем в зависимости от роли.
            // Только 2 роли (см. roles_001_simplify, май 2026): admin / user.
            // Старые accountant/financier были смержены в admin миграцией.
            if (data.role === 'admin') {
                window.location.replace('admin.html');
            } else {
                window.location.replace('index.html');
            }

        } catch (error) {
            toast(error.message, 'error');
            passwordInput.value = '';
            passwordInput.focus();
        } finally {
            setLoading(btnLogin, false, 'Войти');
        }
    });

    // ==========================================
    // «ЗАБЫЛИ ПАРОЛЬ?» — ФОРМА ОБРАЩЕНИЯ К АДМИНУ
    // ==========================================
    const forgotForm = document.getElementById('forgotPasswordForm');
    const forgotSuccess = document.getElementById('forgotSuccess');
    const btnSendForgot = document.getElementById('btnSendForgot');

    const closeResetModal = () => {
        resetModal.classList.remove('open');
        // Сбрасываем форму при закрытии — следующее открытие будет чистым.
        if (forgotForm) {
            forgotForm.reset();
            forgotForm.classList.remove('hide');
            forgotSuccess.classList.add('hide');
        }
    };

    btnForgotPass.addEventListener('click', (e) => {
        e.preventDefault();
        resetModal.classList.add('open');
    });

    btnCloseIconReset.addEventListener('click', closeResetModal);
    btnCloseSuccess.addEventListener('click', closeResetModal);

    resetModal.addEventListener('mousedown', (e) => {
        if (e.target === resetModal) closeResetModal();
    });

    // Отправка формы
    forgotForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const payload = {
            full_name:      document.getElementById('fpFullName').value.trim(),
            dormitory_name: document.getElementById('fpDormitoryName').value.trim(),
            room_number:    document.getElementById('fpRoomNumber').value.trim(),
            contact:        document.getElementById('fpContact').value.trim(),
            note:           document.getElementById('fpNote').value.trim() || null,
        };
        if (!payload.full_name || !payload.dormitory_name || !payload.room_number || !payload.contact) {
            toast('Заполните все обязательные поля', 'warning');
            return;
        }
        setLoading(btnSendForgot, true, 'Отправка...');
        try {
            const res = await fetch('/api/auth/forgot-password', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                throw new Error(err.detail || 'Не удалось отправить заявку');
            }
            // Показываем экран успеха.
            forgotForm.classList.add('hide');
            forgotSuccess.classList.remove('hide');
        } catch (err) {
            toast(err.message, 'error');
        } finally {
            setLoading(btnSendForgot, false, '<i class="fa-solid fa-paper-plane"></i> Отправить заявку');
        }
    });
});