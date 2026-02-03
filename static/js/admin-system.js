// =========================================================
// 4. СИСТЕМА (BACKUP & RESTORE)
// =========================================================

/**
 * Запускает скачивание бэкапа базы данных
 */
async function downloadBackup() {
    try {
        const res = await fetch('/api/admin/backup', { headers: { 'Authorization': `Bearer ${token}` } });
        if (res.ok) {
            const blob = await res.blob();
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.style.display = 'none';
            a.href = url;
            a.download = `backup_${new Date().toISOString().slice(0, 10)}.sql`;
            document.body.appendChild(a);
            a.click();
            window.URL.revokeObjectURL(url);
            a.remove();
        } else {
            alert("Ошибка скачивания бэкапа");
        }
    } catch (e) {
        alert("Ошибка сети");
    }
}

/**
 * Запускает восстановление базы данных из выбранного файла
 */
async function restoreBackup() {
    const fileInput = document.getElementById('restoreFile');
    const file = fileInput.files[0];

    if (!file) {
        alert("Пожалуйста, выберите .sql файл для восстановления!");
        return;
    }

    if (!confirm("ВНИМАНИЕ!\n\nЭто действие полностью заменит текущую базу данных данными из файла. Все несохраненные изменения будут потеряны.\n\nПродолжить?")) {
        return;
    }

    const formData = new FormData();
    formData.append('file', file);

    const restoreButton = document.querySelector('button[onclick="restoreBackup()"]');
    const originalText = restoreButton.innerText;

    try {
        restoreButton.disabled = true;
        restoreButton.innerText = "Восстановление...";

        const res = await fetch('/api/admin/restore', {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${token}` },
            body: formData
        });

        if (res.ok) {
            alert("База данных успешно восстановлена! Страница будет перезагружена.");
            location.reload();
        } else {
            const err = await res.json();
            alert("Произошла ошибка при восстановлении:\n\n" + err.detail);
        }
    } catch (e) {
        alert("Произошла сетевая ошибка. Проверьте консоль для деталей.");
    } finally {
        restoreButton.disabled = false;
        restoreButton.innerText = originalText;
        fileInput.value = '';
    }
}