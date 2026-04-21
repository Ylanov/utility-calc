/**
 * МОДУЛЬ: APP INITIALIZATION
 * Точка входа, привязка событий DOM и обработка Excel.
 */

document.addEventListener('DOMContentLoaded', async () => {
    // 1. ПОЛУЧАЕМ АКТУАЛЬНУЮ РОЛЬ И ДАННЫЕ С СЕРВЕРА
    const userRes = await apiFetch('/api/arsenal/me');
    if (!userRes || !userRes.ok) return; // Токен недействителен - apiFetch сам сделает редирект

    const userData = await userRes.json();
    AppState.userRole = userData.role;

    // Страхуем локальную сессию
    sessionStorage.setItem('arsenal_role', userData.role);
    sessionStorage.setItem('arsenal_username', userData.username);

    // 2. Проверка прав (UI Role Management)
    // Разблокируем админские кнопки, если сервер подтвердил права
    if (AppState.userRole === 'admin') {
        ['btnAddObject', 'menuNomenclature', 'menuUsers', 'btnImportExcel'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.style.display = '';
        });
    }

    // Показываем актуальное имя пользователя
    const usernameEl = document.getElementById('headerUsername');
    if (usernameEl) {
        usernameEl.textContent = userData.username || (userData.role === 'admin' ? 'Администратор' : 'Пользователь');
    }

    // 3. Установка текущей даты
    const dateInput = document.getElementById('newDocDate');
    if (dateInput) dateInput.valueAsDate = new Date();

    // 4. Привязка всех событий
    bindAppEvents();

    // 5. Загрузка данных
    await Promise.all([
        Dictionaries.loadNomenclature(),
        Dictionaries.loadObjectsTree(),
        Dashboard.loadKPIs()
    ]);

    // Обновление состояния формы после загрузки объектов
    Documents.updateFormState();
    await Documents.loadList();

    // Убираем сплэш загрузки только после полной готовности интерфейса
    const splash = document.getElementById('appSplash');
    if (splash) {
        splash.style.transition = 'opacity 0.3s ease';
        splash.style.opacity = '0';
        setTimeout(() => splash.remove(), 300);
    }
});

