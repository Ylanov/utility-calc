/**
 * СТРОБ.Арсенал - Клиентская логика
 */

let nomenclatures = [];
let objects = [];

async function apiFetch(url, options = {}) {
    const defaultHeaders = {'Content-Type': 'application/json'};
    options.headers = { ...defaultHeaders, ...options.headers };
    options.credentials = 'same-origin';
    try {
        const response = await fetch(url, options);
        if (response.status === 401) {
            window.location.href = 'arsenal_login.html';
            return null;
        }
        return response;
    } catch (error) {
        console.error("Сетевая ошибка:", error);
        return null;
    }
}

document.addEventListener('DOMContentLoaded', async () => {
    // Проверка прав (Ролевая модель)
    const userRole = localStorage.getItem('arsenal_role');
    if (userRole === 'unit_head') {
        const btnAddObj = document.getElementById('btnAddObject');
        const menuNom = document.getElementById('menuNomenclature');
        const menuUsers = document.getElementById('menuUsers'); // <-- ДОБАВЛЕНО

        if (btnAddObj) btnAddObj.style.display = 'none';
        if (menuNom) menuNom.style.display = 'none';
        if (menuUsers) menuUsers.style.display = 'none'; // <-- Скрываем меню пользователей от не-админов
    }
    // ==========================================

    injectSourceSelectIfNeeded();
    bindEvents();
    const dateInput = document.getElementById('newDocDate');
    if (dateInput) dateInput.valueAsDate = new Date();
    await Promise.all([loadNomenclature(), loadObjectsTree()]);
    updateFormState();
    loadDocuments();
});

function bindEvents() {
    // 1. Меню
    document.getElementById('menuDocs')?.addEventListener('click', loadDocuments);
    document.getElementById('menuObjects')?.addEventListener('click', loadObjectsTree);
    document.getElementById('menuNomenclature')?.addEventListener('click', openNomenclatureModal);

    // Новое меню "Пользователи" (если есть права)
    document.getElementById('menuUsers')?.addEventListener('click', loadAndShowUsers);

    // 2. Отчеты
    document.getElementById('menuReports')?.addEventListener('click', () => openModal('reportModal'));
    document.getElementById('btnReportSearch')?.addEventListener('click', searchForReport);
    document.getElementById('reportSearchInput')?.addEventListener('keyup', (e) => {
        if (e.key === 'Enter') searchForReport();
    });

    // 3. Открытие модалок создания
    document.getElementById('btnAddObject')?.addEventListener('click', () => openModal('newObjectModal'));
    document.getElementById('btnOpenCreateModal')?.addEventListener('click', openNewDocModal);

    // ============================================================
    // ИСПРАВЛЕНИЕ: ПРИВЯЗКА КНОПОК В МОДАЛКЕ СОЗДАНИЯ ДОКУМЕНТА
    // ============================================================
    // Кнопка "Крестик" (Закрыть)
    const btnClose = document.getElementById('btnCloseModal');
    if (btnClose) btnClose.addEventListener('click', () => closeModal('newDocModal'));

    // Кнопка "Отмена"
    const btnCancel = document.getElementById('btnCancelModal');
    if (btnCancel) btnCancel.addEventListener('click', () => closeModal('newDocModal'));

    // Кнопка "Провести" (Сохранить)
    const btnSave = document.getElementById('btnSaveDoc');
    if (btnSave) btnSave.addEventListener('click', createDocument);
    // ============================================================

    // 4. Закрытие остальных модалок (общий класс)
    document.querySelectorAll('.modal-close-btn').forEach(button => {
        button.addEventListener('click', () => closeModal(button.closest('.modal').id));
    });

    // Закрытие по клику на фон
    document.querySelectorAll('.modal').forEach(modal => {
        modal.addEventListener('click', event => {
            if (event.target === modal) closeModal(modal.id);
        });
    });

    // Закрытие по ESC
    document.addEventListener('keydown', event => {
        if (event.key === 'Escape') document.querySelectorAll('.modal').forEach(modal => closeModal(modal.id));
    });

    // 5. Логика формы документа
    document.getElementById('newDocType')?.addEventListener('change', updateFormState);
    document.getElementById('btnRefreshDocs')?.addEventListener('click', loadDocuments);
    document.getElementById('btnAddRow')?.addEventListener('click', addDocRow);

    // 6. Логика создания справочников
    document.getElementById('btnSaveObject')?.addEventListener('click', createObject);
    document.getElementById('btnSaveNom')?.addEventListener('click', createNomenclature);

    // 7. Выход
    document.getElementById('logoutBtn')?.addEventListener('click', async () => {
        try { await fetch('/api/arsenal/logout', { method: 'POST' }); } catch (e) {}
        window.location.href = 'arsenal_login.html';
    });
}

