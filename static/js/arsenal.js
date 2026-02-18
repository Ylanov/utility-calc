// static/js/arsenal.js

// --- Глобальные переменные ---
let nomenclatures = []; // Кэш справочника номенклатуры
let objects = [];       // Кэш справочника объектов

// --- Инициализация при загрузке страницы ---
document.addEventListener('DOMContentLoaded', async () => {

    // Навешиваем обработчики событий (чтобы не использовать onclick в HTML)
    bindEvents();

    // Устанавливаем текущую дату в поле модалки
    const dateInput = document.getElementById('newDocDate');
    if (dateInput) dateInput.valueAsDate = new Date();

    // Загружаем данные
    await Promise.all([
        loadNomenclature(),
        loadObjectsTree()
    ]);

    loadDocuments();
});

// --- Настройка событий (Event Listeners) ---
function bindEvents() {
    // Кнопки меню
    document.getElementById('menuDocs')?.addEventListener('click', loadDocuments);
    document.getElementById('menuObjects')?.addEventListener('click', loadObjectsTree);

    // Тулбар
    document.getElementById('btnOpenCreateModal')?.addEventListener('click', openNewDocModal);
    document.getElementById('btnRefreshDocs')?.addEventListener('click', loadDocuments);

    // Модальное окно
    document.getElementById('btnCloseModal')?.addEventListener('click', () => closeModal('newDocModal'));
    document.getElementById('btnCancelModal')?.addEventListener('click', () => closeModal('newDocModal'));
    document.getElementById('btnSaveDoc')?.addEventListener('click', createDocument);
    document.getElementById('btnAddRow')?.addEventListener('click', addDocRow);

    // Выход
    document.getElementById('logoutBtn')?.addEventListener('click', logout);
}

// --- ФУНКЦИИ API ---

// 1. Загрузка документов
async function loadDocuments() {
    const tbody = document.getElementById('docsTableBody');
    const counter = document.getElementById('docsCount');

    tbody.innerHTML = '<tr><td colspan="7" class="text-center p-8 text-gray-500"><i class="fa-solid fa-spinner fa-spin mr-2"></i>Загрузка данных...</td></tr>';

    try {
        const res = await fetch('/api/arsenal/documents');
        if (!res.ok) throw new Error(`Ошибка сервера: ${res.status}`);

        const docs = await res.json();

        tbody.innerHTML = '';
        counter.innerText = `Всего документов: ${docs.length}`;

        if (docs.length === 0) {
            tbody.innerHTML = '<tr><td colspan="7" class="text-center p-8 text-gray-400">Журнал документов пуст</td></tr>';
            return;
        }

        docs.forEach(doc => {
            const tr = document.createElement('tr');

            // Иконка в зависимости от типа
            let icon = '<i class="fa-solid fa-file text-gray-400"></i>';
            if (doc.type === 'Первичный ввод') icon = '<i class="fa-solid fa-file-import text-green-600"></i>';
            else if (doc.type === 'Отправка') icon = '<i class="fa-solid fa-truck-arrow-right text-orange-600"></i>';
            else if (doc.type === 'Списание') icon = '<i class="fa-solid fa-ban text-red-600"></i>';

            tr.innerHTML = `
                <td class="text-center text-lg">${icon}</td>
                <td>${doc.date}</td>
                <td class="font-bold text-blue-900">${doc.doc_number}</td>
                <td>${getTypeBadge(doc.type)}</td>
                <td>${doc.source}</td>
                <td>${doc.target}</td>
                <td class="text-center">
                    <button class="delete-btn text-gray-400 hover:text-red-600 transition p-1" title="Удалить документ" data-id="${doc.id}">
                        <i class="fa-solid fa-trash"></i>
                    </button>
                </td>
            `;
            tbody.appendChild(tr);
        });

        // Навешиваем события на кнопки удаления
        document.querySelectorAll('.delete-btn').forEach(btn => {
            btn.addEventListener('click', function() {
                deleteDocument(this.dataset.id);
            });
        });

    } catch (e) {
        console.error(e);
        tbody.innerHTML = `<tr><td colspan="7" class="text-center text-red-500 p-4">Не удалось загрузить документы. Проверьте соединение.</td></tr>`;
    }
}

// 2. Создание документа
async function createDocument() {
    const btn = document.getElementById('btnSaveDoc');
    const originalText = btn.innerHTML;

    // Валидация
    const docNumber = document.getElementById('newDocNumber').value;
    if(!docNumber) { alert('Введите номер документа'); return; }

    btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Сохранение...';
    btn.disabled = true;

    // Сбор данных
    const docData = {
        doc_number: docNumber,
        operation_date: document.getElementById('newDocDate').value,
        operation_type: document.getElementById('newDocType').value,
        source_id: null, // Упрощение
        target_id: document.getElementById('newDocTarget').value || null,
        items: []
    };

    // Сбор строк спецификации
    const rows = document.querySelectorAll('#docItemsTable tbody tr');
    rows.forEach(row => {
        const nomId = row.querySelector('.nom-select').value;
        const serial = row.querySelector('.serial-input').value;
        const qty = row.querySelector('.qty-input').value;

        if(nomId) {
            docData.items.push({
                nomenclature_id: parseInt(nomId),
                serial_number: serial,
                quantity: parseInt(qty) || 1
            });
        }
    });

    if (docData.items.length === 0) {
        alert('Добавьте хотя бы одно изделие в спецификацию');
        btn.innerHTML = originalText;
        btn.disabled = false;
        return;
    }

    try {
        const res = await fetch('/api/arsenal/documents', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(docData)
        });

        if(res.ok) {
            closeModal('newDocModal');
            loadDocuments(); // Обновляем таблицу
            // Очистка формы
            document.getElementById('newDocNumber').value = '';
            document.querySelector('#docItemsTable tbody').innerHTML = '';
        } else {
            const err = await res.json();
            alert('Ошибка: ' + (err.detail || 'Неизвестная ошибка'));
        }
    } catch (e) {
        alert('Ошибка сети: ' + e.message);
    } finally {
        btn.innerHTML = originalText;
        btn.disabled = false;
    }
}

