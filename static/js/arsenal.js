/**
 * СТРОБ.Арсенал - Клиентская логика
 * Версия: Финальная, объединенная (с поддержкой партионного и серийного учета)
 */

// Глобальные переменные для хранения справочников
let nomenclatures = [];
let objects = [];

/**
 * Централизованная функция для выполнения запросов к API с автоматическим
 * добавлением токена аутентификации и обработкой ошибки 401.
 * @param {string} url - URL эндпоинта API
 * @param {object} options - Опции для fetch (method, body, etc.)
 * @returns {Promise<Response|null>} - Возвращает объект Response или null в случае ошибки 401
 */
async function apiFetch(url, options = {}) {
    const token = localStorage.getItem('arsenal_token');
    const defaultHeaders = {
        'Content-Type': 'application/json'
    };

    if (token) {
        defaultHeaders['Authorization'] = 'Bearer ' + token;
    }

    options.headers = { ...defaultHeaders, ...options.headers };

    try {
        const response = await fetch(url, options);

        if (response.status === 401) {
            // Если токен недействителен, очищаем его и перенаправляем на страницу входа
            localStorage.removeItem('arsenal_token');
            window.location.href = 'arsenal_login.html';
            return null; // Прерываем дальнейшее выполнение
        }

        return response;
    } catch (error) {
        console.error("Сетевая ошибка или ошибка fetch:", error);
        // Можно показать пользователю уведомление о проблемах с сетью
        return null;
    }
}


// Основная функция, запускаемая после загрузки страницы
document.addEventListener('DOMContentLoaded', async () => {
    // 1. Динамически добавляем недостающие элементы интерфейса для надежности
    injectSourceSelectIfNeeded();

    // 2. Привязываем все обработчики событий к элементам
    bindEvents();

    // 3. Устанавливаем текущую дату в поле даты документа
    const dateInput = document.getElementById('newDocDate');
    if (dateInput) dateInput.valueAsDate = new Date();

    // 4. Параллельно загружаем основные справочники (номенклатура и объекты)
    await Promise.all([
        loadNomenclature(),
        loadObjectsTree()
    ]);

    // 5. Настраиваем начальное состояние формы и загружаем журнал документов
    updateFormState();
    loadDocuments();
});

/**
 * Привязка всех обработчиков событий в одном месте.
 */
function bindEvents() {
    // Навигация по главному меню
    document.getElementById('menuDocs')?.addEventListener('click', loadDocuments);
    document.getElementById('menuObjects')?.addEventListener('click', loadObjectsTree);
    document.getElementById('menuNomenclature')?.addEventListener('click', openNomenclatureModal);

    // Кнопки для открытия модальных окон
    document.getElementById('btnAddObject')?.addEventListener('click', () => openModal('newObjectModal'));
    document.getElementById('btnOpenCreateModal')?.addEventListener('click', openNewDocModal);

    // Кнопки для закрытия всех модальных окон по крестику
    document.querySelectorAll('.modal-close-btn').forEach(button => {
        button.addEventListener('click', () => closeModal(button.closest('.modal').id));
    });

    // Улучшенное закрытие модальных окон (клик вне окна, Escape)
    document.querySelectorAll('.modal').forEach(modal => {
        modal.addEventListener('click', event => {
            if (event.target === modal) {
                closeModal(modal.id);
            }
        });
    });
    document.addEventListener('keydown', event => {
        if (event.key === 'Escape') {
            document.querySelectorAll('.modal').forEach(modal => closeModal(modal.id));
        }
    });

    // Изменение типа документа в форме создания
    document.getElementById('newDocType')?.addEventListener('change', updateFormState);

    // Действия на формах
    document.getElementById('btnRefreshDocs')?.addEventListener('click', loadDocuments);
    document.getElementById('btnSaveDoc')?.addEventListener('click', createDocument);
    document.getElementById('btnAddRow')?.addEventListener('click', addDocRow);
    document.getElementById('btnSaveObject')?.addEventListener('click', createObject);
    document.getElementById('btnSaveNom')?.addEventListener('click', createNomenclature);

    // Выход из системы
    document.getElementById('logoutBtn')?.addEventListener('click', () => {
        localStorage.removeItem('arsenal_token');
        window.location.href = 'arsenal_login.html';
    });
}

