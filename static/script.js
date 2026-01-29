function logout() {
    localStorage.removeItem('token');
    window.location.href = 'login.html';
}

if (!localStorage.getItem('token')) {
    window.location.href = 'login.html';
}

const token = localStorage.getItem('token');

// Глобальная переменная для хранения утвержденных значений (для валидации)
let lastReadings = { hot: 0, cold: 0, elect: 0 };

// 1. Загрузка данных (История + Черновик)
async function loadData() {
    try {
        const response = await fetch('/api/readings/state', {
            headers: { 'Authorization': `Bearer ${token}` }
        });

        if (response.ok) {
            const data = await response.json();

            // 1. Сохраняем "Базу" (последние утвержденные показания)
            lastReadings = {
                hot: data.prev_hot,
                cold: data.prev_cold,
                elect: data.prev_elect
            };

            // 2. Отображаем их текстом (так как HTML изменился на span)
            document.getElementById('prevHot').innerText = data.prev_hot.toFixed(2);
            document.getElementById('prevCold').innerText = data.prev_cold.toFixed(2);
            document.getElementById('prevElect').innerText = data.prev_elect.toFixed(2);

            const statusArea = document.getElementById('statusArea');
            const resultDiv = document.getElementById('result');
            const resultText = document.getElementById('resultText');

            // 3. Проверяем статус: Черновик или Чисто
            if (data.is_draft) {
                // ЕСТЬ ЧЕРНОВИК: Заполняем поля сохраненными (но не проверенными) данными
                document.getElementById('hotWater').value = data.current_hot;
                document.getElementById('coldWater').value = data.current_cold;
                document.getElementById('electricity').value = data.current_elect;

                // Показываем статус
                statusArea.innerHTML = `<div style="background: #fff3cd; color: #856404; padding: 10px; border-radius: 5px; margin-bottom: 15px; border: 1px solid #ffeeba;">
                    ✏️ <strong>Черновик сохранен.</strong><br>
                    Показания ожидают проверки бухгалтером. Вы можете изменить их и сохранить заново.
                </div>`;

                // Показываем предварительную сумму
                if (data.total_cost !== null) {
                    resultDiv.style.display = 'block';
                    resultText.innerText = `${data.total_cost.toFixed(2)} руб.`;
                }

            } else {
                // ЧИСТО: Заполняем поля предыдущими значениями (чтобы жильцу было удобнее крутить вверх)
                document.getElementById('hotWater').value = data.prev_hot;
                document.getElementById('coldWater').value = data.prev_cold;
                document.getElementById('electricity').value = data.prev_elect;

                statusArea.innerHTML = `<div style="background: #d1e7dd; color: #0f5132; padding: 10px; border-radius: 5px; margin-bottom: 15px; border: 1px solid #badbcc;">
                    ✅ <strong>Месяц закрыт.</strong><br>
                    Введите новые показания.
                </div>`;

                resultDiv.style.display = 'none';
            }

        } else if (response.status === 401) {
            logout();
        }
    } catch (error) {
        console.error("Ошибка загрузки данных", error);
    }
}

// 2. Отправка формы
document.getElementById('meterForm').addEventListener('submit', async function(e) {
    e.preventDefault();

    const currentHot = parseFloat(document.getElementById('hotWater').value);
    const currentCold = parseFloat(document.getElementById('coldWater').value);
    const currentElect = parseFloat(document.getElementById('electricity').value);

    // ВАЛИДАЦИЯ: Нельзя ввести меньше, чем последние УТВЕРЖДЕННЫЕ показания
    if (currentHot < lastReadings.hot) {
        alert(`Ошибка! Горячая вода (${currentHot}) не может быть меньше предыдущей утвержденной (${lastReadings.hot})`);
        return;
    }
    if (currentCold < lastReadings.cold) {
        alert(`Ошибка! Холодная вода (${currentCold}) не может быть меньше предыдущей утвержденной (${lastReadings.cold})`);
        return;
    }
    if (currentElect < lastReadings.elect) {
        alert(`Ошибка! Электричество (${currentElect}) не может быть меньше предыдущего утвержденного (${lastReadings.elect})`);
        return;
    }

    const data = {
        hot_water: currentHot,
        cold_water: currentCold,
        electricity: currentElect
    };

    try {
        const response = await fetch('/api/calculate', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${token}`
            },
            body: JSON.stringify(data)
        });

        if (response.ok) {
            const result = await response.json();
            alert("Показания успешно сохранены! Теперь они доступны бухгалтеру для проверки.");
            // Перезагружаем данные, чтобы обновить статус на "Черновик" и показать сумму
            loadData();
        } else {
            const err = await response.json();
            alert(`Ошибка: ${err.detail}`);
        }

    } catch (error) {
        alert('Ошибка соединения с сервером');
    }
});

// Запуск при старте
loadData();