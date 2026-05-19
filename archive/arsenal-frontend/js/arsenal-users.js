/**
 * МОДУЛЬ: Управление пользователями Арсенала.
 *
 * API:
 *   GET    /api/arsenal/users                 — список с фильтрами
 *   POST   /api/arsenal/users                 — создание (пароль опц.)
 *   GET    /api/arsenal/users/{id}            — карточка + статистика
 *   PATCH  /api/arsenal/users/{id}            — редактирование
 *   POST   /api/arsenal/users/{id}/deactivate
 *   POST   /api/arsenal/users/{id}/activate
 *   POST   /api/arsenal/users/{id}/unlock
 *   DELETE /api/arsenal/users/{id}
 *   POST   /api/arsenal/users/{id}/reset-password-link  — из ops.py
 *   GET    /api/arsenal/objects                — список складов для селектора
 */
const API = '/api/arsenal';

const userModal   = document.getElementById('userModal');
const detailModal = document.getElementById('detailModal');

async function api(path, opts = {}) {
    const res = await apiFetch(API + path, opts);
    if (!res) return null;
    if (res.status === 204) return {};
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `Ошибка ${res.status}`);
    return data;
}

function fmtDate(iso) {
    if (!iso) return '—';
    try {
        return new Date(iso).toLocaleString('ru-RU', {
            day: '2-digit', month: '2-digit', year: '2-digit',
            hour: '2-digit', minute: '2-digit',
        });
    } catch { return iso; }
}

