/**
 * МОДУЛЬ: DATA & REFERENCE
 * Справочники (Объекты, Номенклатура), Пользователи, Баланс и Отчеты.
 * ОПТИМИЗАЦИЯ: Баланс переведен на серверную пагинацию и поиск.
 */

const Dictionaries = {
    // Состояние формы номенклатуры
    nomState: { mode: 'create', currentId: null },

    // Загрузка дерева объектов (Склады/Подразделения)
    loadObjectsTree: async () => {
        const container = document.getElementById('orgTree');
        try {
            const response = await apiFetch('/api/arsenal/objects');
            if (!response || !response.ok) return;
            AppState.objects = await response.json();

            if (AppState.objects.length === 0) {
                container.innerHTML = '<div class="p-4 text-sm text-slate-400 text-center">Нет объектов.</div>';
                return;
            }

            const isAdmin = AppState.userRole === 'admin';

            // Рендер дерева
            container.innerHTML = AppState.objects.map(o => {
                const safeName = o.name.replace(/'/g, "\\'").replace(/"/g, '&quot;');
                const deleteBtn = isAdmin
                    ? `<button onclick="event.stopPropagation(); Dictionaries.deleteObject(${o.id}, '${safeName}')"
                           class="text-slate-300 hover:text-rose-600 px-1.5 opacity-0 group-hover:opacity-100 transition"
                           title="Удалить объект">
                           <i class="fa-solid fa-trash text-sm"></i>
                       </button>`
                    : '';
                return `
                <div class="tree-node pl-3 transition hover:bg-blue-50 flex justify-between items-center group py-1.5">
                    <div class="flex-grow cursor-pointer flex flex-col" onclick="Balance.initModal(${o.id}, '${safeName}')">
                        <div class="flex items-center">
                            <i class="fa-solid ${o.obj_type === 'Склад' ? 'fa-box text-blue-500' : 'fa-layer-group text-slate-400'} mr-2 w-5 text-center"></i>
                            <span class="text-slate-700 font-medium text-sm">${o.name}</span>
                        </div>
                        ${o.mol_name ? `<div class="text-[10px] text-slate-400 ml-7 mt-0.5"><i class="fa-solid fa-user-tag text-slate-300 mr-1"></i> ${o.mol_name}</div>` : ''}
                    </div>
                    <div class="flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition">
                        <button onclick="Balance.initModal(${o.id}, '${safeName}')" class="text-slate-300 hover:text-emerald-600 px-1.5 transition" title="Открыть остатки">
                            <i class="fa-solid fa-box-archive text-lg"></i>
                        </button>
                        ${deleteBtn}
                    </div>
                </div>`;
            }).join('');

            // Обновление выпадающих списков в формах
            const optionsHtml = '<option value="">-- Выберите объект --</option>' +
                AppState.objects.map(o => `<option value="${o.id}">${o.name}</option>`).join('');

            const targetSelect = document.getElementById('newDocTarget');
            const sourceSelect = document.getElementById('newDocSource');

            if (targetSelect) targetSelect.innerHTML = optionsHtml;
            if (sourceSelect) sourceSelect.innerHTML = optionsHtml;

        } catch (e) {
            console.error("Ошибка загрузки объектов:", e);
        }
    },

    // Открыть модалку управления объектами
    openObjectsModal: async () => {
        UI.openModal('objectsModal');
        await Objects.loadAndShow();
    },

    // Создание нового объекта
    createObject: async () => {
        const nameInput = document.getElementById('newObjName');
        const typeInput = document.getElementById('newObjType');
        const molInput = document.getElementById('newObjMol');

        const name = nameInput.value.trim();
        const type = typeInput.value;
        const mol = molInput.value.trim();

        if (!name) return UI.showToast("Введите название объекта.", "error");

        try {
            const res = await apiFetch('/api/arsenal/objects', {
                method: 'POST',
                body: JSON.stringify({ name, obj_type: type, mol_name: mol || null })
            });

            if (res && res.ok) {
                const data = await res.json();
                UI.closeModal('newObjectModal');

                // Очистка полей
                nameInput.value = '';
                molInput.value = '';

                await Dictionaries.loadObjectsTree();

                // Обновляем модалку Objects если она открыта
                const objModal = document.getElementById('objectsModal');
                if (objModal && objModal.style.display !== 'none') {
                    await Objects.loadAndShow();
                }

                if (data.credentials) {
                    UI.showCredentialsModal(`Объект "${data.name}" успешно создан!`, data.credentials.username, data.credentials.password);
                } else {
                    UI.showToast("Объект успешно создан!", "success");
                }
            } else {
                const err = await res.json();
                UI.showToast("Ошибка: " + (err.detail || 'Не удалось создать объект'), "error");
            }
        } catch (e) { console.error(e); }
    },

    // Удаление объекта (только для администратора)
    deleteObject: async (id, name) => {
        if (!confirm(`Удалить объект "${name}"?\nВнимание: удаление возможно только если на объекте нет остатков и нет привязанных документов.`)) return;
        try {
            const res = await apiFetch(`/api/arsenal/objects/${id}`, { method: 'DELETE' });
            if (res && res.ok) {
                UI.showToast(`Объект "${name}" удалён.`, "success");
                await Dictionaries.loadObjectsTree();
                await Dashboard.loadKPIs();
            } else {
                const err = await res.json();
                UI.showToast("Ошибка: " + (err.detail || 'Не удалось удалить объект'), "error");
            }
        } catch (e) { UI.showToast("Сетевая ошибка", "error"); }
    },

    // Загрузка номенклатуры (С поддержкой поиска и пагинации)
    loadNomenclature: async (searchQuery = '') => {
        try {
            const url = searchQuery
                ? `/api/arsenal/nomenclature?limit=100&q=${encodeURIComponent(searchQuery)}`
                : `/api/arsenal/nomenclature?limit=100`;

            const res = await apiFetch(url);
            if (!res || !res.ok) return;
            AppState.nomenclatures = await res.json();

            const tb = document.getElementById('nomenclatureListBody');
            if (!tb) return;

            if (AppState.nomenclatures.length === 0) {
                tb.innerHTML = '<tr><td colspan="4" class="p-8 text-center text-slate-400">Справочник пуст или ничего не найдено.</td></tr>';
            } else {
                tb.innerHTML = AppState.nomenclatures.map(n => {
                    // Экранируем кавычки для безопасной передачи в функцию OnClick
                    const safeName = n.name.replace(/'/g, "\\'").replace(/"/g, '&quot;');
                    const safeCode = (n.code || '').replace(/'/g, "\\'");
                    const safeAcc = (n.default_account || '').replace(/'/g, "\\'");

                    const badge = n.is_numbered
                        ? '<span class="px-2 py-0.5 bg-blue-50 text-blue-600 rounded text-[10px] font-bold border border-blue-100">Номерной</span>'
                        : '<span class="px-2 py-0.5 bg-amber-50 text-amber-600 rounded text-[10px] font-bold border border-amber-100">Партия</span>';

                    return `
                    <tr class="hover:bg-emerald-50 cursor-pointer transition border-b border-slate-100 last:border-0" 
                        onclick="Dictionaries.startEditNomenclature(${n.id}, '${safeName}', '${safeCode}', '${safeAcc}', ${n.is_numbered})">
                        <td class="p-3 font-mono text-xs text-emerald-600 font-bold">${n.code || '-'}</td>
                        <td class="p-3 text-slate-700 text-sm font-medium">${n.name}</td>
                        <td class="p-3 text-center">${badge}</td>
                        <td class="p-3 text-xs font-mono text-slate-500">${n.default_account || '-'}</td>
                    </tr>`;
                }).join('');
            }
        } catch (e) { console.error(e); }
    },

    openNomenclatureModal: () => {
        Dictionaries.resetNomenclatureForm();
        UI.openModal('nomenclatureModal');
        Dictionaries.loadNomenclature();

        // Привязываем поиск
        const searchInput = document.getElementById('nomSearchInput');
        if(searchInput) {
            // Удаляем старые слушатели, чтобы не плодить их при повторном открытии
            const newSearch = searchInput.cloneNode(true);
            searchInput.parentNode.replaceChild(newSearch, searchInput);

            // Поиск по нажатию Enter
            newSearch.addEventListener('keyup', (e) => {
                if (e.key === 'Enter') {
                    Dictionaries.loadNomenclature(e.target.value.trim());
                }
            });
        }
    },

    // Сброс формы в режим создания (Добавление нового)
    resetNomenclatureForm: () => {
        Dictionaries.nomState = { mode: 'create', currentId: null };
        document.getElementById('editNomId').value = '';
        document.getElementById('newNomCode').value = '';
        document.getElementById('newNomName').value = '';
        document.getElementById('newNomAccount').value = '';
        document.getElementById('newNomIsNumbered').checked = true;

        document.getElementById('nomFormTitle').innerHTML = '<i class="fa-solid fa-plus text-emerald-600"></i> Добавить новое изделие';
        document.getElementById('btnSaveNom').style.display = 'block';
        document.getElementById('nomEditActions').classList.add('hidden');
        document.getElementById('nomEditActions').classList.remove('flex');
    },

    // Активация режима редактирования (При клике на строку таблицы)
    startEditNomenclature: (id, name, code, account, isNumbered) => {
        Dictionaries.nomState = { mode: 'edit', currentId: id };
        document.getElementById('editNomId').value = id;
        document.getElementById('newNomName').value = name;
        document.getElementById('newNomCode').value = code;
        document.getElementById('newNomAccount').value = account;
        document.getElementById('newNomIsNumbered').checked = isNumbered;

        document.getElementById('nomFormTitle').innerHTML = '<i class="fa-solid fa-pen text-blue-600"></i> Редактирование изделия';
        document.getElementById('btnSaveNom').style.display = 'none';
        document.getElementById('nomEditActions').classList.remove('hidden');
        document.getElementById('nomEditActions').classList.add('flex');
    },

    // Создание новой номенклатуры
    createNomenclature: async () => {
        const code = document.getElementById('newNomCode').value.trim();
        const name = document.getElementById('newNomName').value.trim();
        const account = document.getElementById('newNomAccount').value.trim();
        const isNum = document.getElementById('newNomIsNumbered').checked;

        if (!name) return UI.showToast("Наименование обязательно.", "error");

        try {
            const res = await apiFetch('/api/arsenal/nomenclature', {
                method: 'POST',
                body: JSON.stringify({ code, name, is_numbered: isNum, default_account: account || null })
            });

            if (res && res.ok) {
                Dictionaries.resetNomenclatureForm();
                await Dictionaries.loadNomenclature();
                UI.showToast("Изделие добавлено в каталог!", "success");
            } else {
                const err = await res.json();
                UI.showToast("Ошибка: " + err.detail, "error");
            }
        } catch (e) { console.error(e); }
    },

    // Обновление существующей номенклатуры
    updateNomenclature: async () => {
        const id = Dictionaries.nomState.currentId;
        const code = document.getElementById('newNomCode').value.trim();
        const name = document.getElementById('newNomName').value.trim();
        const account = document.getElementById('newNomAccount').value.trim();
        const isNum = document.getElementById('newNomIsNumbered').checked;

        if (!id || !name) return;

        try {
            const res = await apiFetch(`/api/arsenal/nomenclature/${id}`, {
                method: 'PUT',
                body: JSON.stringify({ code, name, is_numbered: isNum, default_account: account || null })
            });

            if (res && res.ok) {
                Dictionaries.resetNomenclatureForm();
                await Dictionaries.loadNomenclature();
                UI.showToast("Изделие успешно обновлено!", "success");
            } else {
                const err = await res.json();
                UI.showToast("Ошибка: " + err.detail, "error");
            }
        } catch (e) { console.error(e); }
    },

    // Удаление номенклатуры
    deleteNomenclature: async () => {
        const id = Dictionaries.nomState.currentId;
        if (!id) return;

        if(!confirm("Удалить это изделие из справочника?\nВнимание: если оно числится на складе или в истории, система заблокирует удаление.")) return;

        try {
            const res = await apiFetch(`/api/arsenal/nomenclature/${id}`, { method: 'DELETE' });

            if (res && res.ok) {
                Dictionaries.resetNomenclatureForm();
                await Dictionaries.loadNomenclature();
                UI.showToast("Изделие удалено.", "success");
            } else {
                const err = await res.json();
                UI.showToast(err.detail || "Не удалось удалить", "error");
            }
        } catch (e) { UI.showToast("Сетевая ошибка", "error"); }
    }
};

const Balance = {
    // Состояние просмотра баланса
    state: {
        objectId: null,
        objectName: '',
        skip: 0,
        limit: 100, // УВЕЛИЧЕНО ДО 100
        searchQuery: '',
        hasMore: true
    },

    // Инициализация модалки (открытие)
    initModal: (objectId, objectName) => {
        Balance.state.objectId = objectId;
        Balance.state.objectName = objectName;
        Balance.state.skip = 0;
        Balance.state.searchQuery = '';
        Balance.state.hasMore = true;

        // Вставляем UI элементы управления в модалку, если их там нет
        Balance.injectControls();

        Balance.loadData();
        UI.openModal('balanceModal');
    },

    injectControls: () => {
        const title = document.getElementById('balanceModalTitle');
        const sumEl = document.getElementById('balanceTotalSum');
        // Находим родительский контейнер заголовка, чтобы вставить поиск
        const headerContainer = title.closest('.modal-content').querySelector('.px-6.py-4.border-b');

        // Проверяем, есть ли уже поиск, если нет - добавляем
        if (!document.getElementById('balanceSearchInput')) {
            const searchContainer = document.createElement('div');
            searchContainer.className = "flex items-center gap-2 ml-auto mr-4";
            searchContainer.innerHTML = `
                <div class="relative">
                    <i class="fa-solid fa-search absolute left-3 top-2.5 text-slate-400 text-xs"></i>
                    <input type="text" id="balanceSearchInput" placeholder="Поиск по названию или номеру..." 
                           class="bg-slate-700 border border-slate-600 text-white text-sm rounded-lg pl-8 pr-3 py-1.5 focus:border-blue-400 outline-none w-64 placeholder-slate-400 transition">
                </div>
            `;
            // Вставляем перед кнопкой закрытия
            headerContainer.insertBefore(searchContainer, headerContainer.lastElementChild);

            // Слушатель поиска
            document.getElementById('balanceSearchInput').addEventListener('keyup', (e) => {
                if (e.key === 'Enter') {
                    Balance.state.searchQuery = e.target.value.trim();
                    Balance.state.skip = 0;
                    Balance.loadData();
                }
            });
        } else {
            document.getElementById('balanceSearchInput').value = '';
        }
    },

    loadData: async () => {
        const title = document.getElementById('balanceModalTitle');
        const tb = document.getElementById('balanceTableBody');
        const sumEl = document.getElementById('balanceTotalSum');
        const pagDiv = document.getElementById('balancePagination');

        title.innerText = `Остатки: ${Balance.state.objectName}`;
        UI.setLoading('balanceTableBody', 'Загрузка данных...', 7);

        try {
            const qParam = Balance.state.searchQuery ? `&q=${encodeURIComponent(Balance.state.searchQuery)}` : '';
            const url = `/api/arsenal/balance/${Balance.state.objectId}?skip=${Balance.state.skip}&limit=${Balance.state.limit}${qParam}`;

            const res = await apiFetch(url);
            const items = await res.json();

            Balance.state.hasMore = items.length === Balance.state.limit;

            if (items.length === 0 && Balance.state.skip === 0) {
                tb.innerHTML = '<tr><td colspan="7" class="text-center p-8 text-slate-400">Нет данных по запросу.</td></tr>';
                sumEl.innerText = "0.00 ₽";
                Balance.renderPagination(pagDiv, 0);
                return;
            }

            let pageSum = 0;
            const rowsHtml = items.map(i => {
                const itemTotal = (i.price || 0) * i.quantity;
                pageSum += itemTotal;

                const priceFormatted = (i.price || 0).toLocaleString('ru-RU', { style: 'currency', currency: 'RUB' });
                const totalFormatted = itemTotal.toLocaleString('ru-RU', { style: 'currency', currency: 'RUB' });

                return `
                    <tr class="hover:bg-emerald-50/50 transition border-b border-slate-100 last:border-0">
                        <td class="p-3 font-medium text-slate-800">${i.nomenclature}</td>
                        <td class="p-3">
                            <div class="font-mono text-xs text-purple-700 font-bold">${i.account}</div>
                            <div class="font-mono text-[10px] text-slate-400 mt-0.5">${i.kbk}</div>
                        </td>
                        <td class="p-3 font-mono text-blue-700 text-xs">${i.serial_number || '-'}</td>
                        <td class="p-3 font-mono text-xs text-slate-500">${i.inventory_number || '-'}</td>
                        <td class="p-3 text-right text-xs text-slate-600">${priceFormatted}</td>
                        <td class="p-3 text-center"><span class="bg-emerald-100 text-emerald-800 px-2 py-0.5 rounded text-xs font-bold">${i.quantity}</span></td>
                        <td class="p-3 text-right font-bold text-slate-800 text-xs">${totalFormatted}</td>
                    </tr>`;
            }).join('');

            tb.innerHTML = rowsHtml;

            // Примечание: Это сумма только текущей страницы.
            // Для полной суммы всего склада нужен отдельный API запрос, но для быстродействия пока так.
            sumEl.innerHTML = `<span class="text-xs text-slate-400 mr-1">(на странице)</span> ${pageSum.toLocaleString('ru-RU', { style: 'currency', currency: 'RUB' })}`;

            Balance.renderPagination(pagDiv, items.length);

        } catch (e) {
            console.error(e);
            tb.innerHTML = '<tr><td colspan="7" class="text-center p-8 text-red-500">Ошибка получения остатков.</td></tr>';
        }
    },

    renderPagination: (container, count) => {
        if (!container) return; // Защита, если контейнера нет

        const start = Balance.state.skip + 1;
        const end = Balance.state.skip + count;

        container.innerHTML = `
            <div class="text-xs text-slate-500 font-bold">
                ${count > 0 ? `Строки ${start}-${end}` : 'Нет данных'}
            </div>
            <div class="flex gap-2">
                <button onclick="Balance.changePage(-1)" ${Balance.state.skip === 0 ? 'disabled' : ''} 
                    class="px-3 py-1 text-xs font-bold rounded border ${Balance.state.skip === 0 ? 'bg-slate-100 text-slate-300' : 'bg-white text-slate-600 hover:bg-slate-100'}">
                    Назад
                </button>
                <button onclick="Balance.changePage(1)" ${!Balance.state.hasMore ? 'disabled' : ''}
                    class="px-3 py-1 text-xs font-bold rounded border ${!Balance.state.hasMore ? 'bg-slate-100 text-slate-300' : 'bg-white text-slate-600 hover:bg-slate-100'}">
                    Вперед
                </button>
            </div>
        `;
    },

    changePage: (dir) => {
        if (dir === 1 && Balance.state.hasMore) {
            Balance.state.skip += Balance.state.limit;
            Balance.loadData();
        } else if (dir === -1 && Balance.state.skip > 0) {
            Balance.state.skip = Math.max(0, Balance.state.skip - Balance.state.limit);
            Balance.loadData();
        }
    }
};

const Users = {
    loadAndShow: async () => {
        UI.openModal('usersModal');
        UI.setLoading('usersTableBody', 'Загрузка списка пользователей...', 5);

        try {
            const res = await apiFetch('/api/arsenal/users');
            const users = await res.json();
            const tb = document.getElementById('usersTableBody');

            const rowsHtml = users.map(u => {
                const isAdmin = u.role === 'admin';
                const roleBadge = isAdmin
                    ? '<span class="px-2 py-1 bg-purple-100 text-purple-800 rounded-md text-xs font-bold border border-purple-200">Администратор</span>'
                    : '<span class="px-2 py-1 bg-blue-100 text-blue-800 rounded-md text-xs font-bold border border-blue-200">МОЛ / Нач.склада</span>';

                const btnReset = !isAdmin
                    ? `<button onclick="Users.resetPass(${u.id}, '${u.username}')" class="text-slate-400 hover:text-red-600 p-2 transition" title="Сбросить пароль"><i class="fa-solid fa-key"></i></button>`
                    : '<span class="text-slate-200"><i class="fa-solid fa-ban"></i></span>';

                return `
                    <tr class="border-b border-slate-100 hover:bg-slate-50 last:border-0">
                        <td class="p-3 text-slate-400 font-mono text-xs">${u.id}</td>
                        <td class="p-3 font-mono font-bold text-slate-800">${u.username}</td>
                        <td class="p-3">${roleBadge}</td>
                        <td class="p-3 text-slate-600 text-sm">${u.object_name || '<span class="text-slate-300 italic">Нет привязки</span>'}</td>
                        <td class="p-3 text-center">${btnReset}</td>
                    </tr>`;
            }).join('');

            tb.innerHTML = rowsHtml;
        } catch (e) { console.error(e); }
    },

    resetPass: async (id, username) => {
        if (!confirm(`Вы уверены, что хотите сбросить пароль для пользователя ${username}?`)) return;
        try {
            const res = await apiFetch(`/api/arsenal/users/${id}/reset-password`, { method: 'POST' });
            if (res && res.ok) {
                const data = await res.json();
                UI.closeModal('usersModal');
                UI.showCredentialsModal(`Пароль сброшен!`, data.username, data.new_password);
            }
        } catch (e) { UI.showToast("Ошибка сброса пароля", "error"); }
    }
};

const Reports = {
    search: async () => {
        const q = document.getElementById('reportSearchInput').value.trim();
        if (q.length < 2) return UI.showToast("Введите минимум 2 символа для поиска.", "error");

        const list = document.getElementById('reportSearchList');
        document.getElementById('reportSearchResults').classList.remove('hidden');
        list.innerHTML = '<div class="p-2"><i class="fa-solid fa-spinner fa-spin text-purple-600"></i> Ищем...</div>';

        try {
            const res = await apiFetch(`/api/arsenal/reports/search-weapon?q=${encodeURIComponent(q)}`);
            const items = await res.json();

            list.innerHTML = items.length ? '' : '<span class="text-slate-400 text-sm p-2">Ничего не найдено</span>';

            items.forEach(i => {
                const btn = document.createElement('button');
                btn.className = "text-sm border border-purple-200 bg-purple-50 hover:bg-purple-100 text-purple-900 p-2.5 rounded-lg text-left transition flex flex-col items-start gap-1 w-full max-w-sm shrink-0 shadow-sm";
                btn.innerHTML = `
                    <span class="font-bold text-[13px] leading-tight">${i.name}</span>
                    <div class="flex flex-wrap gap-1 mt-1">
                        <span class="text-[10px] bg-white border border-purple-200 px-1.5 py-0.5 rounded font-mono text-purple-700">№ ${i.serial}</span>
                        ${i.inventory !== 'Б/Н' ? `<span class="text-[10px] bg-slate-100 border border-slate-200 px-1.5 py-0.5 rounded font-mono text-slate-500">Инв: ${i.inventory}</span>` : ''}
                    </div>
                `;
                btn.onclick = () => Reports.loadTimeline(i.serial, i.nom_id, i.name);
                list.appendChild(btn);
            });
        } catch (e) { list.innerHTML = '<span class="text-rose-500 text-sm p-2">Ошибка поиска</span>'; }
    },

    loadTimeline: async (serial, nomId, name) => {
        const container = document.getElementById('reportTimeline');
        const statusBox = document.getElementById('reportCurrentStatus');

        container.innerHTML = '<div class="pl-6 text-slate-500 flex items-center gap-2"><i class="fa-solid fa-spinner fa-spin text-purple-500"></i> Формируем отчет...</div>';
        statusBox.classList.add('hidden');

        try {
            const res = await apiFetch(`/api/arsenal/reports/timeline?serial=${encodeURIComponent(serial)}&nom_id=${nomId}`);
            if (!res.ok) throw new Error("API Error");
            const data = await res.json();

            // Статус (зеленый если на балансе, красный если списано)
            const isScrapped = data.status.includes('Списано');
            const statusColor = isScrapped ? 'text-rose-600' : 'text-emerald-600';

            statusBox.innerHTML = `
                <div class="flex justify-between items-center">
                    <div class="text-left">
                        <div class="text-xs text-purple-400 font-bold tracking-wider uppercase mb-1">Объект трассировки</div>
                        <div class="font-bold text-slate-800 text-lg">${name}</div>
                        <div class="text-xs text-slate-500 font-mono mt-0.5">Серия/Партия: <span class="bg-white px-1 rounded border border-purple-100 text-purple-700">${serial}</span></div>
                    </div>
                    <div class="text-right">
                        <div class="text-xs text-slate-400 font-bold uppercase mb-1">Текущее состояние</div>
                        <div class="font-bold ${statusColor} bg-white px-3 py-1.5 rounded-lg border border-purple-100 shadow-sm">${data.status}</div>
                    </div>
                </div>
            `;
            statusBox.classList.remove('hidden');

            const historyHtml = data.history.map(e => {
                let colorClass = "bg-slate-400";
                let iconClass = "fa-file";
                let opColorText = "text-slate-600";

                if (e.op_type === "Первичный ввод" || e.op_type === "Прием") {
                    colorClass = "bg-emerald-500"; iconClass = "fa-arrow-down-to-line"; opColorText="text-emerald-700";
                }
                else if (e.op_type === "Списание") {
                    colorClass = "bg-rose-500"; iconClass = "fa-ban"; opColorText="text-rose-700";
                }
                else if (e.op_type === "Перемещение" || e.op_type === "Отправка") {
                    colorClass = "bg-blue-500"; iconClass = "fa-truck-fast"; opColorText="text-blue-700";
                }

                const priceFmt = e.price > 0 ? e.price.toLocaleString('ru-RU', { style: 'currency', currency: 'RUB' }) : '---';

                return `
                    <div class="mb-8 ml-8 relative group">
                        <span class="absolute -left-[42px] flex items-center justify-center w-8 h-8 ${colorClass} rounded-full ring-4 ring-slate-50 shadow-sm text-white text-sm z-10">
                            <i class="fa-solid ${iconClass}"></i>
                        </span>
                        <div class="bg-white border border-slate-200 rounded-xl p-0 shadow-sm hover:shadow-md transition relative overflow-hidden flex flex-col">
                            
                            <!-- Шапка карточки операции -->
                            <div class="bg-slate-50 border-b border-slate-200 px-4 py-3 flex justify-between items-center">
                                <div>
                                    <span class="text-sm font-black ${opColorText} uppercase tracking-tight mr-2">${e.op_type}</span>
                                    <span class="text-xs text-slate-500 bg-white border border-slate-200 px-2 py-0.5 rounded cursor-pointer hover:bg-blue-50 hover:text-blue-700 hover:border-blue-200 transition font-bold" onclick="Documents.openViewModal(${e.doc_id})" title="Открыть документ">Док. № ${e.doc_number}</span>
                                </div>
                                <div class="text-xs font-mono font-bold text-slate-500 bg-white px-2 py-1 rounded border border-slate-200">
                                    <i class="fa-regular fa-calendar mr-1"></i> ${e.date}
                                </div>
                            </div>

                            <!-- Тело карточки -->
                            <div class="p-4 flex gap-4">
                                <div class="flex-1">
                                    <div class="flex items-center gap-3 text-sm text-slate-600">
                                        <div class="flex-1 bg-slate-50 p-2.5 rounded-lg border border-slate-100 flex flex-col">
                                            <span class="text-[10px] text-slate-400 font-bold uppercase mb-0.5">Откуда</span>
                                            <span class="font-medium text-slate-800 leading-tight">${e.source}</span>
                                        </div>
                                        <i class="fa-solid fa-arrow-right text-slate-300"></i>
                                        <div class="flex-1 bg-slate-50 p-2.5 rounded-lg border border-slate-100 flex flex-col">
                                            <span class="text-[10px] text-slate-400 font-bold uppercase mb-0.5">Куда</span>
                                            <span class="font-bold text-slate-800 leading-tight">${e.target}</span>
                                        </div>
                                    </div>
                                </div>

                                <!-- Финансовый/Количественный блок -->
                                <div class="w-32 shrink-0 border-l border-slate-100 pl-4 flex flex-col justify-center gap-2">
                                    <div>
                                        <span class="text-[10px] text-slate-400 uppercase font-bold block">Кол-во:</span>
                                        <span class="text-sm font-black text-slate-700 bg-slate-100 px-1.5 rounded">${e.quantity} шт.</span>
                                    </div>
                                    <div>
                                        <span class="text-[10px] text-slate-400 uppercase font-bold block">Сумма (Оценка):</span>
                                        <span class="text-xs font-mono font-bold text-slate-600">${priceFmt}</span>
                                    </div>
                                </div>
                            </div>

                        </div>
                    </div>`;
            }).join('');

            container.innerHTML = data.history.length ? historyHtml : '<div class="pl-6 text-slate-400 text-sm">История движения пуста. Изделие не найдено в проведенных документах.</div>';
        } catch (e) {
            console.error(e);
            container.innerHTML = '<div class="pl-6 text-rose-500 text-sm font-bold">Ошибка формирования отчета. Проверьте подключение к серверу.</div>';
        }
    }
};

const Objects = {
    loadAndShow: async () => {
        const tb = document.getElementById('objectsTableBody');
        const countEl = document.getElementById('objectsCountLabel');
        const isAdmin = AppState.userRole === 'admin';

        const addBtn = document.getElementById('btnAddObjectFromModal');
        if (addBtn) addBtn.style.display = isAdmin ? 'flex' : 'none';

        if (tb) UI.setLoading('objectsTableBody', 'Загрузка объектов...', 5);

        try {
            const res = await apiFetch('/api/arsenal/objects');
            if (!res || !res.ok) return;
            const objects = await res.json();

            AppState.objects = objects;
            if (countEl) countEl.textContent = `Всего объектов: ${objects.length}`;

            if (!tb) return;

            if (objects.length === 0) {
                tb.innerHTML = '<tr><td colspan="5" class="text-center p-8 text-slate-400">Нет объектов. Нажмите «Добавить объект».</td></tr>';
                return;
            }

            const typeBadge = (type) => {
                const styles = {
                    'Склад':         'bg-blue-100 text-blue-800 border-blue-200',
                    'Подразделение': 'bg-slate-100 text-slate-700 border-slate-200',
                    'Контрагент':    'bg-amber-100 text-amber-800 border-amber-200',
                };
                const cls = styles[type] || 'bg-slate-100 text-slate-700 border-slate-200';
                return `<span class="px-2 py-0.5 rounded text-xs font-bold border ${cls}">${type}</span>`;
            };

            tb.innerHTML = objects.map(o => {
                const safeName = o.name.replace(/'/g, "\\'").replace(/"/g, '&quot;');
                const delBtn = isAdmin
                    ? `<button onclick="Objects.deleteFromModal(${o.id}, '${safeName}')"
                           class="text-slate-400 hover:text-rose-600 p-1.5 rounded hover:bg-rose-50 transition" title="Удалить">
                           <i class="fa-solid fa-trash text-xs"></i>
                       </button>`
                    : '';
                return `
                    <tr class="border-b border-slate-100 hover:bg-slate-50 last:border-0">
                        <td class="p-3 text-slate-400 font-mono text-xs">${o.id}</td>
                        <td class="p-3">
                            <div class="flex items-center gap-2">
                                <i class="fa-solid ${o.obj_type === 'Склад' ? 'fa-box text-blue-500' : o.obj_type === 'Контрагент' ? 'fa-building text-amber-500' : 'fa-layer-group text-slate-400'} w-4 text-center shrink-0"></i>
                                <span class="font-semibold text-slate-800">${o.name}</span>
                            </div>
                        </td>
                        <td class="p-3">${typeBadge(o.obj_type)}</td>
                        <td class="p-3 text-slate-600 text-sm">${o.mol_name || '<span class="text-slate-300 italic text-xs">Не указан</span>'}</td>
                        <td class="p-3 text-center">
                            <div class="flex items-center justify-center gap-1">
                                <button onclick="UI.closeModal('objectsModal'); Balance.initModal(${o.id}, '${safeName}')"
                                    class="text-slate-400 hover:text-emerald-600 p-1.5 rounded hover:bg-emerald-50 transition" title="Открыть остатки">
                                    <i class="fa-solid fa-box-archive text-sm"></i>
                                </button>
                                ${delBtn}
                            </div>
                        </td>
                    </tr>`;
            }).join('');

        } catch (e) {
            console.error(e);
            if (tb) tb.innerHTML = '<tr><td colspan="5" class="text-center p-4 text-rose-500">Ошибка загрузки объектов.</td></tr>';
        }
    },

    deleteFromModal: async (id, name) => {
        if (!confirm(`Удалить объект "${name}"?\nВнимание: удаление возможно только если нет остатков и привязанных документов.`)) return;
        try {
            const res = await apiFetch(`/api/arsenal/objects/${id}`, { method: 'DELETE' });
            if (res && res.ok) {
                UI.showToast(`Объект "${name}" удалён.`, "success");
                await Objects.loadAndShow();
                await Dictionaries.loadObjectsTree();
                await Dashboard.loadKPIs();
            } else {
                const err = await res.json();
                UI.showToast("Ошибка: " + (err.detail || 'Не удалось удалить'), "error");
            }
        } catch (e) { UI.showToast("Сетевая ошибка", "error"); }
    }
};

const Dashboard = {
    loadKPIs: async () => {
        try {
            const res = await apiFetch('/api/arsenal/kpi');
            if (!res || !res.ok) return;
            const data = await res.json();

            document.getElementById('kpiTotalQty').innerText = data.total_qty.toLocaleString('ru-RU') + ' шт.';
            document.getElementById('kpiDocsCount').innerText = data.docs_count.toLocaleString('ru-RU');
            document.getElementById('kpiTotalSum').innerText = data.total_sum.toLocaleString('ru-RU', { style: 'currency', currency: 'RUB' });
            document.getElementById('kpiTransit').innerText = data.transit_qty.toLocaleString('ru-RU') + ' шт.';
        } catch (e) {
            console.error("Ошибка загрузки KPI:", e);
        }
        // Отдельный запрос на alerts — не критичный, не блокирует основные KPI
        Dashboard.loadLowStock();
        Dashboard.loadAnomalyCount();
    },

    /**
     * Счётчик активных аномалий (в виджете «Проверки»).
     * Число найденных правилами нарушений, которые ещё не dismissed / resolved.
     */
    loadAnomalyCount: async () => {
        const el = document.getElementById('kpiAnomalies');
        if (!el) return;
        try {
            const res = await apiFetch('/api/arsenal/analyzer/anomalies?limit=1');
            if (!res || !res.ok) {
                el.innerText = '—';
                return;
            }
            const data = await res.json();
            const n = data.total || 0;
            el.innerText = n;
            el.className = 'text-2xl font-bold ' + (n === 0 ? 'text-emerald-600' : 'text-amber-600');
        } catch (e) {
            el.innerText = '—';
        }
    },

    /**
     * Загружает алерты «низкие остатки» (партионные позиции, где qty < min_quantity).
     * Рисует счётчик в виджете, по клику показывает всплывашку со списком.
     */
    loadLowStock: async () => {
        const el = document.getElementById('kpiLowStock');
        const card = document.getElementById('kpiLowStockCard');
        if (!el || !card) return;
        try {
            const res = await apiFetch('/api/arsenal/alerts/low-stock');
            if (!res || !res.ok) {
                el.innerText = '—';
                return;
            }
            const data = await res.json();
            const n = data.total || 0;
            el.innerText = n;
            el.className = 'text-2xl font-bold ' + (n === 0 ? 'text-emerald-600' : 'text-rose-600');

            if (!card.dataset.bound) {
                card.dataset.bound = '1';
                card.addEventListener('click', () => Dashboard.toggleLowStockPopup());
            }
            Dashboard._lowStockData = data.items || [];
        } catch (e) {
            el.innerText = '—';
        }
    },

    toggleLowStockPopup: () => {
        const pop = document.getElementById('lowStockPopup');
        const body = document.getElementById('lowStockBody');
        if (!pop) return;
        if (!pop.classList.contains('hidden')) {
            pop.classList.add('hidden');
            return;
        }
        const items = Dashboard._lowStockData || [];
        if (!items.length) {
            body.innerHTML = '<div class="p-4 text-sm text-emerald-600"><i class="fa-solid fa-check-circle"></i> Всё в норме — остатки не ниже порогов.</div>';
        } else {
            body.innerHTML = items.map(x => {
                const pct = x.min_quantity ? Math.round((x.current_quantity / x.min_quantity) * 100) : 0;
                const barColor = x.severity === 'critical' ? 'bg-rose-500' : 'bg-amber-500';
                return `
                    <div class="px-4 py-3 border-b border-slate-100 last:border-0">
                        <div class="flex justify-between items-start mb-1">
                            <div>
                                <div class="font-semibold text-sm text-slate-800">${x.name}</div>
                                <div class="text-xs text-slate-500">${x.code || ''}</div>
                            </div>
                            <div class="text-xs text-right">
                                <span class="font-bold ${x.severity === 'critical' ? 'text-rose-600' : 'text-amber-600'}">${x.current_quantity}</span>
                                <span class="text-slate-400"> / ${x.min_quantity}</span>
                            </div>
                        </div>
                        <div class="w-full bg-slate-200 rounded-full h-1.5 overflow-hidden">
                            <div class="${barColor} h-1.5" style="width:${Math.min(100, pct)}%"></div>
                        </div>
                        ${x.deficit > 0 ? `<div class="text-xs text-rose-600 mt-1">Дефицит: ${x.deficit} шт.</div>` : ''}
                    </div>`;
            }).join('');
        }
        pop.classList.remove('hidden');
    }
};