/**
 * Проверяет наличие поля "Отправитель". Если его нет в HTML, создает его
 * динамически для обеспечения работы логики перемещений.
 */
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


/**
 * Управляет видимостью полей "Отправитель" и "Получатель"
 * в зависимости от выбранного типа документа.
 */
function updateFormState() {
    const type = document.getElementById('newDocType').value;
    const sourceContainer = document.getElementById('sourceSelectContainer');
    const targetContainer = document.getElementById('targetSelectContainer');

    if (!sourceContainer || !targetContainer) return;

    // По умолчанию показываем оба поля
    sourceContainer.style.display = 'grid';
    targetContainer.style.display = 'grid';

    if (type === 'Первичный ввод') {
        // Скрываем отправителя, так как имущество появляется извне
        sourceContainer.style.display = 'none';
        document.getElementById('newDocSource').value = "";
    } else if (type === 'Списание') {
        // Скрываем получателя, так как имущество списывается в никуда
        targetContainer.style.display = 'none';
        document.getElementById('newDocTarget').value = "";
    }
}

// ==========================================
// 1. ДОКУМЕНТЫ
// ==========================================

/**
 * Загружает и отображает список документов с сервера.
 */
async function loadDocuments() {
    const tableBody = document.getElementById('docsTableBody');
    const counter = document.getElementById('docsCount');
    tableBody.innerHTML = '<tr><td colspan="7" class="text-center p-8"><i class="fa-solid fa-spinner fa-spin text-blue-600"></i> Загрузка журнала...</td></tr>';

    try {
        const response = await apiFetch('/api/arsenal/documents');
        if (!response || !response.ok) throw new Error('Ошибка сети при загрузке документов');
        const documents = await response.json();

        counter.innerText = `Всего документов: ${documents.length}`;
        tableBody.innerHTML = '';

        if (documents.length === 0) {
            tableBody.innerHTML = '<tr><td colspan="7" class="text-center p-8 text-gray-400">Журнал пуст. Создайте первый документ.</td></tr>';
            return;
        }

        documents.forEach(doc => {
            const tableRow = document.createElement('tr');
            tableRow.className = "cursor-pointer hover:bg-blue-50 transition border-b";

            tableRow.onclick = (event) => {
                if (!event.target.closest('.delete-btn')) {
                    openViewDocModal(doc.id);
                }
            };

            let icon = '<i class="fa-solid fa-file text-gray-400"></i>';
            if (document.type === 'Первичный ввод') icon = '<i class="fa-solid fa-file-import text-green-600"></i>';
            else if (['Отправка', 'Перемещение', 'Прием'].includes(document.type)) icon = '<i class="fa-solid fa-truck-arrow-right text-orange-600"></i>';
            else if (document.type === 'Списание') icon = '<i class="fa-solid fa-ban text-red-600"></i>';

            tableRow.innerHTML = `
                <td class="text-center text-lg py-3">${icon}</td>
                <td class="text-sm">${doc.date}</td>
                <td class="font-bold text-blue-900 text-sm">${document.doc_number}</td>
                <td>${getTypeBadge(doc.type)}</td>
                <td class="text-sm text-gray-600">${doc.source || '---'}</td>
                <td class="text-sm text-gray-600">${doc.target || '---'}</td>
                <td class="text-center">
                    <button class="delete-btn text-gray-400 hover:text-red-600 p-2 rounded transition" data-id="${doc.id}" title="Удалить документ">
                        <i class="fa-solid fa-trash"></i>
                    </button>
                </td>
            `;
            tableBody.appendChild(tableRow);
        });

        // Привязываем события удаления к новым кнопкам
        document.querySelectorAll('.delete-btn').forEach(button => {
            button.addEventListener('click', function (event) {
                event.stopPropagation(); // Предотвращаем всплытие события, чтобы не открылось модальное окно
                deleteDocument(this.dataset.id);
            });
        });
    } catch (error) {
        console.error(error);
        tableBody.innerHTML = '<tr><td colspan="7" class="text-center text-red-500 p-4">Ошибка загрузки данных. Проверьте подключение к серверу.</td></tr>';
    }
}

/**
 * Собирает данные из формы и отправляет на сервер для создания нового документа.
 */