function escapeHtml(s) {
    if (s == null) return '';
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

let allObjects = [];

// =====================================================================
// ЗАГРУЗКА
// =====================================================================
async function loadObjects() {
    try {
        const data = await api('/objects');
        allObjects = data || [];
        const fillSelect = (id, withDash = true) => {
            const sel = document.getElementById(id);
            if (!sel) return;
            sel.innerHTML = (withDash ? '<option value="">— не привязан —</option>' : '<option value="">Все склады</option>')
                + allObjects.map(o => `<option value="${o.id}">${escapeHtml(o.name)}</option>`).join('');
        };
        fillSelect('f_object', true);
        fillSelect('fltObject', false);
    } catch (e) {
        UI.showToast('Не удалось загрузить объекты: ' + e.message, 'error');
    }
}

async function loadUsers() {
    const params = new URLSearchParams();
    const q = document.getElementById('fltQ').value.trim();
    const role = document.getElementById('fltRole').value;
    const active = document.getElementById('fltActive').value;
    const obj = document.getElementById('fltObject').value;
    if (q) params.set('q', q);
    if (role) params.set('role', role);
    if (active) params.set('is_active', active);
    if (obj) params.set('object_id', obj);

    try {
        const users = await api('/users?' + params);
        renderUsers(users || []);
        renderKPI(users || []);
    } catch (e) {
        UI.showToast('Ошибка загрузки: ' + e.message, 'error');
    }
}

function renderKPI(users) {
    const total = users.length;
    const active = users.filter(u => u.is_active).length;
    const locked = users.filter(u => u.is_locked).length;
    const admins = users.filter(u => u.role === 'admin').length;
    document.getElementById('kpiTotal').textContent = total;
    document.getElementById('kpiActive').textContent = active;
    document.getElementById('kpiInactive').textContent = total - active;
    document.getElementById('kpiLocked').textContent = locked;
    document.getElementById('kpiAdmins').textContent = admins;
}

function renderUsers(users) {
    const tbody = document.getElementById('usersBody');
    if (!users.length) {
        tbody.innerHTML = `<tr><td colspan="7" class="px-3 py-10 text-center text-slate-400">
            Нет пользователей по заданным фильтрам.</td></tr>`;
        return;
    }
    tbody.innerHTML = users.map(u => {
        const roleChip = u.role === 'admin'
            ? '<span class="chip chip-admin">Админ</span>'
            : '<span class="chip chip-head">Нач. склада</span>';
        let statusChip;
        if (u.is_locked) {
            statusChip = `<span class="chip chip-locked" title="Заблокирован после неверных паролей"><i class="fa-solid fa-lock"></i> Заблок.</span>`;
        } else if (u.is_active) {
            statusChip = '<span class="chip chip-active"><i class="fa-solid fa-circle-check"></i> Активен</span>';
        } else {
            statusChip = '<span class="chip chip-inactive"><i class="fa-solid fa-ban"></i> Отключён</span>';
        }
        const actions = [
            `<button class="text-blue-600 hover:text-blue-800 p-1" title="Подробности" onclick="openDetail(${u.id})"><i class="fa-solid fa-eye"></i></button>`,
            `<button class="text-slate-500 hover:text-slate-800 p-1" title="Редактировать" onclick="openEdit(${u.id})"><i class="fa-solid fa-pen"></i></button>`,
            `<button class="text-amber-600 hover:text-amber-800 p-1" title="Сбросить пароль (отправим ссылку)" onclick="resetLink(${u.id}, '${escapeHtml(u.username)}')"><i class="fa-solid fa-key"></i></button>`,
            u.is_locked ? `<button class="text-amber-600 hover:text-amber-800 p-1" title="Снять блокировку" onclick="unlockUser(${u.id})"><i class="fa-solid fa-lock-open"></i></button>` : '',
            u.is_active
                ? `<button class="text-rose-600 hover:text-rose-800 p-1" title="Отключить" onclick="deactivateUser(${u.id}, '${escapeHtml(u.username)}')"><i class="fa-solid fa-user-slash"></i></button>`
                : `<button class="text-emerald-600 hover:text-emerald-800 p-1" title="Активировать" onclick="activateUser(${u.id})"><i class="fa-solid fa-user-check"></i></button>`,
            (u.documents_count === 0)
                ? `<button class="text-rose-700 hover:text-rose-900 p-1" title="Удалить (нет документов)" onclick="deleteUser(${u.id}, '${escapeHtml(u.username)}')"><i class="fa-solid fa-trash"></i></button>`
                : '',
        ].join('');
        return `
            <tr class="border-b border-slate-100 hover:bg-slate-50 ${!u.is_active ? 'opacity-60' : ''}">
                <td class="px-3 py-3">
                    <div class="font-semibold">${escapeHtml(u.username)}</div>
                    ${u.full_name ? `<div class="text-xs text-slate-600">${escapeHtml(u.full_name)}</div>` : ''}
                    ${u.email ? `<div class="text-xs text-slate-400"><i class="fa-solid fa-envelope"></i> ${escapeHtml(u.email)}</div>` : ''}
                    ${u.phone ? `<div class="text-xs text-slate-400"><i class="fa-solid fa-phone"></i> ${escapeHtml(u.phone)}</div>` : ''}
                </td>
                <td class="px-3 py-3">${roleChip}</td>
                <td class="px-3 py-3 text-sm text-slate-700">${escapeHtml(u.object_name || '—')}</td>
                <td class="px-3 py-3 text-center">${statusChip}</td>
                <td class="px-3 py-3 text-center font-mono">${u.documents_count}</td>
                <td class="px-3 py-3 text-xs text-slate-500">
                    ${u.last_login_at ? fmtDate(u.last_login_at) : '<span class="text-slate-400">никогда</span>'}
                    ${u.last_login_ip ? `<div class="text-[11px] text-slate-400">${escapeHtml(u.last_login_ip)}</div>` : ''}
                </td>
                <td class="px-3 py-3 text-right whitespace-nowrap">${actions}</td>
            </tr>`;
    }).join('');
}

// =====================================================================
// CREATE / EDIT
// =====================================================================
function openCreate() {
    document.getElementById('modalTitle').textContent = 'Новый пользователь';
    document.getElementById('f_id').value = '';
    document.getElementById('f_username').value = '';
    document.getElementById('f_username').disabled = false;
    document.getElementById('f_full_name').value = '';
    document.getElementById('f_email').value = '';
    document.getElementById('f_phone').value = '';
    document.getElementById('f_role').value = 'unit_head';
    document.getElementById('f_object').value = '';
    document.getElementById('f_password').value = '';
    document.getElementById('createPasswordRow').style.display = 'block';
    userModal.style.display = 'flex';
}

async function openEdit(id) {
    try {
        const data = await api(`/users/${id}`);
        const u = data.user;
        document.getElementById('modalTitle').textContent = `Редактирование: ${u.username}`;
        document.getElementById('f_id').value = u.id;
        document.getElementById('f_username').value = u.username;
        document.getElementById('f_username').disabled = true;  // username не меняется
        document.getElementById('f_full_name').value = u.full_name || '';
        document.getElementById('f_email').value = u.email || '';
        document.getElementById('f_phone').value = u.phone || '';
        document.getElementById('f_role').value = u.role;
        document.getElementById('f_object').value = u.object_id || '';
        document.getElementById('f_password').value = '';
        // При редактировании пароль задаётся отдельной «сбросной» кнопкой — убираем из формы
        document.getElementById('createPasswordRow').style.display = 'none';
        userModal.style.display = 'flex';
    } catch (e) {
        UI.showToast(e.message, 'error');
    }
}

function closeModal() { userModal.style.display = 'none'; }
window.closeModal = closeModal;

document.getElementById('userForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const id = document.getElementById('f_id').value;
    const payload = {
        full_name: document.getElementById('f_full_name').value.trim() || null,
        email: document.getElementById('f_email').value.trim() || null,
        phone: document.getElementById('f_phone').value.trim() || null,
        role: document.getElementById('f_role').value,
        object_id: parseInt(document.getElementById('f_object').value) || null,
    };
    try {
        if (id) {
            await api(`/users/${id}`, { method: 'PATCH', body: JSON.stringify(payload) });
            UI.showToast('Сохранено', 'success');
        } else {
            const pwd = document.getElementById('f_password').value;
            const body = {
                username: document.getElementById('f_username').value.trim(),
                ...payload,
                password: pwd || null,
            };
            const res = await api('/users', { method: 'POST', body: JSON.stringify(body) });
            if (res.generated_password) {
                // Показываем сгенерированный пароль один раз — копируемо, без сохранения на сервере логов.
                showGeneratedPassword(res.username, res.generated_password);
            } else {
                UI.showToast(`Создан пользователь «${res.username}»`, 'success');
            }
        }
        closeModal();
        loadUsers();
    } catch (err) {
        UI.showToast(err.message, 'error');
    }
});