function injectSourceSelectIfNeeded() {
    if (document.getElementById('newDocSource')) return;
    const targetContainer = document.getElementById('targetSelectContainer');
    if (!targetContainer) return;
    const formGrid = targetContainer.parentElement;
    const sourceContainer = document.createElement('div');
    sourceContainer.id = 'sourceSelectContainer';
    sourceContainer.innerHTML = `
        <label for="newDocSource" class="block text-gray-700 font-bold mb-1">Отправитель / Источник</label>
        <select id="newDocSource" class="w-full border border-gray-300 p-2 rounded bg-white focus:border-blue-500 outline-none">
            <option value="">Загрузка...</option>
        </select>
    `;
    formGrid.insertBefore(sourceContainer, targetContainer);
}

function updateFormState() {
    const type = document.getElementById('newDocType').value;
    const sourceContainer = document.getElementById('sourceSelectContainer');
    const targetContainer = document.getElementById('targetSelectContainer');
    if (!sourceContainer || !targetContainer) return;
    sourceContainer.style.display = 'grid';
    targetContainer.style.display = 'grid';
    if (type === 'Первичный ввод') {
        sourceContainer.style.display = 'none';
        document.getElementById('newDocSource').value = "";
    } else if (type === 'Списание') {
        targetContainer.style.display = 'none';
        document.getElementById('newDocTarget').value = "";
    }
}

// 1. ДОКУМЕНТЫ
async function loadDocuments() {
    const tableBody = document.getElementById('docsTableBody');
    const counter = document.getElementById('docsCount');
    tableBody.innerHTML = '<tr><td colspan="7" class="text-center p-8"><i class="fa-solid fa-spinner fa-spin text-blue-600"></i> Загрузка журнала...</td></tr>';
    try {
        const response = await apiFetch('/api/arsenal/documents');
        if (!response || !response.ok) throw new Error('Ошибка сети');
        const documents = await response.json();
        counter.innerText = `Всего документов: ${documents.length}`;
        tableBody.innerHTML = '';
        if (documents.length === 0) {
            tableBody.innerHTML = '<tr><td colspan="7" class="text-center p-8 text-gray-400">Журнал пуст.</td></tr>';
            return;
        }
        documents.forEach(doc => {
            const tableRow = document.createElement('tr');
            tableRow.className = "cursor-pointer hover:bg-blue-50 transition border-b";
            tableRow.onclick = (e) => { if (!e.target.closest('.delete-btn')) openViewDocModal(doc.id); };
            let icon = '<i class="fa-solid fa-file text-gray-400"></i>';
            if (doc.type === 'Первичный ввод') icon = '<i class="fa-solid fa-file-import text-green-600"></i>';
            else if (['Отправка', 'Перемещение', 'Прием'].includes(doc.type)) icon = '<i class="fa-solid fa-truck-arrow-right text-orange-600"></i>';
            else if (doc.type === 'Списание') icon = '<i class="fa-solid fa-ban text-red-600"></i>';
            tableRow.innerHTML = `
                <td class="text-center text-lg py-3">${icon}</td>
                <td class="text-sm">${doc.date}</td>
                <td class="font-bold text-blue-900 text-sm">${doc.doc_number}</td>
                <td>${getTypeBadge(doc.type)}</td>
                <td class="text-sm text-gray-600">${doc.source || '---'}</td>
                <td class="text-sm text-gray-600">${doc.target || '---'}</td>
                <td class="text-center">
                    <button class="delete-btn text-gray-400 hover:text-red-600 p-2 rounded transition" data-id="${doc.id}"><i class="fa-solid fa-trash"></i></button>
                </td>`;
            tableBody.appendChild(tableRow);
        });
        document.querySelectorAll('.delete-btn').forEach(btn => {
            btn.addEventListener('click', function(e) { e.stopPropagation(); deleteDocument(this.dataset.id); });
        });
    } catch (error) {
        tableBody.innerHTML = '<tr><td colspan="7" class="text-center text-red-500 p-4">Ошибка загрузки.</td></tr>';
    }
}