async function createDocument() {
    const button = document.getElementById('btnSaveDoc');
    const docNumber = document.getElementById('newDocNumber').value;
    const docType = document.getElementById('newDocType').value;
    const sourceId = document.getElementById('newDocSource')?.value;
    const targetId = document.getElementById('newDocTarget')?.value;

    // --- Валидация формы ---
    if (!docNumber) return alert('Введите номер документа.');
    if (docType === 'Первичный ввод' && !targetId) return alert('Укажите получателя.');
    if (docType === 'Списание' && !sourceId) return alert('Укажите источник списания.');
    if (['Перемещение', 'Отправка', 'Прием'].includes(docType) && (!sourceId || !targetId)) {
        return alert('Укажите и отправителя, и получателя.');
    }

    // --- Сбор позиций документа ---
    const items = [];
    let validationPassed = true;
    document.querySelectorAll('#docItemsTable tbody tr').forEach(row => {
        const nomenclatureId = row.querySelector('.nom-select').value;
        const serial = row.querySelector('.serial-input').value;
        const quantity = row.querySelector('.qty-input').value;

        if (nomenclatureId && !serial) {
            validationPassed = false; // Если выбрана номенклатура, но нет номера/партии
        }

        if (nomenclatureId && serial) {
            items.push({
                nomenclature_id: parseInt(nomenclatureId),
                serial_number: serial,
                quantity: parseInt(quantity) || 1
            });
        }
    });

    if (!validationPassed) return alert('Для каждого выбранного изделия необходимо указать Серийный номер или Номер партии.');
    if (items.length === 0) return alert('Добавьте хотя бы одно изделие в спецификацию.');

    // Блокировка кнопки на время запроса
    button.disabled = true;
    const originalText = button.innerHTML;
    button.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Сохранение...';

    const documentData = {
        doc_number: docNumber,
        operation_date: document.getElementById('newDocDate').value,
        operation_type: docType,
        source_id: sourceId ? parseInt(sourceId) : null,
        target_id: targetId ? parseInt(targetId) : null,
        items: items
    };

    try {
        const response = await apiFetch('/api/arsenal/documents', {
            method: 'POST',
            body: JSON.stringify(documentData)
        });

        if (response && response.ok) {
            closeModal('newDocModal');
            loadDocuments(); // Обновляем журнал
        } else {
            const error = await response.json();
            alert('Ошибка создания документа: ' + (error.detail || 'Неизвестная ошибка сервера.'));
        }
    } catch (error) {
        alert('Сетевая ошибка. Не удалось отправить данные.');
        console.error(error);
    } finally {
        // Возвращаем кнопку в исходное состояние
        button.disabled = false;
        button.innerHTML = originalText;
    }
}

/**
 * Отправляет запрос на удаление документа по его ID.
 */
async function deleteDocument(id) {
    if (!confirm('Вы уверены, что хотите удалить этот документ? Это действие необратимо.')) return;
    try {
        const response = await apiFetch(`/api/arsenal/documents/${id}`, { method: 'DELETE' });
        if (response && response.ok) {
            loadDocuments(); // Обновляем журнал после удаления
        } else {
            const error = await response.json();
            alert('Не удалось удалить документ: ' + (error.detail || 'Возможно, он связан с другими операциями.'));
        }
    } catch (error) {
        alert('Сетевая ошибка при удалении.');
        console.error(error);
    }
}

/**
 * Открывает модальное окно с детальной информацией о документе.
 */
