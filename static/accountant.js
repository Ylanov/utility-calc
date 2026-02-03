async function loadSummary() {

    const token = localStorage.getItem("token");

    const res = await fetch("/api/admin/summary", {
        headers: {
            "Authorization": "Bearer " + token
        }
    });

    const data = await res.json();

    const container = document.getElementById("summary");

    container.innerHTML = "";

    for (const dorm in data) {

        const block = document.createElement("div");
        block.className = "dorm-block";

        let html = `<h2>Общежитие ${dorm}</h2>`;

        html += `
        <table>
            <tr>
                <th>Жилец</th>
                <th>Площадь</th>
                <th>Горячая</th>
                <th>Холодная</th>
                <th>Канал.</th>
                <th>Свет</th>
                <th>Сод.</th>
                <th>Фикс.</th>
                <th>Итого</th>
            </tr>
        `;

        let totalDorm = 0;

        data[dorm].forEach(row => {

            totalDorm += row.total;

            html += `
            <tr>
                <td>${row.username}</td>
                <td>${row.area}</td>
                <td>${row.hot.toFixed(2)}</td>
                <td>${row.cold.toFixed(2)}</td>
                <td>${row.sewage.toFixed(2)}</td>
                <td>${row.electric.toFixed(2)}</td>
                <td>${row.maintenance.toFixed(2)}</td>
                <td>${row.fixed.toFixed(2)}</td>
                <td><b>${row.total.toFixed(2)}</b></td>
            </tr>
            `;
        });

        html += `
            <tr class="total-row">
                <td colspan="8"><b>ИТОГО</b></td>
                <td><b>${totalDorm.toFixed(2)}</b></td>
            </tr>
        `;

        html += "</table>";

        block.innerHTML = html;

        container.appendChild(block);
    }
}

loadSummary();
