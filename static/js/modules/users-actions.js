// static/js/modules/users-actions.js
import { api } from '../core/api.js';
import { toast, setLoading } from '../core/dom.js';
import { showImportResultModal } from './users-ui.js';

// ==========================================
// ИМПОРТ EXCEL
// ==========================================
export async function handleImport(importInput, btnImport, table) {
    const file = importInput.files[0];

    if (!file) {
        toast('Выберите файл Excel', 'info');
        return;
    }
    if (!file.name.match(/\.(xlsx|xls)$/)) {
        toast('Разрешены только файлы Excel (.xlsx, .xls)', 'error');
        return;
    }

    const formData = new FormData();
    formData.append('file', file);

    setLoading(btnImport, true, 'Загрузка...');

    try {
        const result = await api.post('/users/import_excel', formData);
        showImportResultModal(result);
        importInput.value = '';
        table.refresh();
    } catch (error) {
        toast(error.message, 'error');
    } finally {
        setLoading(btnImport, false);
    }
}

// ==========================================
// ЕДИНЫЙ ПРОЦЕСС: РАСЧЕТ + ПЕРЕСЕЛЕНИЕ/ВЫСЕЛЕНИЕ
// ==========================================
export async function openRelocateModal(user, rel, dormsCache) {
    if (!rel.modal) return;

    // Сброс формы перед открытием
    rel.form.reset();

    const address = user.room ? `${user.room.dormitory_name}, ком. ${user.room.room_number}` : 'Не привязан';
    rel.userId.value = user.id;
    rel.userName.textContent = user.username;
    rel.currentAddress.textContent = address;

    // Автоматическая установка дат
    const date = new Date();
    const totalDays = new Date(date.getFullYear(), date.getMonth() + 1, 0).getDate();
    rel.totalDays.value = totalDays;
    rel.daysLived.value = date.getDate();

    // Плейсхолдеры счетчиков
    rel.hot.placeholder = `Загрузка...`;
    rel.cold.placeholder = `Загрузка...`;
    rel.elect.placeholder = `Загрузка...`;

    // Сброс блока переселения
    rel.destinationBlock.style.display = 'none';
    const options = '<option value="">-- Выберите здание --</option>' +
                    dormsCache.map(d => `<option value="${d}">${d}</option>`).join('');
    rel.dormSelect.innerHTML = options;
    rel.roomSelect.innerHTML = '<option value="">Сначала выберите здание</option>';
    rel.roomSelect.disabled = true;

    // Снимаем обязательность с селектов комнаты по умолчанию
    rel.roomSelect.required = false;
    rel.dormSelect.required = false;

    // Дефолтное действие - выселение (радио-кнопка)
    const evictRadio = rel.form.querySelector('input[name="relAction"][value="evict"]');
    if(evictRadio) evictRadio.checked = true;

    rel.modal.classList.add('open');

    // Загрузка предыдущих показаний жильца (для подсказок)
    try {
        const state = await api.get(`/admin/readings/manual-state/${user.id}`);
        rel.hot.placeholder = `Пред: ${state.prev_hot}`;
        rel.cold.placeholder = `Пред: ${state.prev_cold}`;
        rel.elect.placeholder = `Пред: ${state.prev_elect}`;
    } catch (e) {
        rel.hot.placeholder = `Ошибка`;
        rel.cold.placeholder = `Ошибка`;
        rel.elect.placeholder = `Ошибка`;
    }
}

export async function handleRelocateSubmit(e, rel, table) {
    e.preventDefault();

    const action = document.querySelector('input[name="relAction"]:checked').value;

    const payload = {
        action: action, // 'move' или 'evict'
        total_days_in_month: parseInt(rel.totalDays.value),
        days_lived: parseInt(rel.daysLived.value),
        hot_water: parseFloat(rel.hot.value.replace(',', '.')),
        cold_water: parseFloat(rel.cold.value.replace(',', '.')),
        electricity: parseFloat(rel.elect.value.replace(',', '.')),
        new_room_id: null
    };

    if (action === 'move') {
        payload.new_room_id = parseInt(rel.roomSelect.value);
        if (isNaN(payload.new_room_id)) return toast('Для переселения выберите новую комнату', 'error');
    }

    if (action === 'evict') {
        if (!confirm('ВНИМАНИЕ! Пользователь будет окончательно выселен и удален. Финальная квитанция будет сформирована. Продолжить?')) return;
    } else {
        if (!confirm('Система сформирует квитанцию по текущей комнате и переведет жильца в новую. Подтверждаете?')) return;
    }

    setLoading(rel.btnSubmit, true, 'Обработка...');
    try {
        const res = await api.post(`/users/${rel.userId.value}/relocate`, payload);
        toast(res.message, 'success');
        rel.modal.classList.remove('open');
        table.refresh();
    } catch (err) {
        toast(err.message, 'error');
    } finally {
        setLoading(rel.btnSubmit, false, 'Подтвердить операцию');
    }
}