function bindAppEvents() {
    // --- Навигация ---
    document.getElementById('menuDocs')?.addEventListener('click', Documents.loadList);
    document.getElementById('menuObjects')?.addEventListener('click', Dictionaries.openObjectsModal);
    document.getElementById('menuNomenclature')?.addEventListener('click', Dictionaries.openNomenclatureModal);
    // Пользователи: отдельная полноценная страница с CRUD, активацией,
    // блокировкой, историей действий и статистикой по документам.
    document.getElementById('menuUsers')?.addEventListener('click', () => {
        window.location.href = 'arsenal_users.html';
    });
    // Отчёты: отдельная страница с табами (балансовая ведомость, по МОЛ,
    // оборот за период, топ-позиции, история изделия).
    document.getElementById('menuReports')?.addEventListener('click', () => {
        window.location.href = 'arsenal_reports.html';
    });

    // --- Основные кнопки ---
    document.getElementById('btnAddObject')?.addEventListener('click', () => UI.openModal('newObjectModal'));
    document.getElementById('btnAddObjectFromModal')?.addEventListener('click', () => UI.openModal('newObjectModal'));
    document.getElementById('btnOpenCreateModal')?.addEventListener('click', Documents.openCreateModal);
    document.getElementById('btnRefreshDocs')?.addEventListener('click', () => {
        Documents.state.skip = 0;
        Documents.state.searchQuery = '';
        const searchInput = document.getElementById('docSearchInput');
        if (searchInput) searchInput.value = '';
        Documents.loadList();
    });

    // --- Поиск по журналу документов ---
    let docSearchTimer = null;
    document.getElementById('docSearchInput')?.addEventListener('input', (e) => {
        clearTimeout(docSearchTimer);
        docSearchTimer = setTimeout(() => {
            Documents.state.skip = 0;
            Documents.state.searchQuery = e.target.value.trim();
            Documents.loadList();
        }, 400);
    });

    // --- Формы ---
    document.getElementById('newDocType')?.addEventListener('change', Documents.updateFormState);
    document.getElementById('btnAddRow')?.addEventListener('click', Documents.addRow);
    document.getElementById('btnSaveDoc')?.addEventListener('click', Documents.create);
    document.getElementById('btnSaveObject')?.addEventListener('click', Dictionaries.createObject);
    document.getElementById('btnSaveNom')?.addEventListener('click', Dictionaries.createNomenclature);

    // --- Формы Номенклатуры ---
    document.getElementById('btnUpdateNom')?.addEventListener('click', Dictionaries.updateNomenclature);
    document.getElementById('btnDeleteNom')?.addEventListener('click', Dictionaries.deleteNomenclature);
    document.getElementById('btnCancelEditNom')?.addEventListener('click', Dictionaries.resetNomenclatureForm);

    // --- Поиск в отчетах ---
    document.getElementById('btnReportSearch')?.addEventListener('click', Reports.search);
    document.getElementById('reportSearchInput')?.addEventListener('keyup', (e) => {
        if (e.key === 'Enter') Reports.search();
    });

    // --- Закрытие модалок ---
    document.querySelectorAll('.modal-close-btn, #btnCloseModal, #btnCancelModal').forEach(btn => {
        btn.addEventListener('click', () => UI.closeModal(btn.closest('.modal').id));
    });

    // Закрытие по клику вне окна и ESC
    document.querySelectorAll('.modal').forEach(modal => {
        modal.addEventListener('click', e => {
            if (e.target === modal) UI.closeModal(modal.id);
        });
    });
    document.addEventListener('keydown', e => {
        if (e.key === 'Escape') document.querySelectorAll('.modal').forEach(m => UI.closeModal(m.id));
    });

    // --- Logout ---
    document.getElementById('logoutBtn')?.addEventListener('click', async () => {
        if(confirm("Выйти из системы?")) {
            try { await fetch('/api/arsenal/logout', { method: 'POST' }); } catch (e) {}
            window.location.href = 'arsenal_login.html';
        }
    });

    // --- EXCEL IMPORT LOGIC ---
    const btnImport = document.getElementById('btnImportExcel');
    const fileInput = document.getElementById('excelUploadInput');

    if (btnImport && fileInput) {
        btnImport.addEventListener('click', () => fileInput.click());

        fileInput.addEventListener('change', async (event) => {
            const file = event.target.files[0];
            if (!file) return;

            UI.openModal('loadingOverlay');

            const formData = new FormData();
            formData.append("file", file);

            try {
                const response = await fetch('/api/arsenal/import', {
                    method: 'POST',
                    body: formData
                });

                UI.closeModal('loadingOverlay');

                if (response.ok) {
                    const result = await response.json();
                    let msg = `Импорт завершен!\nДобавлено позиций: ${result.added}\nПропущено строк: ${result.skipped}`;

                    // --- ЛОГИКА СКАЧИВАНИЯ ПАРОЛЕЙ ---
                    if (result.new_users && result.new_users.length > 0) {
                        msg += `\n\nСоздано новых складов и пользователей: ${result.new_users.length}. Скачивается файл с паролями...`;

                        // Формируем текст файла
                        let fileContent = "ОТЧЕТ О СОЗДАННЫХ ПОЛЬЗОВАТЕЛЯХ (СОХРАНИТЕ ЭТОТ ФАЙЛ)\n";
                        fileContent += "=========================================================\n\n";
                        result.new_users.forEach(u => {
                            fileContent += `СКЛАД:   ${u.object} (МОЛ: ${u.mol})\n`;
                            fileContent += `ЛОГИН:   ${u.username}\n`;
                            fileContent += `ПАРОЛЬ:  ${u.password}\n`;
                            fileContent += "---------------------------------------------------------\n";
                        });

                        // Создаем и кликаем скрытую ссылку для скачивания
                        const blob = new Blob([fileContent], { type: 'text/plain' });
                        const url = window.URL.createObjectURL(blob);
                        const a = document.createElement('a');
                        a.href = url;
                        a.download = `new_users_${new Date().toISOString().slice(0,10)}.txt`;
                        document.body.appendChild(a);
                        a.click();
                        document.body.removeChild(a);
                        window.URL.revokeObjectURL(url);
                    }
                    // ---------------------------------

                    if (result.errors && result.errors.length > 0) {
                        msg += `\n\nОшибки (первые 5):\n` + result.errors.slice(0, 5).join('\n');
                        UI.showToast(msg, "error");
                    } else {
                        UI.showToast(msg, "success");
                    }

                    // Перезагрузка данных
                    await Dictionaries.loadObjectsTree();
                    await Dictionaries.loadNomenclature();
                    await Dashboard.loadKPIs();
                    // Перезагружаем список пользователей, если окно открыто
                    if(document.getElementById('usersModal').style.display !== 'none') {
                        Users.loadAndShow();
                    }
                } else {
                    const error = await response.json();
                    UI.showToast(`Ошибка импорта: ${error.detail || 'Неизвестная ошибка'}`, "error");
                }
            } catch (error) {
                UI.closeModal('loadingOverlay');
                UI.showToast(`Критическая ошибка: ${error.message}`, "error");
            }
            // Сброс input
            fileInput.value = '';
        });
    }
}