async function createDocument() {
    const button = document.getElementById('btnSaveDoc');
    const docType = document.getElementById('newDocType').value;
    const sourceId = document.getElementById('newDocSource')?.value;
    const targetId = document.getElementById('newDocTarget')?.value;
    if (!docNumber) return alert('Введите номер документа.');
    if (docType === 'Первичный ввод' && !targetId) return alert('Укажите получателя.');
    if (docType === 'Списание' && !sourceId) return alert('Укажите источник.');
    if (['Перемещение', 'Отправка', 'Прием'].includes(docType) && (!sourceId || !targetId)) return alert('Укажите и отправителя, и получателя.');
    const items = [];
    let validationPassed = true;
    document.querySelectorAll('#docItemsTable tbody tr').forEach(row => {
        const nomenclatureId = row.querySelector('.nom-select').value;
        const serial = row.querySelector('.serial-input').value;
        const quantity = row.querySelector('.qty-input').value;
        if (nomenclatureId && !serial) validationPassed = false;
        if (nomenclatureId && serial) items.push({ nomenclature_id: parseInt(nomenclatureId), serial_number: serial, quantity: parseInt(quantity) || 1 });
    });
    if (!validationPassed) return alert('Укажите Серийный номер или Партию.');
    if (items.length === 0) return alert('Добавьте изделия.');
    button.disabled = true;
    const originalText = button.innerHTML;
    button.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Сохранение...';
    try {
        const response = await apiFetch('/api/arsenal/documents', {
            method: 'POST',
            body: JSON.stringify({ doc_number: null, operation_date: document.getElementById('newDocDate').value, operation_type: docType, source_id: sourceId ? parseInt(sourceId) : null, target_id: targetId ? parseInt(targetId) : null, items: items })
        });
        if (response && response.ok) {
            closeModal('newDocModal');
            loadDocuments();
        } else {
            const error = await response.json();
            alert('Ошибка: ' + (error.detail || 'Серверная ошибка.'));
        }
    } catch (error) { alert('Сетевая ошибка.'); }
    finally { button.disabled = false; button.innerHTML = originalText; }
}

async function deleteDocument(id) {
    if (!confirm('Удалить документ?')) return;
    try {
        const response = await apiFetch(`/api/arsenal/documents/${id}`, { method: 'DELETE' });
        if (response && response.ok) loadDocuments();
        else { const error = await response.json(); alert('Ошибка: ' + error.detail); }
    } catch (error) { alert('Ошибка удаления.'); }
}

async function openViewDocModal(id) {
    const tableBody = document.getElementById('viewDocItems');
    tableBody.innerHTML = '<tr><td colspan="3" class="text-center p-4"><i class="fa-solid fa-spinner fa-spin"></i> Загрузка...</td></tr>';
    openModal('viewDocModal');
    try {
        const response = await apiFetch(`/api/arsenal/documents/${id}`);
        if (!response || !response.ok) throw new Error('Doc not found');
        const doc = await response.json();
        document.getElementById('viewDocNumber').innerText = doc.doc_number;
        document.getElementById('viewDocDate').innerText = new Date(doc.operation_date).toLocaleDateString();
        document.getElementById('viewDocType').innerText = doc.operation_type;
        document.getElementById('viewDocSource').innerText = doc.source ? doc.source.name : '---';
        document.getElementById('viewDocTarget').innerText = doc.target ? doc.target.name : '---';
        tableBody.innerHTML = '';
        if (doc.items.length === 0) tableBody.innerHTML = '<tr><td colspan="3" class="text-center text-gray-500">Нет позиций.</td></tr>';
        doc.items.forEach(item => {
            tableBody.innerHTML += `<tr class="border-b last:border-0"><td class="p-2"><div class="font-bold text-gray-800">${item.nomenclature.name}</div><div class="text-xs text-gray-500 font-mono">${item.nomenclature.code || ''}</div></td><td class="p-2 font-mono text-blue-700">${item.serial_number || '-'}</td><td class="p-2 text-center font-bold">${item.quantity}</td></tr>`;
        });
    } catch (error) { tableBody.innerHTML = '<tr><td colspan="3" class="text-red-500 text-center">Ошибка.</td></tr>'; }
}