async function openViewDocModal(id) {
    const tableBody = document.getElementById('viewDocItems');
    tableBody.innerHTML = '<tr><td colspan="3" class="text-center p-4"><i class="fa-solid fa-spinner fa-spin"></i> Загрузка деталей...</td></tr>';
    openModal('viewDocModal');

    try {
        const response = await apiFetch(`/api/arsenal/documents/${id}`);
        if (!response || !response.ok) throw new Error('Document not found');
        const documentData = await response.json();

        // Заполнение шапки
        document.getElementById('viewDocNumber').innerText = documentData.doc_number;
        document.getElementById('viewDocDate').innerText = new Date(documentData.operation_date).toLocaleDateString();
        document.getElementById('viewDocType').innerText = documentData.operation_type;
        document.getElementById('viewDocSource').innerText = documentData.source ? documentData.source.name : '---';
        document.getElementById('viewDocTarget').innerText = documentData.target ? documentData.target.name : '---';

        // Заполнение таблицы позиций
        tableBody.innerHTML = '';
        if (documentData.items.length === 0) {
            tableBody.innerHTML = '<tr><td colspan="3" class="text-center p-4 text-gray-500">В документе нет позиций.</td></tr>';
            return;
        }
        documentData.items.forEach(item => {
            tableBody.innerHTML += `
                <tr class="border-b last:border-0">
                    <td class="p-2">
                        <div class="font-bold text-gray-800">${item.nomenclature.name}</div>
                        <div class="text-xs text-gray-500 font-mono">${item.nomenclature.code || ''}</div>
                    </td>
                    <td class="p-2 font-mono text-blue-700">${item.serial_number || '-'}</td>
                    <td class="p-2 text-center font-bold">${item.quantity}</td>
                </tr>
            `;
        });
    } catch (error) {
        tableBody.innerHTML = '<tr><td colspan="3" class="text-red-500 text-center p-4">Ошибка загрузки деталей документа.</td></tr>';
        console.error(error);
    }
}


// ==========================================
// 2. ОБЪЕКТЫ УЧЕТА
// ==========================================

/**
 * Загружает дерево объектов и обновляет связанные выпадающие списки.
 */
async function loadObjectsTree() {
    const container = document.getElementById('orgTree');
    const targetSelect = document.getElementById('newDocTarget');
    const sourceSelect = document.getElementById('newDocSource');

    try {
        const response = await apiFetch('/api/arsenal/objects');
        if (!response || !response.ok) return;

        objects = await response.json();

        if (objects.length === 0) {
            container.innerHTML = '<div class="p-4 text-sm text-gray-500">Нет объектов. Нажмите "+", чтобы добавить.</div>';
            const emptyOption = '<option value="">Нет объектов для выбора</option>';
            if (targetSelect) targetSelect.innerHTML = emptyOption;
            if (sourceSelect) sourceSelect.innerHTML = emptyOption;
            return;
        }

        // Рендеринг дерева объектов
        container.innerHTML = objects.map(object => `
            <div class="tree-node pl-4 transition hover:bg-blue-50 flex justify-between items-center group">
                <div class="flex-grow cursor-pointer py-1" onclick="showBalanceModal(${object.id}, '${object.name}')">
                    <i class="fa-solid fa-layer-group text-blue-500 mr-2"></i>
                    <span class="text-gray-700 ml-1 font-medium text-sm">${object.name}</span>
                    <span class="text-xs text-gray-400 ml-2">(${object.obj_type})</span>
                </div>
                <button onclick="showBalanceModal(${object.id}, '${object.name}')" class="text-gray-300 hover:text-green-600 px-2 py-1 text-xs opacity-0 group-hover:opacity-100 transition" title="Показать остатки">
                    <i class="fa-solid fa-box-archive"></i>
                </button>
            </div>
        `).join('');

        // Обновление выпадающих списков в форме создания документа
        const optionsHtml = '<option value="">-- Выберите объект --</option>' + objects.map(o => `<option value="${o.id}">${o.name}</option>`).join('');
        if (targetSelect) targetSelect.innerHTML = optionsHtml;
        if (sourceSelect) sourceSelect.innerHTML = optionsHtml;
    } catch (error) {
        console.error(error);
    }
}

/**
 * Создает новый объект учета.
 */
async function createObject() {
    const name = document.getElementById('newObjName').value;
    const type = document.getElementById('newObjType').value;
    if (!name) return alert("Введите название объекта.");

    try {
        const response = await apiFetch('/api/arsenal/objects', {
            method: 'POST',
            body: JSON.stringify({ name: name, obj_type: type })
        });
        if (response && response.ok) {
            closeModal('newObjectModal');
            document.getElementById('newObjName').value = ''; // Очистка поля
            loadObjectsTree(); // Перезагрузка дерева и списков
        } else {
            const error = await response.json();
            alert("Ошибка создания: " + (error.detail || "Не удалось создать объект."));
        }
    } catch (error) {
        console.error(error);
    }
}


// ==========================================
// 3. НОМЕНКЛАТУРА
// ==========================================

