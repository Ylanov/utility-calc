// =========================================================
// 3. УПРАВЛЕНИЕ ТАРИФАМИ (TARIFFS)
// =========================================================

/**
 * Загружает текущие тарифы и заполняет форму
 */
async function loadTariffs() {
    try {
        const response = await fetch('/api/tariffs', {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (response.ok) {
            const t = await response.json();
            document.getElementById('t_main').value = t.maintenance_repair;
            document.getElementById('t_rent').value = t.social_rent;
            document.getElementById('t_waste').value = t.waste_disposal;
            document.getElementById('t_heat').value = t.heating;
            document.getElementById('t_w_heat').value = t.water_heating;
            document.getElementById('t_w_sup').value = t.water_supply;
            document.getElementById('t_sew').value = t.sewage;
            document.getElementById('t_el_sqm').value = t.electricity_per_sqm;
            document.getElementById('t_el_rate').value = t.electricity_rate;
        }
    } catch (e) {
        console.error("Ошибка загрузки тарифов:", e);
    }
}

/**
 * Обработчик формы сохранения тарифов
 */
document.getElementById('tariffsForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const submitBtn = e.target.querySelector('button[type="submit"]');
    const originalText = submitBtn.innerText;
    submitBtn.innerText = "Сохранение...";

    const data = {
        maintenance_repair: parseFloat(document.getElementById('t_main').value),
        social_rent: parseFloat(document.getElementById('t_rent').value),
        heating: parseFloat(document.getElementById('t_heat').value),
        water_heating: parseFloat(document.getElementById('t_w_heat').value),
        water_supply: parseFloat(document.getElementById('t_w_sup').value),
        sewage: parseFloat(document.getElementById('t_sew').value),
        waste_disposal: parseFloat(document.getElementById('t_waste').value),
        electricity_per_sqm: parseFloat(document.getElementById('t_el_sqm').value),
        electricity_rate: parseFloat(document.getElementById('t_el_rate').value)
    };

    try {
        const response = await fetch('/api/tariffs', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
            body: JSON.stringify(data)
        });

        if (response.ok) {
            alert("Тарифы обновлены!");
        } else {
            alert("Ошибка сохранения");
        }
    } catch (e) {
        alert("Ошибка сети");
    }
    finally {
        submitBtn.innerText = originalText;
    }
});