// 3. Удаление документа
async function deleteDocument(id) {
    if(!confirm('Вы уверены, что хотите безвозвратно удалить этот документ и все связанные записи?')) return;

    try {
        const res = await fetch(`/api/arsenal/documents/${id}`, { method: 'DELETE' });
        if(res.ok) {
            loadDocuments();
        } else {
            alert('Ошибка при удалении');
        }
    } catch(e) {
        alert('Ошибка сети');
    }
}

// --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

async function loadObjectsTree() {
    const container = document.getElementById('orgTree');
    const select = document.getElementById('newDocTarget');

    try {
        const res = await fetch('/api/arsenal/objects');
        if (!res.ok) throw new Error(`Ошибка загрузки объектов: ${res.status}`);

        objects = await res.json();

        if (objects.length === 0) {
            container.innerHTML = '<div class="p-4 text-sm text-gray-500">Нет объектов учета</div>';
            select.innerHTML = '<option value="">Нет объектов</option>';
            return;
        }

        // Рендер дерева
        container.innerHTML = objects.map(obj => `
            <div class="tree-node pl-4 transition" onclick="alert('Объект: ${obj.name}')">
                <i class="fa-solid fa-folder text-yellow-500"></i> <span class="text-gray-700">${obj.name}</span>
            </div>
        `).join('');

        // Заполнение селекта в модалке
        select.innerHTML = objects.map(o => `<option value="${o.id}">${o.name}</option>`).join('');

    } catch(e) {
        console.error(e);
        container.innerHTML = '<div class="text-red-500 p-2 text-sm">Ошибка загрузки объектов.</div>';
    }
}

async function loadNomenclature() {
    try {
        const res = await fetch('/api/arsenal/nomenclature');
        if (!res.ok) throw new Error('Ошибка загрузки номенклатуры');
        nomenclatures = await res.json();
    } catch(e) {
        console.error("Ошибка загрузки номенклатуры", e);
    }
}

function getTypeBadge(type) {
    const map = {
        'Первичный ввод': 'bg-green-100 text-green-800 border-green-200',
        'Отправка': 'bg-orange-100 text-orange-800 border-orange-200',
        'Списание': 'bg-red-100 text-red-800 border-red-200',
        'Прием': 'bg-blue-100 text-blue-800 border-blue-200'
    };
    const cls = map[type] || 'bg-gray-100 text-gray-800 border-gray-200';
    return `<span class="px-2 py-0.5 rounded text-xs font-bold border ${cls}">${type}</span>`;
}

// --- Управление модальным окном и таблицей строк ---

function openNewDocModal() {
    // Если номенклатура пустая, попробуем загрузить еще раз
    if (nomenclatures.length === 0) loadNomenclature();

    // Очищаем таблицу строк
    document.querySelector('#docItemsTable tbody').innerHTML = '';
    addDocRow(); // Добавляем одну пустую строку

    document.getElementById('newDocModal').style.display = 'flex';
}

function closeModal(id) {
    document.getElementById(id).style.display = 'none';
}

function addDocRow() {
    const tbody = document.querySelector('#docItemsTable tbody');
    const tr = document.createElement('tr');
    tr.className = "hover:bg-blue-50 transition";

    // Если номенклатура не загрузилась, показываем заглушку
    let options = '<option value="">Нет данных</option>';
    if (nomenclatures.length > 0) {
        options = '<option value="">-- Выберите изделие --</option>' +
                  nomenclatures.map(n => `<option value="${n.id}">${n.name} (${n.code || '-'})</option>`).join('');
    }

    tr.innerHTML = `
        <td class="p-1 border-b pl-2">
            <select class="nom-select w-full bg-white border border-gray-300 rounded p-1 text-sm focus:border-blue-500 outline-none">
                ${options}
            </select>
        </td>
        <td class="p-1 border-b">
            <input type="text" class="serial-input w-full bg-white border border-gray-300 rounded p-1 text-sm focus:border-blue-500 outline-none" placeholder="№">
        </td>
        <td class="p-1 border-b">
            <input type="number" class="qty-input w-full bg-white border border-gray-300 rounded p-1 text-sm text-center focus:border-blue-500 outline-none" value="1" min="1">
        </td>
        <td class="p-1 border-b text-center">
            <button type="button" class="remove-row-btn text-gray-400 hover:text-red-500 transition px-2">
                <i class="fa-solid fa-times"></i>
            </button>
        </td>
    `;

    // Навешиваем событие удаления на кнопку
    tr.querySelector('.remove-row-btn').addEventListener('click', function() {
        tr.remove();
    });

    tbody.appendChild(tr);
}

function logout() {
    window.location.href = 'arsenal_login.html';
}