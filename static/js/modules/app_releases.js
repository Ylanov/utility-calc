// static/js/modules/app_releases.js
//
// Управление APK-релизами мобильного приложения через админку.
//
// UI: загрузка нового APK + таблица всех загруженных версий с возможностью
// опубликовать/снять с публикации, поменять release notes, удалить.

import { api } from '../core/api.js';
import { toast, escapeHtml } from '../core/dom.js';

function fmtSize(bytes) {
    if (!bytes) return '—';
    const mb = bytes / 1024 / 1024;
    if (mb >= 1) return `${mb.toFixed(1)} МБ`;
    const kb = bytes / 1024;
    return `${kb.toFixed(0)} КБ`;
}

function fmtDate(iso) {
    if (!iso) return '—';
    try {
        const d = new Date(iso);
        return d.toLocaleString('ru-RU', {
            day: '2-digit', month: '2-digit', year: 'numeric',
            hour: '2-digit', minute: '2-digit',
        });
    } catch { return iso; }
}

export const AppReleasesModule = {
    isInitialized: false,

    init() {
        if (this.isInitialized) {
            this.loadReleases();
            return;
        }
        this.cacheDOM();
        if (!this.dom.tableBody) return;
        this.bindEvents();
        this.isInitialized = true;
        this.loadReleases();
    },

    cacheDOM() {
        this.dom = {
            form: document.getElementById('appReleaseForm'),
            // version и versionCode удалены из формы — определяются автоматически
            // на сервере из APK. Оставляем поля как null-safe (querySelector вернёт
            // null, а formData.append просто пропустит — этого достаточно).
            version: null,
            versionCode: null,
            minRequired: document.getElementById('releaseMinRequired'),
            notes: document.getElementById('releaseNotes'),
            file: document.getElementById('releaseFile'),
            published: document.getElementById('releasePublished'),
            btnUpload: document.getElementById('btnReleaseUpload'),
            uploadProgress: document.getElementById('releaseUploadProgress'),
            uploadText: document.getElementById('releaseUploadProgressText'),
            tableBody: document.getElementById('appReleasesTableBody'),
            btnRefresh: document.getElementById('btnReleasesRefresh'),
        };
    },

    bindEvents() {
        this.dom.form?.addEventListener('submit', (e) => this.handleUpload(e));
        this.dom.btnRefresh?.addEventListener('click', () => this.loadReleases());

        // Делегирование действий в таблице
        this.dom.tableBody?.addEventListener('click', (e) => {
            const btn = e.target.closest('button[data-action]');
            if (!btn) return;
            const id = Number(btn.dataset.id);
            const action = btn.dataset.action;
            if (action === 'toggle-publish') this.togglePublish(id, btn.dataset.next === 'true');
            else if (action === 'delete') this.deleteRelease(id);
            else if (action === 'edit-notes') this.editNotes(id);
        });

        // Раньше тут был авто-подсказка version_code из version. Удалено —
        // теперь сервер сам читает обе величины из APK при загрузке.
    },

    async loadReleases() {
        try {
            const releases = await api.get('/admin/app/releases');
            this.renderTable(releases || []);
        } catch (e) {
            this.dom.tableBody.innerHTML = `
                <tr><td colspan="8" style="text-align:center; padding:20px; color:var(--danger-color);">
                    Ошибка: ${escapeHtml(e.message)}
                </td></tr>
            `;
        }
    },

    renderTable(releases) {
        if (!releases.length) {
            this.dom.tableBody.innerHTML = `
                <tr><td colspan="8" style="text-align:center; padding:30px; color:var(--text-secondary);">
                    Версий ещё не загружено. Загрузите первый APK через форму выше.
                </td></tr>
            `;
            return;
        }

        this.dom.tableBody.innerHTML = releases.map(r => {
            const statusBadge = r.is_published
                ? '<span style="padding:2px 8px; border-radius:10px; background:#d1fae5; color:#065f46; font-size:11px; font-weight:600;">Опубликовано</span>'
                : '<span style="padding:2px 8px; border-radius:10px; background:#fef3c7; color:#92400e; font-size:11px; font-weight:600;">Скрыто</span>';

            const notesShort = (r.release_notes || '').length > 60
                ? escapeHtml(r.release_notes.slice(0, 60)) + '…'
                : escapeHtml(r.release_notes || '—');

            return `
                <tr>
                    <td><b>${escapeHtml(r.version)}</b> <span style="font-size:11px; color:var(--text-secondary);">${escapeHtml(r.platform)}</span></td>
                    <td style="font-family:monospace;">${r.version_code}</td>
                    <td style="font-family:monospace; color:${r.min_required_version_code ? '#d97706' : 'var(--text-secondary)'};">
                        ${r.min_required_version_code || '—'}
                    </td>
                    <td>${fmtSize(r.file_size)}</td>
                    <td style="font-size:12px;">${escapeHtml(fmtDate(r.created_at))}</td>
                    <td>${statusBadge}</td>
                    <td style="max-width:240px; font-size:12px; color:var(--text-secondary);" title="${escapeHtml(r.release_notes || '')}">
                        ${notesShort}
                    </td>
                    <td style="text-align:right; white-space:nowrap;">
                        <a href="/api/app/download/${escapeHtml(r.file_name)}" download class="action-btn secondary-btn" style="padding:3px 8px; font-size:11px;" title="Скачать">
                            <i class="fa-solid fa-download"></i>
                        </a>
                        <button class="action-btn" data-action="edit-notes" data-id="${r.id}" style="padding:3px 8px; font-size:11px; background:#6366f1; color:#fff;" title="Изменить notes">
                            <i class="fa-solid fa-pen"></i>
                        </button>
                        <button class="action-btn ${r.is_published ? 'secondary-btn' : 'success-btn'}" data-action="toggle-publish" data-id="${r.id}" data-next="${!r.is_published}" style="padding:3px 8px; font-size:11px;" title="${r.is_published ? 'Снять с публикации' : 'Опубликовать'}">
                            <i class="fa-solid fa-${r.is_published ? 'eye-slash' : 'eye'}"></i>
                        </button>
                        <button class="action-btn danger-btn" data-action="delete" data-id="${r.id}" style="padding:3px 8px; font-size:11px;" title="Удалить">
                            <i class="fa-solid fa-trash"></i>
                        </button>
                    </td>
                </tr>
            `;
        }).join('');
    },

    async handleUpload(e) {
        e.preventDefault();
        const file = this.dom.file.files[0];
        if (!file) return toast('Выберите APK-файл', 'error');

        const formData = new FormData();
        // version и version_code НЕ отправляем — сервер прочитает их из APK.
        // Это устраняет рассинхронизацию «в форме 3.5.5, а в APK 1.2.0».
        formData.append('platform', 'android');
        if (this.dom.minRequired.value) {
            formData.append('min_required_version_code', this.dom.minRequired.value);
        }
        if (this.dom.notes.value.trim()) {
            formData.append('release_notes', this.dom.notes.value.trim());
        }
        formData.append('is_published', this.dom.published.checked ? 'true' : 'false');
        formData.append('file', file);

        this.dom.btnUpload.disabled = true;
        this.dom.uploadProgress.style.display = 'block';
        this.dom.uploadText.textContent = `Загрузка ${(file.size / 1024 / 1024).toFixed(1)} МБ...`;

        try {
            // Прямой fetch (не api.post — он сериализует JSON, а нам multipart)
            const token = sessionStorage.getItem('access_token');
            const res = await fetch('/api/admin/app/releases', {
                method: 'POST',
                headers: token ? { 'Authorization': `Bearer ${token}` } : {},
                body: formData,
                credentials: 'include',
            });

            if (!res.ok) {
                const err = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
                throw new Error(err.detail || `Ошибка ${res.status}`);
            }

            const data = await res.json();
            toast(`Версия ${data.version} загружена (${fmtSize(data.file_size)})`, 'success');
            this.dom.form.reset();
            this.loadReleases();
        } catch (err) {
            toast('Ошибка загрузки: ' + err.message, 'error');
        } finally {
            this.dom.btnUpload.disabled = false;
            this.dom.uploadProgress.style.display = 'none';
        }
    },

    async togglePublish(id, next) {
        try {
            await api._request(`/admin/app/releases/${id}`, {
                method: 'PATCH',
                body: { is_published: next },
            });
            toast(next ? 'Версия опубликована' : 'Версия снята с публикации', 'success');
            this.loadReleases();
        } catch (e) {
            toast('Ошибка: ' + e.message, 'error');
        }
    },

    async editNotes(id) {
        const newNotes = prompt('Новые release notes (можно multi-line — \\n для переноса):');
        if (newNotes === null) return;
        try {
            await api._request(`/admin/app/releases/${id}`, {
                method: 'PATCH',
                body: { release_notes: newNotes.replace(/\\n/g, '\n') },
            });
            toast('Release notes обновлены', 'success');
            this.loadReleases();
        } catch (e) {
            toast('Ошибка: ' + e.message, 'error');
        }
    },

    async deleteRelease(id) {
        if (!confirm('Удалить эту версию? Файл APK тоже будет удалён с сервера.')) return;
        try {
            await api.delete(`/admin/app/releases/${id}`);
            toast('Версия удалена', 'success');
            this.loadReleases();
        } catch (e) {
            toast('Ошибка: ' + e.message, 'error');
        }
    },
};
