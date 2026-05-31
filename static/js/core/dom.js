// static/js/core/dom.js

/**
 * Экранирование строк для безопасной вставки в innerHTML / template-literal.
 *
 * Используется ВСЕГДА когда имя жильца, адрес, ФИО, название тарифа,
 * любая строка из API подставляется в HTML через template literal.
 * Без этого название общежития "<img src=x onerror=alert(1)>" даст XSS.
 *
 * Лучше предпочесть `el(...)` — он автоматически безопасен через textContent.
 * escapeHtml оставлен как fallback для мест, где переписать на el() слишком дорого.
 */
export function escapeHtml(value) {
    if (value === null || value === undefined) return '';
    return String(value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

/**
 * Создает HTML элемент безопасным способом (защита от XSS).
 */
export function el(tag, attributes = {}, ...children) {
    const element = document.createElement(tag);

    for (const [key, value] of Object.entries(attributes)) {
        if (key.startsWith('on') && typeof value === 'function') {
            element.addEventListener(key.substring(2).toLowerCase(), value);
        } else if (key === 'style' && typeof value === 'object') {
            Object.assign(element.style, value);
        } else if (key === 'dataset' && typeof value === 'object') {
            Object.assign(element.dataset, value);
        } else if (value !== null && value !== undefined && value !== false) {
            element.setAttribute(key, value);
        }
    }

    children.forEach(child => {
        if (child === null || child === undefined || child === false) return;

        if (typeof child === 'string' || typeof child === 'number') {
            // Создаем текстовый узел — предотвращает выполнение <script> (XSS)
            element.appendChild(document.createTextNode(child));
        } else if (child instanceof Node) {
            element.appendChild(child);
        } else if (Array.isArray(child)) {
            child.forEach(c => c && element.appendChild(c));
        }
    });

    return element;
}

/**
 * Очищает содержимое элемента по ID.
 */
export function clear(elementId) {
    const element = document.getElementById(elementId);
    if (element) element.innerHTML = '';
    return element;
}

/**
 * Управляет состоянием кнопки (Загрузка/Обычное).
 * Предотвращает множественные отправки форм (Double-click prevention).
 */
export function setLoading(btn, isLoading, loadingText = 'Загрузка...') {
    if (!btn) return;

    if (isLoading) {
        // Сохраняем оригинальные ширину и текст, чтобы кнопка не прыгала
        if (!btn.dataset.originalText) {
            btn.dataset.originalText = btn.innerText;
            // Фиксируем ширину (опционально, убрано для гибкости, но можно добавить btn.style.width = btn.offsetWidth + 'px')
        }

        btn.disabled = true;
        btn.classList.add('loading-state'); // Класс для CSS (потускнение)
        btn.innerText = loadingText;
    } else {
        btn.disabled = false;
        btn.classList.remove('loading-state');
        btn.innerText = btn.dataset.originalText || 'OK';

        // Удаляем сохраненный текст для будущих вызовов
        delete btn.dataset.originalText;
    }
}

/**
 * Показывает красивое всплывающее уведомление (Toast).
 */
export function toast(message, type = 'success') {
    let container = document.getElementById('toast-container');
    if (!container) {
        container = el('div', { id: 'toast-container' });
        document.body.appendChild(container);
    }

    const colors = {
        success: '#10b981', // Изумрудный (Tailwind green-500)
        error: '#ef4444',   // Красный (Tailwind red-500)
        info: '#3b82f6',    // Синий (Tailwind blue-500)
        warning: '#f59e0b'  // Желтый (Tailwind amber-500)
    };

    // Определяем иконку
    const icons = {
        success: '✅',
        error: '❌',
        info: 'ℹ️',
        warning: '⚠️'
    };

    const icon = icons[type] || icons.success;

    const toastEl = el('div', {
        class: 'toast show',
        style: {
            backgroundColor: colors[type] || colors.success,
            display: 'flex',
            alignItems: 'center',
            gap: '10px'
        }
    },
        el('span', { style: { fontSize: '18px' } }, icon),
        el('span', {}, message)
    );

    container.appendChild(toastEl);

    // Анимация удаления
    setTimeout(() => {
        toastEl.style.opacity = '0';
        toastEl.style.transform = 'translateX(100%)';
        setTimeout(() => toastEl.remove(), 300);
    }, 4000); // Держим 4 секунды (чуть дольше для длинных текстов)
}

/**
 * Асинхронное модальное окно с вводом данных (Замена window.prompt).
 * Возвращает Promise.
 */
export function showPrompt(title, message, defaultValue = '', placeholder = '') {
    return new Promise((resolve) => {
        const overlay = el('div', { class: 'modal-overlay open', style: { zIndex: 10000 } });

        const input = el('input', {
            type: 'text',
            value: defaultValue,
            placeholder: placeholder,
            style: { width: '100%', marginBottom: '20px', fontSize: '16px' }
        });

        const btnConfirm = el('button', { class: 'action-btn primary-btn' }, 'OK');
        const btnCancel = el('button', { class: 'action-btn secondary-btn' }, 'Отмена');

        const modal = el('div', { class: 'modal-window', style: { width: '400px' } },
            el('div', { class: 'modal-header' }, el('h3', {}, title)),
            el('div', { class: 'modal-form' },
                el('p', { style: { marginBottom: '15px', color: '#4b5563', fontSize: '14px' } }, message),
                input
            ),
            el('div', { class: 'modal-footer' }, btnCancel, btnConfirm)
        );

        overlay.appendChild(modal);
        document.body.appendChild(overlay);

        // Фокус на поле ввода с небольшой задержкой для рендера
        setTimeout(() => input.focus(), 50);

        const close = (value) => {
            if (document.body.contains(overlay)) {
                document.body.removeChild(overlay);
            }
            resolve(value);
        };

        btnConfirm.onclick = () => close(input.value.trim());
        btnCancel.onclick = () => close(null);

        // Обработка клавиш
        input.onkeydown = (e) => {
            if (e.key === 'Enter') {
                e.preventDefault(); // Защита от сабмита форм на фоне
                close(input.value.trim());
            }
            if (e.key === 'Escape') {
                e.preventDefault();
                close(null);
            }
        };

        // Закрытие по клику вне модалки
        overlay.onmousedown = (e) => {
            if (e.target === overlay) close(null);
        };
    });
}

/**
 * Асинхронное подтверждение (красивая замена window.confirm).
 * Возвращает Promise<boolean>. message может быть многострочным (\n).
 */
export function showConfirm(message, opts = {}) {
    const {
        title = 'Подтверждение',
        confirmText = 'Подтвердить',
        cancelText = 'Отмена',
        danger = false,
    } = opts;
    return new Promise((resolve) => {
        const overlay = el('div', { class: 'modal-overlay open', style: { zIndex: 10000 } });
        const btnConfirm = el('button', {
            class: 'action-btn primary-btn',
            style: danger ? { background: '#dc2626', borderColor: '#dc2626' } : {},
        }, confirmText);
        const btnCancel = el('button', { class: 'action-btn secondary-btn' }, cancelText);

        const body = el('div', { class: 'modal-form' });
        String(message).split('\n').forEach(line => {
            body.appendChild(el('p', {
                style: { margin: '0 0 6px 0', color: '#374151', fontSize: '14px', whiteSpace: 'pre-wrap' },
            }, line));
        });

        const modal = el('div', { class: 'modal-window', style: { width: '440px' } },
            el('div', { class: 'modal-header' }, el('h3', {}, title)),
            body,
            el('div', { class: 'modal-footer' }, btnCancel, btnConfirm)
        );
        overlay.appendChild(modal);
        document.body.appendChild(overlay);
        setTimeout(() => btnConfirm.focus(), 50);

        const close = (v) => {
            if (document.body.contains(overlay)) document.body.removeChild(overlay);
            resolve(v);
        };
        btnConfirm.onclick = () => close(true);
        btnCancel.onclick = () => close(false);
        overlay.onmousedown = (e) => { if (e.target === overlay) close(false); };
        const onKey = (e) => {
            if (!document.body.contains(overlay)) { document.removeEventListener('keydown', onKey); return; }
            if (e.key === 'Escape') { e.preventDefault(); close(false); }
            else if (e.key === 'Enter') { e.preventDefault(); close(true); }
        };
        document.addEventListener('keydown', onKey);
    });
}

/**
 * Блокирующее уведомление с одной кнопкой OK (замена alert(), когда дальше
 * идёт действие вроде logout — toast не успел бы показаться). Promise<void>.
 */
export function showAlert(message, opts = {}) {
    const { title = 'Готово', okText = 'OK' } = opts;
    return new Promise((resolve) => {
        const overlay = el('div', { class: 'modal-overlay open', style: { zIndex: 10000 } });
        const btnOk = el('button', { class: 'action-btn primary-btn' }, okText);
        const body = el('div', { class: 'modal-form' });
        String(message).split('\n').forEach(line => {
            body.appendChild(el('p', {
                style: { margin: '0 0 6px 0', color: '#374151', fontSize: '14px', whiteSpace: 'pre-wrap' },
            }, line));
        });
        const modal = el('div', { class: 'modal-window', style: { width: '420px' } },
            el('div', { class: 'modal-header' }, el('h3', {}, title)),
            body,
            el('div', { class: 'modal-footer' }, btnOk)
        );
        overlay.appendChild(modal);
        document.body.appendChild(overlay);
        setTimeout(() => btnOk.focus(), 50);
        const close = () => {
            if (document.body.contains(overlay)) document.body.removeChild(overlay);
            resolve();
        };
        btnOk.onclick = close;
        const onKey = (e) => {
            if (!document.body.contains(overlay)) { document.removeEventListener('keydown', onKey); return; }
            if (e.key === 'Enter' || e.key === 'Escape') { e.preventDefault(); close(); }
        };
        document.addEventListener('keydown', onKey);
    });
}

/**
 * Многополевой диалог ввода (красивая замена нескольких prompt сразу).
 *   fields: [{ key, label, type='number'|'text', value, hint, min, step }]
 * Возвращает Promise<object|null> — { key: value, ... } (строки) или null при отмене.
 */
export function showDialog(opts = {}) {
    const {
        title = 'Ввод данных',
        message = '',
        fields = [],
        confirmText = 'Сохранить',
        cancelText = 'Отмена',
    } = opts;
    return new Promise((resolve) => {
        const overlay = el('div', { class: 'modal-overlay open', style: { zIndex: 10000 } });
        const inputs = {};

        const submit = () => {
            const result = {};
            for (const f of fields) result[f.key] = (inputs[f.key].value || '').trim();
            close(result);
        };
        const close = (v) => {
            if (document.body.contains(overlay)) document.body.removeChild(overlay);
            resolve(v);
        };

        const fieldNodes = fields.map(f => {
            const input = el('input', {
                type: f.type || 'text',
                value: (f.value !== undefined && f.value !== null) ? String(f.value) : '',
                style: { width: '100%', fontSize: '15px' },
            });
            if (f.min !== undefined) input.setAttribute('min', String(f.min));
            if (f.step !== undefined) input.setAttribute('step', String(f.step));
            input.onkeydown = (e) => {
                if (e.key === 'Enter') { e.preventDefault(); submit(); }
                if (e.key === 'Escape') { e.preventDefault(); close(null); }
            };
            inputs[f.key] = input;
            return el('div', { class: 'form-group', style: { marginBottom: '14px' } },
                el('label', { style: { display: 'block', fontSize: '13px', fontWeight: '600', marginBottom: '4px' } }, f.label || f.key),
                input,
                f.hint ? el('div', { style: { fontSize: '11px', color: '#6b7280', marginTop: '4px' } }, f.hint) : null
            );
        });

        const btnConfirm = el('button', { class: 'action-btn primary-btn' }, confirmText);
        const btnCancel = el('button', { class: 'action-btn secondary-btn' }, cancelText);

        const form = el('div', { class: 'modal-form' },
            message ? el('p', { style: { margin: '0 0 14px 0', color: '#374151', fontSize: '14px', whiteSpace: 'pre-wrap' } }, message) : null,
            ...fieldNodes
        );
        const modal = el('div', { class: 'modal-window', style: { width: '440px' } },
            el('div', { class: 'modal-header' }, el('h3', {}, title)),
            form,
            el('div', { class: 'modal-footer' }, btnCancel, btnConfirm)
        );
        overlay.appendChild(modal);
        document.body.appendChild(overlay);
        setTimeout(() => { const f0 = fieldNodes[0] && fieldNodes[0].querySelector('input'); if (f0) f0.focus(); }, 50);

        btnConfirm.onclick = submit;
        btnCancel.onclick = () => close(null);
        overlay.onmousedown = (e) => { if (e.target === overlay) close(null); };
    });
}