// 2. ОБЪЕКТЫ
async function loadObjectsTree() {
    const container = document.getElementById('orgTree');
    const targetSelect = document.getElementById('newDocTarget');
    const sourceSelect = document.getElementById('newDocSource');
    try {
        const response = await apiFetch('/api/arsenal/objects');
        if (!response || !response.ok) return;
        objects = await response.json();
        if (objects.length === 0) {
            container.innerHTML = '<div class="p-4 text-sm text-gray-500">Нет объектов.</div>';
            const emptyOption = '<option value="">Нет объектов</option>';
            if (targetSelect) targetSelect.innerHTML = emptyOption;
            if (sourceSelect) sourceSelect.innerHTML = emptyOption;
            return;
        }
        container.innerHTML = objects.map(o => `
            <div class="tree-node pl-4 transition hover:bg-blue-50 flex justify-between items-center group">
                <div class="flex-grow cursor-pointer py-1" onclick="showBalanceModal(${o.id}, '${o.name}')">
                    <i class="fa-solid fa-layer-group text-blue-500 mr-2"></i><span class="text-gray-700 ml-1 font-medium text-sm">${o.name}</span><span class="text-xs text-gray-400 ml-2">(${o.obj_type})</span>
                </div>
                <button onclick="showBalanceModal(${o.id}, '${o.name}')" class="text-gray-300 hover:text-green-600 px-2 py-1 text-xs opacity-0 group-hover:opacity-100 transition"><i class="fa-solid fa-box-archive"></i></button>
            </div>`).join('');
        const optionsHtml = '<option value="">-- Выберите объект --</option>' + objects.map(o => `<option value="${o.id}">${o.name}</option>`).join('');
        if (targetSelect) targetSelect.innerHTML = optionsHtml;
        if (sourceSelect) sourceSelect.innerHTML = optionsHtml;
    } catch (error) {}
}

async function createObject() {
    const name = document.getElementById('newObjName').value;
    const type = document.getElementById('newObjType').value;
    if (!name) return alert("Введите название.");

    try {
        const response = await apiFetch('/api/arsenal/objects', {
            method: 'POST',
            body: JSON.stringify({ name, obj_type: type })
        });

        if (response && response.ok) {
            const data = await response.json();
            closeModal('newObjectModal');
            document.getElementById('newObjName').value = '';
            loadObjectsTree();

            // --- НОВАЯ КРАСИВАЯ МОДАЛКА ВМЕСТО ALERT ---
            if (data.credentials) {
                showCredentialsModal(
                    `Объект "${data.name}" успешно создан!`,
                    data.credentials.username,
                    data.credentials.password
                );
            }
        }
        else {
            const error = await response.json();
            alert("Ошибка: " + error.detail);
        }
    } catch (error) {
        console.error(error);
    }
}

// 3. НОМЕНКЛАТУРА
async function loadNomenclature() {
    try {
        const response = await apiFetch('/api/arsenal/nomenclature');
        if (!response || !response.ok) return;
        nomenclatures = await response.json();
        renderNomenclatureList();
    } catch (error) {}
}

function renderNomenclatureList() {
    const tableBody = document.getElementById('nomenclatureListBody');
    if (!tableBody) return;
    if (nomenclatures.length === 0) { tableBody.innerHTML = '<tr><td colspan="2" class="p-4 text-center">Пусто.</td></tr>'; return; }
    tableBody.innerHTML = nomenclatures.map(n => `<tr class="hover:bg-gray-100 border-b last:border-0"><td class="p-2 border-r font-mono text-xs text-blue-600">${n.code || '-'}</td><td class="p-2 font-bold text-gray-700 text-sm">${n.name}</td></tr>`).join('');
}

function openNomenclatureModal() {
    document.getElementById('newNomCode').value = ''; document.getElementById('newNomName').value = ''; document.getElementById('newNomCat').value = ''; document.getElementById('newNomIsNumbered').checked = true; openModal('nomenclatureModal');
}

