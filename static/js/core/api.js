// static/js/core/api.js
import { Auth } from './auth.js';
import { toast } from './dom.js';

class ApiClient {
    /**
     * @param {string} baseUrl - Базовый URL (по умолчанию /api, так как основные роуты там)
     */
    constructor(baseUrl = '/api') {
        this.baseUrl = baseUrl;
    }

    async _request(endpoint, options = {}) {
        const headers = {
            'Accept': 'application/json',
            ...options.headers
        };

        // Автоматически сериализуем тело запроса в JSON
        if (options.body && !(options.body instanceof FormData)) {
            headers['Content-Type'] = 'application/json';
            options.body = JSON.stringify(options.body);
        }

        // ВАЖНО: 'same-origin' указывает браузеру автоматически прикреплять
        // нашу HttpOnly куку (access_token) к запросу
        const config = {
            ...options,
            headers,
            credentials: 'include' // ПОПРОБУЙТЕ 'include' ВМЕСТО 'same-origin'
        };

        try {
            // Формируем полный URL
            const response = await fetch(`${this.baseUrl}${endpoint}`, config);

            // 1. Обработка протухшего токена или отсутствия куки (401 Unauthorized)
            if (response.status === 401) {
                // Если мы не на странице логина, делаем логаут
                if (!window.location.pathname.includes('login.html')) {
                    toast('Сессия истекла. Пожалуйста, войдите снова.', 'error');
                    Auth.logout();
                }
                throw new Error('Unauthorized');
            }

            // 2. Разбор ответа
            const contentType = response.headers.get("content-type");
            let data;

            if (contentType && contentType.includes("application/json")) {
                data = await response.json();
            } else {
                data = await response.text();
            }

            // 3. Обработка ошибок приложения (400, 403, 404, 500)
            if (!response.ok) {
                let errorMessage = 'Ошибка сервера';

                if (typeof data === 'object') {
                    // FastAPI возвращает ошибки в поле detail
                    if (data.detail) {
                        errorMessage = typeof data.detail === 'string'
                            ? data.detail
                            : JSON.stringify(data.detail); // Если detail это массив ошибок валидации
                    }
                } else if (typeof data === 'string') {
                    // Если сервер вернул HTML-ошибку, не выводим её всю
                    if (data.trim().startsWith('<!DOCTYPE') || data.trim().startsWith('<html')) {
                        errorMessage = 'Внутренняя ошибка сервера (500). Обратитесь к администратору.';
                    } else {
                        errorMessage = data;
                    }
                }

                throw new Error(errorMessage);
            }

            return data;

        } catch (error) {
            // Игнорируем ошибку отмены запроса, она штатная
            if (error.name === 'AbortError') {
                throw error; // Пробрасываем дальше, чтобы TableController его обработал, но не логируем
            }

            // Не логируем ошибку авторизации, так как она уже обработана
            if (error.message !== 'Unauthorized') {
                console.error(`API Error [${endpoint}]:`, error);
            }
            throw error;
        }
    }

    // Методы-обертки
    get(endpoint, options) { return this._request(endpoint, { ...options, method: 'GET' }); }
    post(endpoint, body) { return this._request(endpoint, { method: 'POST', body }); }
    put(endpoint, body) { return this._request(endpoint, { method: 'PUT', body }); }
    delete(endpoint) { return this._request(endpoint, { method: 'DELETE' }); }

    /**
     * Метод для скачивания файлов (Blob).
     */
    async download(endpoint, defaultFilename = 'file') {
        try {
            // При скачивании файлов также обязательно указываем credentials
            const response = await fetch(`${this.baseUrl}${endpoint}`, {
                method: 'GET',
                credentials: 'same-origin'
            });

            if (response.status === 401) {
                Auth.logout();
                return;
            }

            if (!response.ok) {
                const errText = await response.text();
                throw new Error(errText || 'Ошибка скачивания');
            }

            // Пытаемся достать имя файла из заголовков
            let filename = defaultFilename;
            const disposition = response.headers.get('content-disposition');
            if (disposition && disposition.includes('filename=')) {
                const match = disposition.match(/filename=['"]?([^'"]+)['"]?/);
                if (match) filename = match;
            }

            // Создаем ссылку для скачивания
            const blob = await response.blob();
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = filename;
            document.body.appendChild(a);
            a.click();
            a.remove();
            window.URL.revokeObjectURL(url);

        } catch (error) {
            toast('Не удалось скачать файл: ' + error.message, 'error');
        }
    }
}

// Экспортируем единственный экземпляр
export const api = new ApiClient();