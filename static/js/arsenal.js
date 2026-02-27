/**
 * СТРОБ.Арсенал - Клиентская логика v2.2 (Excel Import & Accounting)
 * Файл логически разделен на модули для удобства дальнейшего разделения на отдельные файлы.
 */

// ============================================================================
// МОДУЛЬ 1: API И ГЛОБАЛЬНОЕ СОСТОЯНИЕ (API & State)
// ============================================================================
const AppState = {
    nomenclatures: [],
    objects: [],
    userRole: localStorage.getItem('arsenal_role') || 'unit_head'
};

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

// ============================================================================
// МОДУЛЬ 2: ИНИЦИАЛИЗАЦИЯ И UI (Core UI)
// ============================================================================
document.addEventListener('DOMContentLoaded', async () => {
    // 1. Проверка прав (Скрываем элементы для не-админов)
    if (AppState.userRole === 'unit_head') {
        const hideElements = ['btnAddObject', 'menuNomenclature', 'menuUsers', 'btnImportExcel'];
        hideElements.forEach(id => {
            const el = document.getElementById(id);
            if (el) el.style.display = 'none';
        });
    }

    // 2. Инициализация UI
    UI.injectSourceSelectIfNeeded();
    UI.bindEvents();

    const dateInput = document.getElementById('newDocDate');
    if (dateInput) dateInput.valueAsDate = new Date();

    // 3. Загрузка стартовых данных
    await Promise.all([Dictionaries.loadNomenclature(), Dictionaries.loadObjectsTree()]);
    Documents.updateFormState();
    Documents.loadList();
});