function showGeneratedPassword(username, password) {
    const html = `
        <div class="p-6">
            <h3 class="text-lg font-bold mb-2"><i class="fa-solid fa-circle-check text-emerald-600"></i> Пользователь создан</h3>
            <p class="text-sm text-slate-600 mb-4">Передайте пароль пользователю. После закрытия окна он больше не отобразится.</p>
            <div class="space-y-2">
                <div class="bg-slate-50 rounded p-3">
                    <div class="text-xs text-slate-500">Логин</div>
                    <code class="text-base font-mono">${escapeHtml(username)}</code>
                </div>
                <div class="bg-amber-50 border border-amber-200 rounded p-3">
                    <div class="text-xs text-amber-700">Временный пароль</div>
                    <code class="text-lg font-mono text-amber-900">${escapeHtml(password)}</code>
                    <button onclick="navigator.clipboard.writeText('${escapeHtml(password).replace(/'/g, "\\'")}'); UI.showToast('Скопировано', 'success');"
                            class="ml-3 text-xs bg-amber-600 text-white px-2 py-1 rounded hover:bg-amber-700">
                        <i class="fa-solid fa-copy"></i> Копировать
                    </button>
                </div>
            </div>
            <div class="text-right mt-5">
                <button onclick="detailModal.style.display='none'" class="bg-slate-700 text-white px-5 py-2 rounded text-sm">Закрыть</button>
            </div>
        </div>`;
    document.getElementById('detailBody').innerHTML = html;
    detailModal.style.display = 'flex';
}

