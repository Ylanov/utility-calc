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
        // Берём токен текущей вкладки из sessionStorage
        const token = Auth.getToken();

        const headers = {
            'Accept': 'application/json',
            // Токен передаётся в заголовке Authorization — каждая вкладка имеет свой токен
            ...(token ? { 'Authorization': `Bearer ${token}` } : {}),
            ...options.headers
        };

        // Автоматически сериализуем тело запроса в JSON
        if (options.body && !(options.body instanceof FormData)) {
            headers['Content-Type'] = 'application/json';
            options.body = JSON.stringify(options.body);
        }

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
                // silentNotFound: для опциональных запросов (например /app/latest когда
                // ещё не загружен ни один APK). 404 — не ошибка, а ожидаемый случай.
                // Возвращаем null без шума в консоли вместо бросания exception.
                if (options.silentNotFound && response.status === 404) {
                    return null;
                }

                let errorMessage = `Ошибка ${response.status}`;
                let errData = data;

                if (typeof data === 'object') {
                    // FastAPI отдаёт ошибки в поле detail. Два варианта:
                    //   - detail: строка → берём её как текст ошибки
                    //   - detail: объект  → разворачиваем его в err.data,
                    //     чтобы модули могли читать err.data.conflict и т.п.
                    //     errorMessage берём из detail.message, либо JSON.stringify.
                    if (typeof data.detail === 'string') {
                        errorMessage = data.detail;
                    } else if (data.detail && typeof data.detail === 'object') {
                        errorMessage = data.detail.message || JSON.stringify(data.detail);
                        errData = data.detail;
                    }
                } else if (typeof data === 'string') {
                    // Защита от огромных HTML-ошибок серверов (Nginx/FastAPI)
                    if (data.trim().startsWith('<!DOCTYPE') || data.trim().startsWith('<html')) {
                        errorMessage = 'Внутренняя ошибка сервера (500). Обратитесь к администратору.';
                    } else {
                        errorMessage = data;
                    }
                }

                // Прикрепляем к Error http-статус и тело ответа, чтобы модули могли
                // делать context-aware обработку (например, конфликт-модалка на 409).
                const err = new Error(errorMessage);
                err.status = response.status;
                err.data = errData;
                throw err;
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
    get(endpoint, options = {}) { return this._request(endpoint, { ...options, method: 'GET' }); }
    post(endpoint, body) { return this._request(endpoint, { method: 'POST', body }); }
    put(endpoint, body) { return this._request(endpoint, { method: 'PUT', body }); }
    patch(endpoint, body) { return this._request(endpoint, { method: 'PATCH', body }); }
    delete(endpoint) { return this._request(endpoint, { method: 'DELETE' }); }

    /**
     * GET с TTL-кешем в sessionStorage. Используется для редко меняющихся
     * данных: тарифы, релизы, settings — они не меняются десятки раз за сессию,
     * нет смысла дёргать сервер при каждом ремонте модуля.
     *
     * Семантика:
     *   - Кеш-ключ: `api:cache:${endpoint}` (привязан к окну/вкладке).
     *   - Кеш чистится при logout (Auth.logout удаляет sessionStorage).
     *   - При ошибке сети/парсинга — fallback на свежий fetch (без кеша).
     *   - options.bypassCache = true → принудительно идём в сеть.
     *
     * @param {string} endpoint
     * @param {{ttlSeconds?: number, bypassCache?: boolean} & object} options
     */
    async getCached(endpoint, options = {}) {
        const ttl = (options.ttlSeconds ?? 300) * 1000;
        const key = `api:cache:${endpoint}`;
        if (!options.bypassCache) {
            try {
                const raw = sessionStorage.getItem(key);
                if (raw) {
                    const cached = JSON.parse(raw);
                    if (cached && (Date.now() - cached.t) < ttl) {
                        return cached.v;
                    }
                }
            } catch { /* битый кеш — просто идём в сеть */ }
        }
        const data = await this.get(endpoint, options);
        try {
            sessionStorage.setItem(key, JSON.stringify({ t: Date.now(), v: data }));
        } catch { /* sessionStorage полон/выключен — это нормально */ }
        return data;
    }

    /** Сброс кеша по точному endpoint или по префиксу (для invalidation после mutate). */
    invalidateCache(endpointOrPrefix) {
        const prefix = `api:cache:${endpointOrPrefix}`;
        try {
            const keys = [];
            for (let i = 0; i < sessionStorage.length; i++) {
                const k = sessionStorage.key(i);
                if (k && k.startsWith(prefix)) keys.push(k);
            }
            keys.forEach(k => sessionStorage.removeItem(k));
        } catch { /* sessionStorage недоступен — silent */ }
    }

    /**
     * Метод для скачивания файлов (Blob).
     *
     * Отличается от _request тем, что на 401 только сообщает пользователю
     * и кидает ошибку — НЕ вызывает Auth.logout().
     * Раньше именно здесь начинался редирект на portal.html при попытке
     * скачать PDF с протухшим токеном: пользователь терял открытую админку.
     * Теперь логика такая: дать пользователю увидеть подсказку и выбрать,
     * перелогиниваться или обновить страницу.
     */
    async download(endpoint, defaultFilename = 'file') {
        const token = Auth.getToken();

        const response = await fetch(`${this.baseUrl}${endpoint}`, {
            method: 'GET',
            headers: token ? { 'Authorization': `Bearer ${token}` } : {},
            credentials: 'include'
        });

        if (response.status === 401) {
            toast('Сессия истекла. Обновите страницу и войдите заново.', 'error');
            throw new Error('Unauthorized');
        }

        if (response.status === 403) {
            throw new Error('Нет прав на скачивание этого файла');
        }

        if (!response.ok) {
            // Пытаемся прочитать ошибку из JSON, если сервер вернул JSON вместо файла
            const contentType = response.headers.get("content-type");
            if (contentType && contentType.includes("application/json")) {
                const errData = await response.json();
                throw new Error(errData.detail || `Ошибка сервера: ${response.status}`);
            }
            const errText = await response.text();
            throw new Error(errText || `Ошибка сервера: ${response.status}`);
        }

        // Проверка, что сервер реально вернул файл, а не JSON/HTML
        // (иначе пользователь получит "скачивание" HTML под именем .pdf).
        const contentType = response.headers.get('content-type') || '';
        if (contentType.includes('application/json') ||
            contentType.startsWith('text/html')) {
            throw new Error('Сервер вернул не файл, а страницу. Попробуйте ещё раз или обновите страницу.');
        }

        // Пытаемся достать имя файла из заголовков Content-Disposition
        let filename = defaultFilename;
        const disposition = response.headers.get('content-disposition');
        if (disposition && disposition.includes('filename=')) {
            const filenameRegex = /filename[^;=\n]*=((['"]).*?\2|[^;\n]*)/;
            const matches = filenameRegex.exec(disposition);
            if (matches != null && matches[1]) {
                filename = matches[1].replace(/['"]/g, '');
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

        setTimeout(() => {
            document.body.removeChild(a);
            window.URL.revokeObjectURL(url);
        }, 100);
    }
}

export const api = new ApiClient();