// static/js/core/api.js
import { Auth } from './auth.js';
import { toast } from './dom.js';

class ApiClient {
    /**
     * @param {string} baseUrl - Базовый URL (по умолчанию /api)
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

        // ВАЖНО: 'include' указывает браузеру прикреплять HttpOnly куку
        const config = {
            ...options,
            headers,
            credentials: 'include'
        };

        try {
            const response = await fetch(`${this.baseUrl}${endpoint}`, config);

            // 1. Обработка протухшего токена (401)
            if (response.status === 401) {
                if (!window.location.pathname.includes('login.html')) {
                    toast('Сессия истекла или нет прав. Пожалуйста, войдите снова.', 'error');
                    Auth.logout();
                }
                throw new Error('Unauthorized');
            }

            // Обработка 403 (Запрещено)
            if (response.status === 403) {
                throw new Error('Недостаточно прав для выполнения этого действия');
            }

            // 2. Разбор ответа
            const contentType = response.headers.get("content-type");
            let data;

            // Если контента нет (например, 204 No Content), просто возвращаем null
            if (response.status === 204) {
                return null;
            }

            if (contentType && contentType.includes("application/json")) {
                data = await response.json();
            } else {
                data = await response.text();
            }

            // 3. Обработка ошибок сервера (400, 404, 500)
            if (!response.ok) {
                let errorMessage = `Ошибка ${response.status}`;

                if (typeof data === 'object') {
                    // FastAPI возвращает ошибки в поле detail
                    if (data.detail) {
                        errorMessage = typeof data.detail === 'string'
                            ? data.detail
                            : JSON.stringify(data.detail);
                    }
                } else if (typeof data === 'string') {
                    // Защита от огромных HTML-ошибок серверов (Nginx/FastAPI)
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
            // Игнорируем штатную отмену запроса
            if (error.name === 'AbortError') {
                throw error;
            }

            // Не логируем 401, так как он уже обработан
            if (error.message !== 'Unauthorized') {
                console.error(`[API Error] ${options.method || 'GET'} ${endpoint}:`, error.message);
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
            const response = await fetch(`${this.baseUrl}${endpoint}`, {
                method: 'GET',
                credentials: 'include'
            });

            if (response.status === 401) {
                toast('Сессия истекла. Авторизуйтесь заново.', 'error');
                Auth.logout();
                return;
            }

            if (!response.ok) {
                // Пытаемся прочитать ошибку из JSON, если сервер вернул JSON вместо файла
                const contentType = response.headers.get("content-type");
                if (contentType && contentType.includes("application/json")) {
                    const errData = await response.json();
                    throw new Error(errData.detail || 'Ошибка скачивания');
                }
                const errText = await response.text();
                throw new Error(errText || `Ошибка сервера: ${response.status}`);
            }

            // Пытаемся достать имя файла из заголовков Content-Disposition
            let filename = defaultFilename;
            const disposition = response.headers.get('content-disposition');
            if (disposition && disposition.includes('filename=')) {
                // Извлекаем имя файла (включая utf-8 кодировку, если есть)
                const filenameRegex = /filename[^;=\n]*=((['"]).*?\2|[^;\n]*)/;
                const matches = filenameRegex.exec(disposition);
                if (matches != null && matches[1]) {
                    filename = matches[1].replace(/['"]/g, '');
                    // Декодируем URL-encoded строку (напр. filename*=utf-8''otchet.xlsx)
                    if (filename.startsWith("UTF-8''") || filename.startsWith("utf-8''")) {
                        filename = decodeURIComponent(filename.substring(7));
                    }
                }
            }

            // Создаем ссылку для скачивания Blob в памяти
            const blob = await response.blob();
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.style.display = 'none';
            a.href = url;
            a.download = filename;

            document.body.appendChild(a);
            a.click();

            // Очищаем память
            setTimeout(() => {
                document.body.removeChild(a);
                window.URL.revokeObjectURL(url);
            }, 100);

        } catch (error) {
            toast('Не удалось скачать файл: ' + error.message, 'error');
        }
    }
}

export const api = new ApiClient();