async function createNomenclature() {
    const code = document.getElementById('newNomCode').value;
    const name = document.getElementById('newNomName').value;
    const category = document.getElementById('newNomCat').value;
    const isNumbered = document.getElementById('newNomIsNumbered').checked;
    if (!name) return alert("Наименование обязательно.");
    try {
        const response = await apiFetch('/api/arsenal/nomenclature', { method: 'POST', body: JSON.stringify({ code, name, category, is_numbered: isNumbered }) });
        if (response && response.ok) { document.getElementById('newNomName').value = ''; document.getElementById('newNomCode').value = ''; await loadNomenclature(); }
        else { const error = await response.json(); alert("Ошибка: " + error.detail); }
    } catch (error) {}
}

// 4. ОСТАТКИ
async function showBalanceModal(objectId, objectName) {
    const title = document.getElementById('balanceModalTitle');
    const tableBody = document.getElementById('balanceTableBody');
    if (!title || !tableBody) return;
    title.innerText = `Остатки: ${objectName}`;
    tableBody.innerHTML = '<tr><td colspan="4" class="text-center p-8"><i class="fa-solid fa-spinner fa-spin text-green-600"></i> Загрузка...</td></tr>';
    openModal('balanceModal');
    try {
        const response = await apiFetch(`/api/arsenal/balance/${objectId}`);
        if (!response || !response.ok) throw new Error('Ошибка');
        const items = await response.json();
        if (items.length === 0) { tableBody.innerHTML = '<tr><td colspan="4" class="text-center p-8 text-gray-500">Пусто.</td></tr>'; return; }
        tableBody.innerHTML = '';
        items.forEach(item => {
            tableBody.innerHTML += `<tr><td class="p-2 border-b font-medium text-gray-800">${item.nomenclature}</td><td class="p-2 border-b font-mono text-xs text-gray-500">${item.code || '-'}</td><td class="p-2 border-b font-mono text-blue-700">${item.serial_number}</td><td class="p-2 border-b text-center font-bold bg-green-50">${item.quantity}</td></tr>`;
        });
    } catch (error) { tableBody.innerHTML = '<tr><td colspan="4" class="text-center p-8 text-red-500">Ошибка.</td></tr>'; }
}

// 5. ОТЧЕТЫ (LIFECYCLE)
async function searchForReport() {
    const query = document.getElementById('reportSearchInput').value.trim();
    if (query.length < 2) return alert("Минимум 2 символа");
    const listContainer = document.getElementById('reportSearchList');
    const resultsBlock = document.getElementById('reportSearchResults');
    listContainer.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';
    resultsBlock.classList.remove('hidden');
    try {
        const response = await apiFetch(`/api/arsenal/reports/search-weapon?q=${encodeURIComponent(query)}`);
        const items = await response.json();
        listContainer.innerHTML = '';
        if (items.length === 0) { listContainer.innerHTML = '<span class="text-gray-400 text-sm">Ничего не найдено</span>'; return; }
        items.forEach(item => {
            const btn = document.createElement('button');
            btn.className = "text-sm border border-purple-200 bg-purple-50 hover:bg-purple-100 px-3 py-1 rounded text-purple-900 transition text-left";
            btn.innerHTML = `<b>${item.name}</b> <span class="text-xs text-gray-500">№ ${item.serial}</span>`;
            btn.onclick = () => loadTimeline(item.serial, item.nom_id, item.name);
            listContainer.appendChild(btn);
        });
    } catch (e) {}
}

