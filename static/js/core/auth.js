// static/js/core/auth.js

export const Auth = {
    // Получить токен
    getToken() {
        return localStorage.getItem('token');
    },

    // Сохранить токен (используется при логине)
    setToken(token) {
        localStorage.setItem('token', token);
    },

    // Выход из системы
    logout() {
        console.log('Logging out...');
        localStorage.removeItem('token');
        // Редирект на логин
        window.location.href = 'login.html';
    },

    // Проверка, вошел ли пользователь
    isAuthenticated() {
        const token = this.getToken();
        if (!token) return false;

        // Базовая проверка структуры JWT (три части, разделенные точкой)
        // Это не гарантирует валидность подписи, но отсекает явный мусор
        const parts = token.split('.');
        if (parts.length !== 3) {
            this.logout(); // Токен битый - удаляем и выходим
            return false;
        }

        return true;
    }
};