const UI = {
    openModal: (id) => { const m = document.getElementById(id); if (m) m.style.display = 'flex'; },
    closeModal: (id) => { const m = document.getElementById(id); if (m) m.style.display = 'none'; },

    injectSourceSelectIfNeeded: () => {
        if (document.getElementById('newDocSource')) return;
        const targetContainer = document.getElementById('targetSelectContainer');
        if (!targetContainer) return;
        const formGrid = targetContainer.parentElement;
        const sourceContainer = document.createElement('div');
        sourceContainer.id = 'sourceSelectContainer';
        sourceContainer.innerHTML = `
            <label class="block text-gray-700 font-bold mb-1">Отправитель / Источник</label>
            <select id="newDocSource" class="w-full border border-gray-300 p-2 rounded bg-white focus:border-blue-500 outline-none">
                <option value="">Загрузка...</option>
            </select>
        `;
        formGrid.insertBefore(sourceContainer, targetContainer);
    },

    bindEvents: () => {
        // Меню
        document.getElementById('menuDocs')?.addEventListener('click', Documents.loadList);
        document.getElementById('menuObjects')?.addEventListener('click', Dictionaries.loadObjectsTree);
        document.getElementById('menuNomenclature')?.addEventListener('click', Dictionaries.openNomenclatureModal);
        document.getElementById('menuUsers')?.addEventListener('click', Users.loadAndShow);
        document.getElementById('menuReports')?.addEventListener('click', () => UI.openModal('reportModal'));

        // Кнопки модалок
        document.getElementById('btnAddObject')?.addEventListener('click', () => UI.openModal('newObjectModal'));
        document.getElementById('btnOpenCreateModal')?.addEventListener('click', Documents.openCreateModal);
        document.getElementById('btnRefreshDocs')?.addEventListener('click', Documents.loadList);

        // Формы
        document.getElementById('newDocType')?.addEventListener('change', Documents.updateFormState);
        document.getElementById('btnAddRow')?.addEventListener('click', Documents.addRow);
        document.getElementById('btnSaveDoc')?.addEventListener('click', Documents.create);
        document.getElementById('btnSaveObject')?.addEventListener('click', Dictionaries.createObject);
        document.getElementById('btnSaveNom')?.addEventListener('click', Dictionaries.createNomenclature);

        // Отчеты
        document.getElementById('btnReportSearch')?.addEventListener('click', Reports.search);
        document.getElementById('reportSearchInput')?.addEventListener('keyup', (e) => { if (e.key === 'Enter') Reports.search(); });

        // Закрытие модалок
        document.querySelectorAll('.modal-close-btn, #btnCloseModal, #btnCancelModal').forEach(btn => {
            btn.addEventListener('click', () => UI.closeModal(btn.closest('.modal').id));
        });
        document.querySelectorAll('.modal').forEach(modal => {
            modal.addEventListener('click', e => { if (e.target === modal) UI.closeModal(modal.id); });
        });
        document.addEventListener('keydown', e => {
            if (e.key === 'Escape') document.querySelectorAll('.modal').forEach(m => UI.closeModal(m.id));
        });

        // Выход
        document.getElementById('logoutBtn')?.addEventListener('click', async () => {
            try { await fetch('/api/arsenal/logout', { method: 'POST' }); } catch (e) {}
            window.location.href = 'arsenal_login.html';
        });

        // ==========================================
        // 🔥 ЛОГИКА ИМПОРТА ИЗ EXCEL 🔥
        // ==========================================
        const btnImport = document.getElementById('btnImportExcel');
        const fileInput = document.getElementById('excelUploadInput');

        if (btnImport && fileInput) {
            btnImport.addEventListener('click', () => {
                fileInput.click(); // Открываем окно выбора файла
            });

            fileInput.addEventListener('change', async (event) => {
                const file = event.target.files[0];
                if (!file) return;

                // Показываем спиннер загрузки
                UI.openModal('loadingOverlay');

                const formData = new FormData();
                formData.append("file", file);

                try {
                    // Используем чистый fetch, чтобы браузер сам выставил Boundary для multipart/form-data
                    const response = await fetch('/api/arsenal/import', {
                        method: 'POST',
                        body: formData
                    });

                    UI.closeModal('loadingOverlay');

                    if (response.ok) {
                        const result = await response.json();
                        let msg = `✅ Импорт успешно завершен!\n\n➕ Добавлено: ${result.added}\n🔄 Обновлено: ${result.updated}\n⏭ Пропущено: ${result.skipped}`;

                        // Если есть ошибки или пропуски - покажем первые 10
                        if (result.errors && result.errors.length > 0) {
                            msg += `\n\n⚠️ Примеры ошибок/пропусков:\n` + result.errors.slice(0, 10).join('\n');
                        } else if (result.skipped > 0 && (!result.errors || result.errors.length === 0)) {
                            msg += `\n\n(Строки пропущены, так как в колонке А не найдено число)`;
                        }

                        alert(msg);

                        await Dictionaries.loadObjectsTree();
                        await Dictionaries.loadNomenclature();
                    } else {
                        const error = await response.json();
                        alert(`❌ Ошибка импорта: ${error.detail || 'Неизвестная ошибка'}`);
                    }
                } catch (error) {
                    UI.closeModal('loadingOverlay');
                    alert(`❌ Критическая ошибка при отправке файла: ${error.message}`);
                    console.error(error);
                }

                // Очищаем инпут, чтобы можно было загрузить тот же файл еще раз, если надо
                fileInput.value = '';
            });
        }
    },

    showCredentialsModal: (title, username, password) => {
        document.getElementById('credModalTitle').innerText = title;
        document.getElementById('credUsername').innerText = username;
        document.getElementById('credPassword').innerText = password;

        const copyBtn = document.getElementById('btnCopyCreds');
        copyBtn.innerHTML = '<i class="fa-regular fa-copy"></i> Копировать';
        copyBtn.className = "absolute top-4 right-4 bg-white border border-gray-300 text-gray-600 hover:bg-gray-50 hover:text-blue-600 px-3 py-1 rounded text-sm shadow-sm transition flex items-center gap-2";

        copyBtn.onclick = () => {
            navigator.clipboard.writeText(`Логин: ${username}\nПароль: ${password}`).then(() => {
                copyBtn.innerHTML = '<i class="fa-solid fa-check text-green-600"></i> Скопировано';
                copyBtn.classList.add('border-green-300', 'bg-green-50');
                setTimeout(() => {
                    copyBtn.innerHTML = '<i class="fa-regular fa-copy"></i> Копировать';
                    copyBtn.classList.remove('border-green-300', 'bg-green-50');
                }, 2000);
            });
        };
        UI.openModal('credentialsModal');
    }
};

