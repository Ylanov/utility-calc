// static/js/modules/client-app-download.js
//
// Карточка «Скачать приложение» в личном кабинете жильца.
//
// Запрашивает /api/app/latest при инициализации. Если есть опубликованная
// версия — показывает карточку: версию, release notes, кнопку скачать APK,
// QR-код со ссылкой (чтобы можно было отсканировать с телефона).
//
// Если опубликованных версий нет — карточка скрыта, ничего не делаем.

import { api } from '../core/api.js';

/**
 * Простейший QR-код через публичный сервис quickchart.io.
 * Не требует JS-библиотеки и тяжёлых зависимостей.
 *
 * Альтернативно можно подключить qrcode.js — но для статичной ссылки
 * это overkill: QR один на всю карточку, генерится раз при загрузке.
 */
function buildQrUrl(text, size = 160) {
    const encoded = encodeURIComponent(text);
    return `https://quickchart.io/qr?text=${encoded}&size=${size}&margin=1`;
}

function fmtSize(bytes) {
    if (!bytes) return '';
    const mb = bytes / 1024 / 1024;
    return mb >= 1 ? `${mb.toFixed(1)} МБ` : `${(bytes / 1024).toFixed(0)} КБ`;
}

export const ClientAppDownload = {
    async init() {
        const card = document.getElementById('appDownloadCard');
        if (!card) return;

        let info;
        try {
            info = await api.get('/app/latest?platform=android');
        } catch (e) {
            // 404 = нет опубликованных версий, это нормально для свежей установки
            console.log('App release not available:', e.message);
            return;
        }
        if (!info || !info.download_url) return;

        // Полный URL — нужен для QR (телефон должен открыть его напрямую)
        const fullUrl = `${window.location.origin}${info.download_url}`;

        const versionEl = document.getElementById('appDownloadVersion');
        if (versionEl) {
            versionEl.innerHTML =
                `Версия: <b>${info.version}</b>` +
                (info.file_size ? ` · ${fmtSize(info.file_size)}` : '');
        }

        const notesEl = document.getElementById('appDownloadNotes');
        if (notesEl && info.release_notes) {
            notesEl.textContent = info.release_notes;
            notesEl.style.display = 'block';
        }

        const btn = document.getElementById('appDownloadBtn');
        if (btn) {
            btn.href = info.download_url;
            // download-атрибут хочет имя — берём из URL
            const fileName = info.download_url.split('/').pop();
            btn.setAttribute('download', fileName);
        }

        const qrContainer = document.getElementById('appDownloadQR');
        if (qrContainer) {
            const img = document.createElement('img');
            img.src = buildQrUrl(fullUrl, 160);
            img.alt = 'QR-код для скачивания APK';
            img.style.width = '100%';
            img.style.height = '100%';
            img.loading = 'lazy';
            qrContainer.innerHTML = '';
            qrContainer.appendChild(img);
        }

        card.style.display = 'block';
    },
};