/**
 * Загружает справочник номенклатуры с сервера.
 */
async function loadNomenclature() {
    try {
        const response = await apiFetch('/api/arsenal/nomenclature');
        if (!response || !response.ok) return;
        nomenclatures = await response.json();
        renderNomenclatureList(); // Обновляем список в модальном окне
    } catch (error) {
        console.error(error);
    }
}

/**
 * Отображает список номенклатуры в модальном окне.
 */
function renderNomenclatureList() {
    const tableBody = document.getElementById('nomenclatureListBody');
    if (!tableBody) return;
    if (nomenclatures.length === 0) {
        tableBody.innerHTML = '<tr><td colspan="2" class="p-4 text-center text-gray-400">Справочник пуст.</td></tr>';
        return;
    }
    tableBody.innerHTML = nomenclatures.map(n => `
        <tr class="hover:bg-gray-100 border-b last:border-0">
            <td class="p-2 border-r font-mono text-xs text-blue-600">${n.code || '-'}</td>
            <td class="p-2 font-bold text-gray-700 text-sm">${n.name}</td>
        </tr>
    `).join('');
}

/**
 * Открывает модальное окно для работы с номенклатурой.
 */
function openNomenclatureModal() {
    // Очистка формы перед открытием
    document.getElementById('newNomCode').value = '';
    document.getElementById('newNomName').value = '';
    document.getElementById('newNomCat').value = '';
    document.getElementById('newNomIsNumbered').checked = true;
    openModal('nomenclatureModal');
}

/**
 * Создает новую позицию номенклатуры.
 */
async function createNomenclature() {
    const code = document.getElementById('newNomCode').value;
    const name = document.getElementById('newNomName').value;
    const category = document.getElementById('newNomCat').value;
    const isNumbered = document.getElementById('newNomIsNumbered').checked;
    if (!name) return alert("Наименование является обязательным полем.");

    try {
        const response = await apiFetch('/api/arsenal/nomenclature', {
            method: 'POST',
            body: JSON.stringify({ code, name, category: category, is_numbered: isNumbered })
        });
        if (response && response.ok) {
            // Очистка полей в случае успеха
            document.getElementById('newNomName').value = '';
            document.getElementById('newNomCode').value = '';
            await loadNomenclature(); // Перезагрузка списка
        } else {
            const error = await response.json();
            alert("Ошибка создания номенклатуры: " + error.detail);
        }
    } catch (error) {
        console.error(error);
    }
}

// ==========================================
// 4. ОСТАТКИ (РЕЕСТР)
// ==========================================

/**
 * Запрашивает и отображает остатки по конкретному объекту.
 */
async function showBalanceModal(objectId, objectName) {
    const title = document.getElementById('balanceModalTitle');
    const tableBody = document.getElementById('balanceTableBody');
    if (!title || !tableBody) return;

    title.innerText = `Остатки: ${objectName}`;
    tableBody.innerHTML = '<tr><td colspan="4" class="text-center p-8"><i class="fa-solid fa-spinner fa-spin text-green-600"></i> Загрузка реестра...</td></tr>';
    openModal('balanceModal');

    try {
        const response = await apiFetch(`/api/arsenal/balance/${objectId}`);
        if (!response || !response.ok) throw new Error('Ошибка сервера при загрузке остатков');
        const balanceItems = await response.json();

        if (balanceItems.length === 0) {
            tableBody.innerHTML = '<tr><td colspan="4" class="text-center p-8 text-gray-500">На данном объекте нет закрепленного имущества.</td></tr>';
            return;
        }

        tableBody.innerHTML = '';
        balanceItems.forEach(item => {
            const tableRow = document.createElement('tr');
            tableRow.innerHTML = `
                <td class="p-2 border-b font-medium text-gray-800">${item.nomenclature}</td>
                <td class="p-2 border-b font-mono text-xs text-gray-500">${item.code || '-'}</td>
                <td class="p-2 border-b font-mono text-blue-700">${item.serial_number}</td>
                <td class="p-2 border-b text-center font-bold bg-green-50">${item.quantity}</td>
            `;
            tableBody.appendChild(tableRow);
        });
    } catch (error) {
        console.error(error);
        tableBody.innerHTML = '<tr><td colspan="4" class="text-center p-8 text-red-500">Не удалось загрузить остатки.</td></tr>';
    }
}