// ============================================================================
// МОДУЛЬ 3: ДОКУМЕНТЫ (Documents)
// ============================================================================
const Documents = {
    loadList: async () => {
        const tableBody = document.getElementById('docsTableBody');
        const counter = document.getElementById('docsCount');
        tableBody.innerHTML = '<tr><td colspan="7" class="text-center p-8"><i class="fa-solid fa-spinner fa-spin text-blue-600"></i> Загрузка журнала...</td></tr>';
        try {
            const response = await apiFetch('/api/arsenal/documents');
            if (!response || !response.ok) throw new Error('Ошибка сети');
            const docs = await response.json();
            counter.innerText = `Всего документов: ${docs.length}`;
            tableBody.innerHTML = '';

            if (docs.length === 0) {
                tableBody.innerHTML = '<tr><td colspan="7" class="text-center p-8 text-gray-400">Журнал пуст.</td></tr>';
                return;
            }

            docs.forEach(doc => {
                const tr = document.createElement('tr');
                tr.className = "cursor-pointer hover:bg-blue-50 transition border-b";
                tr.onclick = (e) => { if (!e.target.closest('.delete-btn')) Documents.openViewModal(doc.id); };

                let icon = '<i class="fa-solid fa-file text-gray-400"></i>';
                if (doc.type === 'Первичный ввод') icon = '<i class="fa-solid fa-file-import text-green-600"></i>';
                else if (['Отправка', 'Перемещение', 'Прием'].includes(doc.type)) icon = '<i class="fa-solid fa-truck-arrow-right text-orange-600"></i>';
                else if (doc.type === 'Списание') icon = '<i class="fa-solid fa-ban text-red-600"></i>';

                tr.innerHTML = `
                    <td class="text-center text-lg py-3">${icon}</td>
                    <td class="text-sm">${doc.date}</td>
                    <td class="font-bold text-blue-900 text-sm">${doc.doc_number}</td>
                    <td>${Documents.getTypeBadge(doc.type)}</td>
                    <td class="text-sm text-gray-600">${doc.source || '---'}</td>
                    <td class="text-sm text-gray-600">${doc.target || '---'}</td>
                    <td class="text-center"><button class="delete-btn text-gray-400 hover:text-red-600 p-2 rounded" data-id="${doc.id}"><i class="fa-solid fa-trash"></i></button></td>`;
                tableBody.appendChild(tr);
            });

            document.querySelectorAll('.delete-btn').forEach(btn => {
                btn.addEventListener('click', function(e) { e.stopPropagation(); Documents.delete(this.dataset.id); });
            });
        } catch (error) { tableBody.innerHTML = '<tr><td colspan="7" class="text-center text-red-500 p-4">Ошибка загрузки.</td></tr>'; }
    },

    openCreateModal: () => {
        document.getElementById('newDocForm').reset();
        document.querySelector('#docItemsTable tbody').innerHTML = '';
        document.getElementById('newDocType').value = 'Первичный ввод';

        const numInput = document.getElementById('newDocNumber');
        numInput.value = "АВТО";
        numInput.disabled = true;
        numInput.classList.add('bg-gray-100', 'text-gray-500', 'cursor-not-allowed');

        Documents.updateFormState();
        Documents.addRow();
        UI.openModal('newDocModal');
    },

    updateFormState: () => {
        const type = document.getElementById('newDocType').value;
        const sourceContainer = document.getElementById('sourceSelectContainer');
        const targetContainer = document.getElementById('targetSelectContainer');

        if (sourceContainer && targetContainer) {
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

        // ПОКАЗЫВАЕМ БУХГАЛТЕРСКИЕ ПОЛЯ ТОЛЬКО ПРИ ПЕРВИЧНОМ ВВОДЕ
        const showAccounting = (type === 'Первичный ввод');
        document.querySelectorAll('.doc-accounting-col').forEach(el => {
            el.style.display = showAccounting ? '' : 'none';
        });
    },

    addRow: () => {
        const tableBody = document.querySelector('#docItemsTable tbody');
        const tr = document.createElement('tr');
        tr.className = 'border-b';

        const showAcc = document.getElementById('newDocType').value === 'Первичный ввод' ? '' : 'display:none;';
        const opts = '<option value="">-- Выберите --</option>' + AppState.nomenclatures.map(n => `<option value="${n.id}" data-is-numbered="${n.is_numbered}">${n.name}${n.code ? ' ('+n.code+')' : ''}</option>`).join('');

        tr.innerHTML = `
            <td class="p-1"><select class="nom-select w-full border border-gray-300 p-1.5 rounded text-sm bg-white" onchange="Documents.handleNomChange(this)">${opts}</select></td>
            <td class="p-1"><input type="text" class="serial-input w-full border border-gray-300 p-1.5 rounded text-sm" placeholder="№ / Партия"></td>
            <td class="p-1 doc-accounting-col" style="${showAcc}"><input type="text" class="inv-input w-full border border-gray-300 p-1.5 rounded text-sm" placeholder="Инв. №"></td>
            <td class="p-1 doc-accounting-col" style="${showAcc}"><input type="number" step="0.01" class="price-input w-full border border-gray-300 p-1.5 rounded text-sm" placeholder="0.00"></td>
            <td class="p-1"><input type="number" class="qty-input w-full border border-gray-300 p-1.5 rounded text-sm text-center" value="1" min="1"></td>
            <td class="p-1 text-center"><button type="button" class="text-xl text-red-400 hover:text-red-600 p-1 leading-none" onclick="this.closest('tr').remove()">&times;</button></td>
        `;
        tableBody.appendChild(tr);
    },

    handleNomChange: (selectEl) => {
        if(selectEl.selectedIndex === 0) return;
        const isNum = selectEl.options[selectEl.selectedIndex].dataset.isNumbered === 'true';
        const row = selectEl.closest('tr');
        const qty = row.querySelector('.qty-input');
        const ser = row.querySelector('.serial-input');

        if (isNum) {
            qty.value = 1;
            qty.readOnly = true;
            qty.classList.add('bg-gray-100');
            ser.placeholder = "Заводской номер";
        } else {
            qty.readOnly = false;
            qty.classList.remove('bg-gray-100');
            ser.placeholder = "Номер партии";
        }
    },

    create: async () => {
        const btn = document.getElementById('btnSaveDoc');
        const docType = document.getElementById('newDocType').value;
        const sourceId = document.getElementById('newDocSource')?.value;
        const targetId = document.getElementById('newDocTarget')?.value;

        if (docType === 'Первичный ввод' && !targetId) return alert('Укажите получателя.');
        if (docType === 'Списание' && !sourceId) return alert('Укажите источник.');
        if (['Перемещение', 'Отправка', 'Прием'].includes(docType) && (!sourceId || !targetId)) return alert('Укажите и отправителя, и получателя.');

        const items = [];
        let valid = true;

        document.querySelectorAll('#docItemsTable tbody tr').forEach(row => {
            const nomId = row.querySelector('.nom-select').value;
            const serial = row.querySelector('.serial-input').value;
            const invNum = row.querySelector('.inv-input').value;
            const price = row.querySelector('.price-input').value;
            const qty = row.querySelector('.qty-input').value;

            if (nomId && !serial) valid = false;
            if (nomId && serial) {
                items.push({
                    nomenclature_id: parseInt(nomId),
                    serial_number: serial,
                    quantity: parseInt(qty) || 1,
                    inventory_number: invNum || null,
                    price: price ? parseFloat(price) : null
                });
            }
        });

        if (!valid) return alert('Укажите Серийный номер или Партию для всех изделий.');
        if (items.length === 0) return alert('Добавьте спецификацию.');

        btn.disabled = true;
        const origText = btn.innerHTML;
        btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Сохранение...';

        try {
            const response = await apiFetch('/api/arsenal/documents', {
                method: 'POST',
                body: JSON.stringify({
                    doc_number: null, // Сервер сгенерирует сам
                    operation_date: document.getElementById('newDocDate').value,
                    operation_type: docType,
                    source_id: sourceId ? parseInt(sourceId) : null,
                    target_id: targetId ? parseInt(targetId) : null,
                    items: items
                })
            });
            if (response && response.ok) {
                UI.closeModal('newDocModal');
                Documents.loadList();
            } else {
                const err = await response.json();
                alert('Ошибка: ' + (err.detail || 'Серверная ошибка.'));
            }
        } catch (e) { alert('Сетевая ошибка.'); }
        finally { btn.disabled = false; btn.innerHTML = origText; }
    },

    delete: async (id) => {
        if (!confirm('Удалить документ навсегда?')) return;
        try {
            const res = await apiFetch(`/api/arsenal/documents/${id}`, { method: 'DELETE' });
            if (res && res.ok) Documents.loadList();
            else alert('Ошибка удаления.');
        } catch (e) {}
    },

    openViewModal: async (id) => {
        const tBody = document.getElementById('viewDocItems');
        tBody.innerHTML = '<tr><td colspan="3" class="text-center p-4"><i class="fa-solid fa-spinner fa-spin"></i> Загрузка...</td></tr>';
        UI.openModal('viewDocModal');
        try {
            const res = await apiFetch(`/api/arsenal/documents/${id}`);
            const doc = await res.json();
            document.getElementById('viewDocNumber').innerText = doc.doc_number;
            document.getElementById('viewDocDate').innerText = new Date(doc.operation_date).toLocaleDateString();
            document.getElementById('viewDocType').innerText = doc.operation_type;
            document.getElementById('viewDocSource').innerText = doc.source ? doc.source.name : '---';
            document.getElementById('viewDocTarget').innerText = doc.target ? doc.target.name : '---';

            tBody.innerHTML = '';
            if (doc.items.length === 0) tBody.innerHTML = '<tr><td colspan="3" class="text-center">Нет позиций.</td></tr>';
            doc.items.forEach(i => {
                tBody.innerHTML += `<tr class="border-b last:border-0"><td class="p-2"><div class="font-bold text-gray-800">${i.nomenclature.name}</div><div class="text-xs text-gray-500 font-mono">${i.nomenclature.code || ''}</div></td><td class="p-2 font-mono text-blue-700">${i.serial_number || '-'}</td><td class="p-2 text-center font-bold">${i.quantity}</td></tr>`;
            });
        } catch (e) { tBody.innerHTML = '<tr><td colspan="3" class="text-red-500 text-center">Ошибка.</td></tr>'; }
    },

    getTypeBadge: (type) => {
        const map = { 'Первичный ввод': 'bg-green-100 text-green-800', 'Отправка': 'bg-orange-100 text-orange-800', 'Списание': 'bg-red-100 text-red-800', 'Прием': 'bg-blue-100 text-blue-800', 'Перемещение': 'bg-blue-100 text-blue-800' };
        return `<span class="px-2 py-0.5 rounded text-xs font-bold border ${map[type] || 'bg-gray-100'}">${type}</span>`;
    }
};

// ============================================================================
// МОДУЛЬ 4: СПРАВОЧНИКИ (Objects & Nomenclature)
// ============================================================================
const Dictionaries = {
    loadObjectsTree: async () => {
        const container = document.getElementById('orgTree');
        try {
            const response = await apiFetch('/api/arsenal/objects');
            if (!response || !response.ok) return;
            AppState.objects = await response.json();

            if (AppState.objects.length === 0) {
                container.innerHTML = '<div class="p-4 text-sm text-gray-500">Нет объектов.</div>';
                return;
            }

            container.innerHTML = AppState.objects.map(o => `
                <div class="tree-node pl-4 transition hover:bg-blue-50 flex justify-between items-center group">
                    <div class="flex-grow cursor-pointer py-1" onclick="Balance.showModal(${o.id}, '${o.name}')">
                        <i class="fa-solid ${o.obj_type==='Склад' ? 'fa-box' : 'fa-layer-group'} text-blue-500 mr-2"></i>
                        <span class="text-gray-700 ml-1 font-medium text-sm">${o.name}</span>
                        ${o.mol_name ? `<div class="text-xs text-gray-400 ml-6"><i class="fa-solid fa-user-tag text-gray-300"></i> МОЛ: ${o.mol_name}</div>` : ''}
                    </div>
                    <button onclick="Balance.showModal(${o.id}, '${o.name}')" class="text-gray-300 hover:text-green-600 px-2 py-1 text-xs opacity-0 group-hover:opacity-100 transition" title="Остатки"><i class="fa-solid fa-box-archive text-lg"></i></button>
                </div>`).join('');

            const opts = '<option value="">-- Выберите объект --</option>' + AppState.objects.map(o => `<option value="${o.id}">${o.name}</option>`).join('');
            const ts = document.getElementById('newDocTarget'), ss = document.getElementById('newDocSource');
            if (ts) ts.innerHTML = opts; if (ss) ss.innerHTML = opts;
        } catch (e) {}
    },

    createObject: async () => {
        const name = document.getElementById('newObjName').value;
        const type = document.getElementById('newObjType').value;
        const mol = document.getElementById('newObjMol').value; // НОВОЕ
        if (!name) return alert("Введите название.");

        try {
            const res = await apiFetch('/api/arsenal/objects', {
                method: 'POST', body: JSON.stringify({ name, obj_type: type, mol_name: mol || null })
            });
            if (res && res.ok) {
                const data = await res.json();
                UI.closeModal('newObjectModal');
                document.getElementById('newObjName').value = '';
                document.getElementById('newObjMol').value = '';
                Dictionaries.loadObjectsTree();
                if (data.credentials) UI.showCredentialsModal(`Объект "${data.name}" успешно создан!`, data.credentials.username, data.credentials.password);
            } else {
                const err = await res.json(); alert("Ошибка: " + err.detail);
            }
        } catch (e) {}
    },

    loadNomenclature: async () => {
        try {
            const res = await apiFetch('/api/arsenal/nomenclature');
            if (!res || !res.ok) return;
            AppState.nomenclatures = await res.json();
            const tb = document.getElementById('nomenclatureListBody');
            if (!tb) return;
            if (AppState.nomenclatures.length === 0) tb.innerHTML = '<tr><td colspan="3" class="p-4 text-center">Пусто.</td></tr>';
            else tb.innerHTML = AppState.nomenclatures.map(n => `<tr class="hover:bg-gray-100 border-b"><td class="p-2 border-r font-mono text-xs text-blue-600">${n.code || '-'}</td><td class="p-2 font-bold text-gray-700 text-sm">${n.name}</td><td class="p-2 text-xs font-mono text-gray-500">${n.default_account || '-'}</td></tr>`).join('');
        } catch (e) {}
    },

    openNomenclatureModal: () => {
        document.getElementById('newNomCode').value = '';
        document.getElementById('newNomName').value = '';
        document.getElementById('newNomAccount').value = '';
        document.getElementById('newNomIsNumbered').checked = true;
        UI.openModal('nomenclatureModal');
    },

    createNomenclature: async () => {
        const code = document.getElementById('newNomCode').value;
        const name = document.getElementById('newNomName').value;
        const account = document.getElementById('newNomAccount').value; // НОВОЕ
        const isNum = document.getElementById('newNomIsNumbered').checked;
        if (!name) return alert("Наименование обязательно.");
        try {
            const res = await apiFetch('/api/arsenal/nomenclature', { method: 'POST', body: JSON.stringify({ code, name, is_numbered: isNum, default_account: account || null }) });
            if (res && res.ok) { document.getElementById('newNomName').value = ''; document.getElementById('newNomCode').value = ''; document.getElementById('newNomAccount').value = ''; await Dictionaries.loadNomenclature(); }
            else { const err = await res.json(); alert("Ошибка: " + err.detail); }
        } catch (e) {}
    }
};

// ============================================================================
// МОДУЛЬ 5: ОСТАТКИ НА СКЛАДАХ (Balance & Inventory)
// ============================================================================
const Balance = {
    showModal: async (objectId, objectName) => {
        const title = document.getElementById('balanceModalTitle');
        const tb = document.getElementById('balanceTableBody');
        const sumEl = document.getElementById('balanceTotalSum');

        title.innerText = `Остатки: ${objectName}`;
        sumEl.innerText = "0.00 ₽";
        tb.innerHTML = '<tr><td colspan="7" class="text-center p-8"><i class="fa-solid fa-spinner fa-spin text-green-600"></i> Загрузка...</td></tr>';
        UI.openModal('balanceModal');

        try {
            const res = await apiFetch(`/api/arsenal/balance/${objectId}`);
            const items = await res.json();
            tb.innerHTML = '';

            if (items.length === 0) {
                tb.innerHTML = '<tr><td colspan="7" class="text-center p-8 text-gray-500">Склад пуст.</td></tr>';
                return;
            }

            let totalSum = 0;

            items.forEach(i => {
                const itemTotal = (i.price || 0) * i.quantity;
                totalSum += itemTotal;

                const priceFormatted = (i.price || 0).toLocaleString('ru-RU', { style: 'currency', currency: 'RUB' });
                const totalFormatted = itemTotal.toLocaleString('ru-RU', { style: 'currency', currency: 'RUB' });

                tb.innerHTML += `
                    <tr class="hover:bg-green-50">
                        <td class="p-2 border-b font-medium text-gray-800">${i.nomenclature}</td>
                        <td class="p-2 border-b font-mono text-xs text-purple-700">${i.account}</td>
                        <td class="p-2 border-b font-mono text-blue-700">${i.serial_number}</td>
                        <td class="p-2 border-b font-mono text-xs text-gray-600">${i.inventory_number}</td>
                        <td class="p-2 border-b text-right text-xs">${priceFormatted}</td>
                        <td class="p-2 border-b text-center font-bold bg-green-100">${i.quantity}</td>
                        <td class="p-2 border-b text-right font-bold text-gray-800">${totalFormatted}</td>
                    </tr>`;
            });

            sumEl.innerText = totalSum.toLocaleString('ru-RU', { style: 'currency', currency: 'RUB' });

        } catch (e) { tb.innerHTML = '<tr><td colspan="7" class="text-center p-8 text-red-500">Ошибка получения остатков.</td></tr>'; }
    }
};

// ============================================================================
// МОДУЛЬ 6: ПОЛЬЗОВАТЕЛИ И ОТЧЕТЫ (Users & Reports)
// ============================================================================
const Users = {
    loadAndShow: async () => {
        UI.openModal('usersModal');
        const tb = document.getElementById('usersTableBody');
        tb.innerHTML = '<tr><td colspan="5" class="text-center p-8"><i class="fa-solid fa-spinner fa-spin text-teal-600"></i> Загрузка...</td></tr>';
        try {
            const res = await apiFetch('/api/arsenal/users');
            const users = await res.json();
            tb.innerHTML = '';
            users.forEach(u => {
                const roleBadge = u.role === 'admin' ? '<span class="px-2 py-0.5 bg-purple-100 text-purple-800 rounded text-xs border border-purple-200">Админ</span>' : '<span class="px-2 py-0.5 bg-blue-100 text-blue-800 rounded text-xs border border-blue-200">МОЛ / Нач.склада</span>';
                const btnReset = u.role !== 'admin' ? `<button onclick="Users.resetPass(${u.id}, '${u.username}')" class="text-gray-400 hover:text-red-600 p-1" title="Сбросить пароль"><i class="fa-solid fa-key"></i></button>` : '';
                tb.innerHTML += `<tr class="border-b hover:bg-gray-50"><td class="p-2 text-gray-500">${u.id}</td><td class="p-2 font-mono font-bold text-gray-800">${u.username}</td><td class="p-2">${roleBadge}</td><td class="p-2 text-gray-600">${u.object_name}</td><td class="p-2 text-center">${btnReset}</td></tr>`;
            });
        } catch (e) {}
    },
    resetPass: async (id, username) => {
        if (!confirm(`Сбросить пароль для ${username}?`)) return;
        try {
            const res = await apiFetch(`/api/arsenal/users/${id}/reset-password`, { method: 'POST' });
            if (res && res.ok) {
                const data = await res.json();
                UI.closeModal('usersModal');
                UI.showCredentialsModal(`Пароль сброшен!`, data.username, data.new_password);
            }
        } catch (e) {}
    }
};

const Reports = {
    search: async () => {
        const q = document.getElementById('reportSearchInput').value.trim();
        if (q.length < 2) return alert("Минимум 2 символа");
        const list = document.getElementById('reportSearchList');
        document.getElementById('reportSearchResults').classList.remove('hidden');
        list.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';
        try {
            const res = await apiFetch(`/api/arsenal/reports/search-weapon?q=${encodeURIComponent(q)}`);
            const items = await res.json();
            list.innerHTML = items.length ? '' : '<span class="text-gray-400">Ничего не найдено</span>';
            items.forEach(i => {
                const btn = document.createElement('button');
                btn.className = "text-sm border border-purple-200 bg-purple-50 hover:bg-purple-100 px-3 py-1 rounded text-left";
                btn.innerHTML = `<b>${i.name}</b> <span class="text-xs text-gray-500">№ ${i.serial}</span>`;
                btn.onclick = () => Reports.loadTimeline(i.serial, i.nom_id, i.name);
                list.appendChild(btn);
            });
        } catch (e) {}
    },
    loadTimeline: async (serial, nomId, name) => {
        const container = document.getElementById('reportTimeline');
        const statusBox = document.getElementById('reportCurrentStatus');
        container.innerHTML = '<div class="pl-6 text-gray-500">Загрузка...</div>';
        statusBox.classList.add('hidden');
        try {
            const res = await apiFetch(`/api/arsenal/reports/timeline?serial=${encodeURIComponent(serial)}&nom_id=${nomId}`);
            const data = await res.json();
            statusBox.innerText = `${name} (№ ${serial}) — ${data.status}`;
            statusBox.classList.remove('hidden');
            container.innerHTML = data.history.length ? '' : '<div class="pl-6">История пуста.</div>';
            data.history.forEach(e => {
                let color = "bg-gray-500", icon = "fa-file";
                if (e.op_type === "Первичный ввод") { color = "bg-green-500"; icon = "fa-plus"; }
                else if (e.op_type === "Списание") { color = "bg-red-500"; icon = "fa-trash"; }
                container.innerHTML += `
                    <div class="mb-6 ml-6 relative group">
                        <span class="absolute -left-9 flex items-center justify-center w-6 h-6 ${color} rounded-full ring-4 ring-white text-white text-xs"><i class="fa-solid ${icon}"></i></span>
                        <div class="bg-white border border-gray-200 rounded p-3 shadow-sm hover:shadow-md transition">
                            <div class="flex justify-between mb-1"><span class="text-sm font-bold text-gray-800">${e.op_type}</span><span class="text-xs text-gray-500">${e.date}</span></div>
                            <div class="text-sm text-gray-600 mb-1">Документ: <span class="font-mono text-blue-600 font-bold">${e.doc_number}</span></div>
                            <div class="text-xs flex items-center gap-2 text-gray-500 bg-gray-50 p-2 rounded"><span class="truncate max-w-[120px]">${e.source}</span><i class="fa-solid fa-arrow-right text-gray-300"></i><span class="font-bold text-gray-700 truncate max-w-[120px]">${e.target}</span></div>
                        </div>
                    </div>`;
            });
        } catch (e) {}
    }
};