// =====================================================================
// DETAIL
// =====================================================================
async function openDetail(id) {
    detailModal.style.display = 'flex';
    document.getElementById('detailBody').innerHTML = '<div class="text-center py-10"><i class="fa-solid fa-spinner fa-spin text-3xl text-slate-400"></i></div>';
    try {
        const data = await api(`/users/${id}`);
        renderDetail(data);
    } catch (e) {
        document.getElementById('detailBody').innerHTML = `<div class="p-6 text-rose-600">Ошибка: ${escapeHtml(e.message)}</div>`;
    }
}
window.openDetail = openDetail;

function renderDetail(data) {
    const u = data.user;
    const stats = data.stats;

    const byTypeHtml = Object.keys(stats.documents_by_type || {}).map(t =>
        `<li><b>${escapeHtml(t)}</b>: ${stats.documents_by_type[t]}</li>`
    ).join('') || '<li class="text-slate-400">нет документов</li>';

    const recentDocs = (data.recent_documents || []).map(d => `
        <tr class="border-b border-slate-100">
            <td class="py-1.5 font-mono text-xs">${escapeHtml(d.doc_number)}</td>
            <td class="py-1.5 text-xs">${escapeHtml(d.operation_type)}</td>
            <td class="py-1.5 text-xs text-slate-500">${fmtDate(d.created_at)}</td>
            <td class="py-1.5 text-xs">${d.is_reversed ? '<span class="chip chip-inactive">отменён</span>' : ''}</td>
        </tr>`).join('');

    const audit = (data.recent_audit || []).map(a => `
        <div class="text-xs border-b border-slate-100 py-1.5">
            <span class="font-mono text-slate-500">${fmtDate(a.created_at)}</span>
            <span class="inline-block px-2 py-0.5 rounded bg-slate-100 ml-1 mr-1">${escapeHtml(a.action)}</span>
            <span class="text-slate-600">${escapeHtml(a.entity_type)}${a.entity_id ? ` #${a.entity_id}` : ''}</span>
            ${a.ip_address ? `<span class="text-slate-400 ml-2">${escapeHtml(a.ip_address)}</span>` : ''}
        </div>`).join('');

    const html = `
        <div class="p-6">
            <div class="flex items-start justify-between mb-4">
                <div>
                    <h3 class="text-xl font-bold">${escapeHtml(u.username)}</h3>
                    ${u.full_name ? `<div class="text-sm text-slate-600">${escapeHtml(u.full_name)}</div>` : ''}
                </div>
                <button onclick="detailModal.style.display='none'" class="text-slate-400 hover:text-slate-600 text-2xl leading-none">&times;</button>
            </div>

            <!-- Профиль -->
            <div class="grid grid-cols-2 gap-3 text-sm mb-5">
                <div><span class="text-slate-500">Роль:</span> <b>${u.role === 'admin' ? 'Администратор' : 'Начальник склада'}</b></div>
                <div><span class="text-slate-500">Объект:</span> <b>${escapeHtml(u.object_name || '—')}</b></div>
                <div><span class="text-slate-500">Email:</span> ${escapeHtml(u.email || '—')}</div>
                <div><span class="text-slate-500">Телефон:</span> ${escapeHtml(u.phone || '—')}</div>
                <div><span class="text-slate-500">Создан:</span> ${fmtDate(u.created_at)}</div>
                <div><span class="text-slate-500">Последний вход:</span> ${fmtDate(u.last_login_at)} ${u.last_login_ip ? `<span class="text-slate-400">(${escapeHtml(u.last_login_ip)})</span>` : ''}</div>
                <div><span class="text-slate-500">Статус:</span>
                    ${u.is_active ? '<span class="chip chip-active">Активен</span>' : '<span class="chip chip-inactive">Отключён</span>'}
                    ${u.locked_until ? '<span class="chip chip-locked ml-1">Заблокирован</span>' : ''}
                </div>
                <div><span class="text-slate-500">Неудачных входов:</span> ${u.failed_login_count || 0}</div>
                ${u.deactivated_at ? `<div class="col-span-2 text-xs text-rose-700">
                    Отключён ${fmtDate(u.deactivated_at)}${u.deactivation_reason ? ': ' + escapeHtml(u.deactivation_reason) : ''}
                </div>` : ''}
            </div>

            <!-- Статистика документов -->
            <div class="grid grid-cols-2 gap-4 mb-5">
                <div class="bg-slate-50 rounded p-3">
                    <div class="text-xs text-slate-500 uppercase mb-1">Всего документов</div>
                    <div class="text-2xl font-bold">${stats.documents_total}</div>
                    <ul class="text-xs text-slate-600 mt-2">${byTypeHtml}</ul>
                </div>

                <!-- Действия -->
                <div class="bg-slate-50 rounded p-3 flex flex-col gap-2">
                    <div class="text-xs text-slate-500 uppercase mb-1">Действия</div>
                    <button onclick="openEdit(${u.id})" class="bg-white border border-slate-300 text-slate-700 px-3 py-1.5 rounded text-sm text-left hover:bg-slate-100">
                        <i class="fa-solid fa-pen"></i> Редактировать
                    </button>
                    <button onclick="resetLink(${u.id}, '${escapeHtml(u.username)}')" class="bg-white border border-amber-300 text-amber-700 px-3 py-1.5 rounded text-sm text-left hover:bg-amber-50">
                        <i class="fa-solid fa-key"></i> Сбросить пароль (ссылка)
                    </button>
                    ${u.locked_until ? `<button onclick="unlockUser(${u.id})" class="bg-white border border-amber-300 text-amber-700 px-3 py-1.5 rounded text-sm text-left hover:bg-amber-50">
                        <i class="fa-solid fa-lock-open"></i> Снять блокировку
                    </button>` : ''}
                    ${u.is_active
                        ? `<button onclick="deactivateUser(${u.id}, '${escapeHtml(u.username)}')" class="bg-white border border-rose-300 text-rose-700 px-3 py-1.5 rounded text-sm text-left hover:bg-rose-50">
                               <i class="fa-solid fa-user-slash"></i> Отключить
                           </button>`
                        : `<button onclick="activateUser(${u.id})" class="bg-white border border-emerald-300 text-emerald-700 px-3 py-1.5 rounded text-sm text-left hover:bg-emerald-50">
                               <i class="fa-solid fa-user-check"></i> Активировать
                           </button>`}
                </div>
            </div>

            <!-- Последние документы -->
            <h4 class="font-semibold mb-2 text-slate-700">Последние документы</h4>
            <table class="w-full text-sm mb-5">
                <thead class="bg-slate-50 text-xs text-slate-500 uppercase">
                    <tr><th class="py-1.5 text-left">№</th><th class="py-1.5 text-left">Тип</th><th class="py-1.5 text-left">Когда</th><th class="py-1.5"></th></tr>
                </thead>
                <tbody>${recentDocs || '<tr><td colspan="4" class="py-4 text-center text-slate-400">Документов нет</td></tr>'}</tbody>
            </table>

            <!-- Активность -->
            <h4 class="font-semibold mb-2 text-slate-700">Последние действия</h4>
            <div class="bg-slate-50 rounded p-3 max-h-60 overflow-y-auto">
                ${audit || '<div class="text-slate-400 text-xs">Записей нет</div>'}
            </div>
        </div>`;
    document.getElementById('detailBody').innerHTML = html;
}

// =====================================================================
// ACTIONS (reset link / activate / deactivate / unlock / delete)
// =====================================================================
async function resetLink(id, username) {
    if (!confirm(`Создать одноразовую ссылку сброса пароля для «${username}»?\n\nПароль будет устанавливать сам пользователь, в JSON он не попадёт.`)) return;
    try {
        const res = await api(`/users/${id}/reset-password-link`, { method: 'POST' });
        const fullUrl = location.origin + res.reset_url;
        const html = `
            <div class="p-6">
                <h3 class="text-lg font-bold mb-2"><i class="fa-solid fa-key text-amber-600"></i> Ссылка сброса создана</h3>
                <p class="text-sm text-slate-600 mb-3">Передайте ссылку пользователю <b>${escapeHtml(res.username)}</b>. Действует до ${fmtDate(res.expires_at)}.</p>
                <div class="bg-slate-50 rounded p-3 font-mono text-xs break-all">${escapeHtml(fullUrl)}</div>
                <div class="flex justify-end gap-2 mt-4">
                    <button onclick="navigator.clipboard.writeText('${fullUrl.replace(/'/g, "\\'")}'); UI.showToast('Скопировано', 'success');"
                            class="bg-amber-600 text-white px-4 py-2 rounded text-sm">
                        <i class="fa-solid fa-copy"></i> Скопировать
                    </button>
                    <button onclick="detailModal.style.display='none'" class="bg-slate-700 text-white px-4 py-2 rounded text-sm">Закрыть</button>
                </div>
            </div>`;
        document.getElementById('detailBody').innerHTML = html;
        detailModal.style.display = 'flex';
    } catch (e) { UI.showToast(e.message, 'error'); }
}
window.resetLink = resetLink;

async function deactivateUser(id, username) {
    const reason = prompt(`Отключить пользователя «${username}»?\n\nПричина (опционально):`);
    if (reason === null) return;
    try {
        await api(`/users/${id}/deactivate`, { method: 'POST', body: JSON.stringify({ reason: reason || null }) });
        UI.showToast('Отключено', 'success');
        detailModal.style.display = 'none';
        loadUsers();
    } catch (e) { UI.showToast(e.message, 'error'); }
}
window.deactivateUser = deactivateUser;

async function activateUser(id) {
    try {
        await api(`/users/${id}/activate`, { method: 'POST' });
        UI.showToast('Активировано', 'success');
        detailModal.style.display = 'none';
        loadUsers();
    } catch (e) { UI.showToast(e.message, 'error'); }
}
window.activateUser = activateUser;

async function unlockUser(id) {
    try {
        await api(`/users/${id}/unlock`, { method: 'POST' });
        UI.showToast('Блокировка снята', 'success');
        detailModal.style.display = 'none';
        loadUsers();
    } catch (e) { UI.showToast(e.message, 'error'); }
}
window.unlockUser = unlockUser;

async function deleteUser(id, username) {
    if (!confirm(`УДАЛИТЬ «${username}» безвозвратно?\n\nРазрешено только при отсутствии проведённых документов. Если есть — используйте «Отключить».`)) return;
    try {
        await api(`/users/${id}`, { method: 'DELETE' });
        UI.showToast('Удалено', 'success');
        loadUsers();
    } catch (e) { UI.showToast(e.message, 'error'); }
}
window.deleteUser = deleteUser;

// =====================================================================
// INIT
// =====================================================================
document.getElementById('btnCreate').addEventListener('click', openCreate);

// Фильтры с debounce
let fltDebounce = null;
['fltQ', 'fltRole', 'fltActive', 'fltObject'].forEach(id => {
    document.getElementById(id).addEventListener('input', () => {
        clearTimeout(fltDebounce);
        fltDebounce = setTimeout(loadUsers, 250);
    });
});

// Закрытие модалок по клику вне окна
[userModal, detailModal].forEach(m => {
    m.addEventListener('click', (e) => { if (e.target === m) m.style.display = 'none'; });
});

loadObjects().then(loadUsers);