// ==========================================
// ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
// ==========================================

/**
 * Возвращает HTML-бейдж для типа документа.
 * @param {string} type - Тип документа
 */
function getTypeBadge(type) {
    const map = {
        'Первичный ввод': 'bg-green-100 text-green-800 border-green-200',
        'Отправка': 'bg-orange-100 text-orange-800 border-orange-200',
        'Списание': 'bg-red-100 text-red-800 border-red-200',
        'Прием': 'bg-blue-100 text-blue-800 border-blue-200',
        'Перемещение': 'bg-blue-100 text-blue-800 border-blue-200'
    };
    const classes = map[type] || 'bg-gray-100 text-gray-800 border-gray-200';
    return `<span class="px-2 py-0.5 rounded text-xs font-bold border ${classes}">${type}</span>`;
}

/**
 * Подготавливает и открывает модальное окно создания нового документа.
 */
function openNewDocModal() {
    document.getElementById('newDocForm').reset();
    document.querySelector('#docItemsTable tbody').innerHTML = '';
    document.getElementById('newDocType').value = 'Первичный ввод'; // Значение по умолчанию
    updateFormState();
    addDocRow(); // Добавляем одну пустую строку для начала
    openModal('newDocModal');
}

/**
 * Открывает модальное окно по его ID.
 * @param {string} id - ID модального окна
 */
function openModal(id) {
    const modal = document.getElementById(id);
    if (modal) modal.style.display = 'flex';
}

/**
 * Закрывает модальное окно по его ID.
 * @param {string} id - ID модального окна
 */
function closeModal(id) {
    const modal = document.getElementById(id);
    if (modal) modal.style.display = 'none';
}

/**
 * Добавляет новую строку в таблицу позиций документа.
 */
function addDocRow() {
    const tableBody = document.querySelector('#docItemsTable tbody');
    const tableRow = document.createElement('tr');
    tableRow.className = 'border-b';

    let options = '<option value="">-- Выберите номенклатуру --</option>' +
        nomenclatures.map(n => `<option value="${n.id}" data-is-numbered="${n.is_numbered}">${n.name}${n.code ? ' ('+n.code+')' : ''}</option>`).join('');

    tableRow.innerHTML = `
        <td class="p-1">
            <select class="nom-select w-full border border-gray-300 p-1.5 rounded text-sm bg-white" onchange="handleNomenclatureChange(this)">
                ${options}
            </select>
        </td>
        <td class="p-1">
            <input type="text" class="serial-input w-full border border-gray-300 p-1.5 rounded text-sm" placeholder="№ / Партия">
        </td>
        <td class="p-1">
            <input type="number" class="qty-input w-full border border-gray-300 p-1.5 rounded text-sm text-center" value="1" min="1">
        </td>
        <td class="p-1 text-center">
            <button type="button" class="text-xl text-red-400 hover:text-red-600 p-1 leading-none" onclick="this.closest('tr').remove()" title="Удалить строку">&times;</button>
        </td>
    `;
    tableBody.appendChild(tableRow);
    // Сразу применяем логику к новой строке, чтобы поле количества было настроено правильно
    handleNomenclatureChange(tableRow.querySelector('.nom-select'));
}

/**
 * Блокирует/разблокирует поле "Количество" в зависимости от типа номенклатуры
 * (серийная продукция всегда имеет количество 1).
 * @param {HTMLSelectElement} selectElement - Элемент select, который был изменен
 */
function handleNomenclatureChange(selectElement) {
    const selectedOption = selectElement.options[selectElement.selectedIndex];
    const isNumbered = selectedOption.dataset.isNumbered === 'true';
    const row = selectElement.closest('tr');
    const quantityInput = row.querySelector('.qty-input');
    const serialInput = row.querySelector('.serial-input');

    if (isNumbered) {
        quantityInput.value = 1;
        quantityInput.readOnly = true;
        quantityInput.classList.add('bg-gray-100');
        serialInput.placeholder = "Серийный номер";
    } else {
        quantityInput.readOnly = false;
        quantityInput.classList.remove('bg-gray-100');
        serialInput.placeholder = "Номер партии";
    }
}