async function loadTimeline(serial, nomId, name) {
    const container = document.getElementById('reportTimeline');
    const statusBox = document.getElementById('reportCurrentStatus');
    container.innerHTML = '<div class="pl-6 text-gray-500">Загрузка...</div>';
    statusBox.classList.add('hidden');
    try {
        const response = await apiFetch(`/api/arsenal/reports/timeline?serial=${encodeURIComponent(serial)}&nom_id=${nomId}`);
        const data = await response.json();
        statusBox.innerText = `${name} (№ ${serial}) — ${data.status}`;
        statusBox.classList.remove('hidden');
        container.innerHTML = '';
        if (data.history.length === 0) { container.innerHTML = '<div class="pl-6">История пуста.</div>'; return; }
        data.history.forEach(event => {
            let color = "bg-gray-500", icon = "fa-file";
            if (event.op_type === "Первичный ввод") { color = "bg-green-500"; icon = "fa-plus"; }
            else if (event.op_type === "Списание") { color = "bg-red-500"; icon = "fa-trash"; }
            else if (event.op_type === "Перемещение") { color = "bg-blue-500"; icon = "fa-truck"; }
            else if (event.op_type === "Отправка") { color = "bg-orange-500"; icon = "fa-arrow-right"; }
            container.innerHTML += `
                <div class="mb-6 ml-6 relative group">
                    <span class="absolute -left-9 flex items-center justify-center w-6 h-6 ${color} rounded-full ring-4 ring-white text-white text-xs"><i class="fa-solid ${icon}"></i></span>
                    <div class="bg-white border border-gray-200 rounded p-3 shadow-sm hover:shadow-md transition">
                        <div class="flex justify-between mb-1"><span class="text-sm font-bold text-gray-800">${event.op_type}</span><span class="text-xs text-gray-500">${event.date}</span></div>
                        <div class="text-sm text-gray-600 mb-1">Документ: <span class="font-mono text-blue-600 font-bold">${event.doc_number}</span></div>
                        <div class="text-xs flex items-center gap-2 text-gray-500 bg-gray-50 p-2 rounded"><span class="truncate max-w-[120px]">${event.source}</span><i class="fa-solid fa-arrow-right text-gray-300"></i><span class="font-bold text-gray-700 truncate max-w-[120px]">${event.target}</span></div>
                    </div>
                </div>`;
        });
    } catch (e) { container.innerHTML = `<div class="pl-6 text-red-500">Ошибка: ${e.message}</div>`; }
}

// HELPERS
function getTypeBadge(type) {
    const map = { 'Первичный ввод': 'bg-green-100 text-green-800 border-green-200', 'Отправка': 'bg-orange-100 text-orange-800 border-orange-200', 'Списание': 'bg-red-100 text-red-800 border-red-200', 'Прием': 'bg-blue-100 text-blue-800 border-blue-200', 'Перемещение': 'bg-blue-100 text-blue-800 border-blue-200' };
    const classes = map[type] || 'bg-gray-100 text-gray-800 border-gray-200';
    return `<span class="px-2 py-0.5 rounded text-xs font-bold border ${classes}">${type}</span>`;
}
function openNewDocModal() {
    document.getElementById('newDocForm').reset();
    document.querySelector('#docItemsTable tbody').innerHTML = '';
    document.getElementById('newDocType').value = 'Первичный ввод';

    // --- НОВОЕ: Настройка поля номера ---
    const numInput = document.getElementById('newDocNumber');
    numInput.value = "АВТО"; // Показываем заглушку
    numInput.disabled = true; // Запрещаем редактирование
    numInput.classList.add('bg-gray-100', 'text-gray-500', 'cursor-not-allowed'); // Визуально делаем серым
    // ------------------------------------

    updateFormState();
    addDocRow();
    openModal('newDocModal');
}
function openModal(id) { const m = document.getElementById(id); if (m) m.style.display = 'flex'; }
function closeModal(id) { const m = document.getElementById(id); if (m) m.style.display = 'none'; }
function addDocRow() {
    const tableBody = document.querySelector('#docItemsTable tbody');
    const tableRow = document.createElement('tr'); tableRow.className = 'border-b';
    const opts = '<option value="">-- Выберите --</option>' + nomenclatures.map(n => `<option value="${n.id}" data-is-numbered="${n.is_numbered}">${n.name}${n.code ? ' ('+n.code+')' : ''}</option>`).join('');
    tableRow.innerHTML = `<td class="p-1"><select class="nom-select w-full border border-gray-300 p-1.5 rounded text-sm bg-white" onchange="handleNomenclatureChange(this)">${opts}</select></td><td class="p-1"><input type="text" class="serial-input w-full border border-gray-300 p-1.5 rounded text-sm" placeholder="№ / Партия"></td><td class="p-1"><input type="number" class="qty-input w-full border border-gray-300 p-1.5 rounded text-sm text-center" value="1" min="1"></td><td class="p-1 text-center"><button type="button" class="text-xl text-red-400 hover:text-red-600 p-1 leading-none" onclick="this.closest('tr').remove()">&times;</button></td>`;
    tableBody.appendChild(tableRow); handleNomenclatureChange(tableRow.querySelector('.nom-select'));
}
function handleNomenclatureChange(el) {
    const isNum = el.options[el.selectedIndex].dataset.isNumbered === 'true';
    const row = el.closest('tr');
    const qty = row.querySelector('.qty-input');
    const ser = row.querySelector('.serial-input');
    if (isNum) { qty.value = 1; qty.readOnly = true; qty.classList.add('bg-gray-100'); ser.placeholder = "Серийный номер"; }
    else { qty.readOnly = false; qty.classList.remove('bg-gray-100'); ser.placeholder = "Номер партии"; }
}

// ==========================================
// ФУНКЦИИ ДЛЯ КРАСИВОГО ПОКАЗА ПАРОЛЕЙ
// ==========================================
function showCredentialsModal(title, username, password) {
    document.getElementById('credModalTitle').innerText = title;
    document.getElementById('credUsername').innerText = username;
    document.getElementById('credPassword').innerText = password;

    const copyBtn = document.getElementById('btnCopyCreds');
    copyBtn.innerHTML = '<i class="fa-regular fa-copy"></i> Копировать';
    copyBtn.className = "absolute top-4 right-4 bg-white border border-gray-300 text-gray-600 hover:bg-gray-50 hover:text-blue-600 px-3 py-1 rounded text-sm shadow-sm transition flex items-center gap-2";

    // Логика копирования
    copyBtn.onclick = () => {
        const textToCopy = `Логин: ${username}\nПароль: ${password}`;
        navigator.clipboard.writeText(textToCopy).then(() => {
            copyBtn.innerHTML = '<i class="fa-solid fa-check text-green-600"></i> Скопировано';
            copyBtn.classList.add('border-green-300', 'bg-green-50');
            setTimeout(() => {
                copyBtn.innerHTML = '<i class="fa-regular fa-copy"></i> Копировать';
                copyBtn.classList.remove('border-green-300', 'bg-green-50');
            }, 2000);
        });
    };

    openModal('credentialsModal');
}

// ==========================================
// УПРАВЛЕНИЕ ПОЛЬЗОВАТЕЛЯМИ (Вкладка Админа)
// ==========================================
async function loadAndShowUsers() {
    openModal('usersModal');
    const tableBody = document.getElementById('usersTableBody');
    tableBody.innerHTML = '<tr><td colspan="5" class="text-center p-8"><i class="fa-solid fa-spinner fa-spin text-teal-600"></i> Загрузка...</td></tr>';

    try {
        const response = await apiFetch('/api/arsenal/users');
        if (!response || !response.ok) throw new Error("Ошибка загрузки");

        const users = await response.json();
        tableBody.innerHTML = '';

        users.forEach(u => {
            const roleBadge = u.role === 'admin'
                ? '<span class="px-2 py-0.5 bg-purple-100 text-purple-800 rounded text-xs font-bold border border-purple-200">Админ</span>'
                : '<span class="px-2 py-0.5 bg-blue-100 text-blue-800 rounded text-xs font-bold border border-blue-200">Начальник склада</span>';

            const btnReset = u.role !== 'admin'
                ? `<button onclick="resetUserPassword(${u.id}, '${u.username}')" class="text-gray-400 hover:text-red-600 p-1" title="Сбросить пароль"><i class="fa-solid fa-key"></i></button>`
                : '';

            tableBody.innerHTML += `
                <tr class="border-b hover:bg-gray-50">
                    <td class="p-2 text-gray-500">${u.id}</td>
                    <td class="p-2 font-mono font-bold text-gray-800">${u.username}</td>
                    <td class="p-2">${roleBadge}</td>
                    <td class="p-2 text-gray-600">${u.object_name}</td>
                    <td class="p-2 text-center">${btnReset}</td>
                </tr>
            `;
        });
    } catch (error) {
        tableBody.innerHTML = '<tr><td colspan="5" class="text-center p-8 text-red-500">Ошибка загрузки пользователей</td></tr>';
    }
}

async function resetUserPassword(userId, username) {
    if (!confirm(`Вы уверены, что хотите сбросить пароль для пользователя ${username}? Старый пароль перестанет работать.`)) return;

    try {
        const response = await apiFetch(`/api/arsenal/users/${userId}/reset-password`, { method: 'POST' });
        if (response && response.ok) {
            const data = await response.json();
            closeModal('usersModal');
            // Используем наше новое красивое окно для показа сгенерированного пароля!
            showCredentialsModal(
                `Пароль сброшен!`,
                data.username,
                data.new_password
            );
        } else {
            alert("Ошибка сброса пароля");
        }
    } catch (e) {
        console.error(e);
    }
}