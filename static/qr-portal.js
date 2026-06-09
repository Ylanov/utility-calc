// static/qr-portal.js — анонимный QR-портал подачи показаний по квартире.
// Токен — во фрагменте URL (#...), на сервер в логи/Referer не уходит.
// Все запросы — /api/q/<token>/*. Без ФИО/адреса.

(function () {
  'use strict';

  var app = document.getElementById('app');
  var token = decodeURIComponent((location.hash || '').replace(/^#/, '')).trim();

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  function api(path, opts) {
    return fetch('/api/q/' + encodeURIComponent(token) + path, opts).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (body) {
        if (!r.ok) { var e = new Error(body.detail || ('Ошибка ' + r.status)); e.status = r.status; throw e; }
        return body;
      });
    });
  }

  function banner(cls, html) { return '<div class="banner ' + cls + '">' + html + '</div>'; }

  // Один счётчик: чёрные (5) + красные (3) поля. Защита от пропущенной точки.
  function meterBlock(key, label, icon, val) {
    var black = '', red = '';
    if (val) { var p = String(val).split('.'); black = p[0] || ''; red = p[1] || ''; }
    return '' +
      '<div class="meter">' +
      '  <div class="meter-label">' + icon + ' ' + esc(label) + '</div>' +
      '  <div class="digits">' +
      '    <span class="blk black"><input inputmode="numeric" maxlength="5" data-k="' + key + '-b" value="' + esc(black) + '" placeholder="00000"></span>' +
      '    <span class="dot">,</span>' +
      '    <span class="rd red"><input inputmode="numeric" maxlength="3" data-k="' + key + '-r" value="' + esc(red) + '" placeholder="000"></span>' +
      '  </div>' +
      '  <div class="cap">чёрные цифры&nbsp;&nbsp;,&nbsp;&nbsp;красные (3)</div>' +
      '</div>';
  }

  function readMeter(key) {
    var b = (app.querySelector('[data-k="' + key + '-b"]') || {}).value || '';
    var r = (app.querySelector('[data-k="' + key + '-r"]') || {}).value || '';
    b = b.replace(/\D/g, ''); r = r.replace(/\D/g, '');
    if (!b && !r) return null;            // пусто → не передаём (электричество опц.)
    if (!b) b = '0';
    while (r.length < 3) r += '0';        // красные дополняем до 3
    return b + '.' + r.slice(0, 3);
  }

  function showForm(state) {
    var cur = state.current || {};
    var editing = state.editable;
    app.className = '';
    app.innerHTML = '' +
      '<div class="card">' +
      '  <h1>Подача показаний</h1>' +
      '  <p class="sub">Период: ' + esc(state.period || '—') + '</p>' +
      (editing ? banner('b-warn', 'Показания за этот период уже переданы. Можно исправить — измените и отправьте снова.') : '') +
      '  <div class="hint">Вводите как на счётчике: крупные (чёрные) цифры до запятой, мелкие (красные, 3 шт.) — после. Красные пишите, даже если их «не считают».</div>' +
      meterBlock('hot', 'Горячая вода (ГВС)', '🔥', cur.hot_water) +
      meterBlock('cold', 'Холодная вода (ХВС)', '💧', cur.cold_water) +
      meterBlock('el', 'Электричество', '⚡', cur.electricity) +
      '  <button class="primary" id="send">' + (editing ? 'Исправить показания' : 'Передать показания') + '</button>' +
      '  <div id="msg"></div>' +
      '</div>';

    document.getElementById('send').addEventListener('click', function () {
      var btn = this, msg = document.getElementById('msg');
      var payload = { hot_water: readMeter('hot'), cold_water: readMeter('cold'), electricity: readMeter('el') };
      if (!payload.hot_water || !payload.cold_water) {
        msg.innerHTML = banner('b-err', 'Заполните показания горячей и холодной воды.');
        return;
      }
      btn.disabled = true; btn.textContent = 'Отправляем…';
      api('/submit', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      }).then(function () {
        showThanks(state.period);
      }).catch(function (e) {
        btn.disabled = false; btn.textContent = editing ? 'Исправить показания' : 'Передать показания';
        msg.innerHTML = banner('b-err', esc(e.message));
      });
    });
  }

  function showThanks(period) {
    app.className = '';
    app.innerHTML = '' +
      '<div class="card center">' +
      '  <div style="font-size:54px;">✅</div>' +
      '  <h1>Спасибо!</h1>' +
      '  <p class="sub">Показания за ' + esc(period || 'период') + ' приняты. Их проверит бухгалтерия.</p>' +
      banner('b-ok', 'Если ошиблись — можно исправить, пока бухгалтерия не приняла показания.') +
      '  <button class="ghost" id="fix">Исправить показания</button>' +
      '</div>';
    document.getElementById('fix').addEventListener('click', load);
  }

  function render(state) {
    if (state.no_residents) {
      app.className = ''; app.innerHTML = '<div class="card">' + banner('b-warn',
        'По этому коду пока нет зарегистрированных жильцов. Обратитесь к администратору.') + '</div>';
      return;
    }
    if (!state.has_period) {
      app.className = ''; app.innerHTML = '<div class="card">' + banner('b-warn',
        'Расчётный период сейчас закрыт. Загляните позже.') + '</div>';
      return;
    }
    if (!state.metered) {
      app.className = ''; app.innerHTML = '<div class="card"><h1>Ваша квартира</h1>' + banner('b-ok',
        'По этой квартире показания счётчиков не подаются — сумма фиксированная.') + '</div>';
      return;   // Фаза 2: тут появятся «скачать квитанцию» и «связаться с админом».
    }
    if (state.approved) {
      app.className = ''; app.innerHTML = '<div class="card center">' +
        '<div style="font-size:54px;">📋</div><h1>Показания приняты</h1>' +
        banner('b-ok', 'Показания за ' + esc(state.period) + ' уже проверены бухгалтерией. Изменить нельзя.') +
        '</div>';   // Фаза 2: «скачать квитанцию».
      return;
    }
    if (!state.window_open) {
      var w = state.window || {};
      app.className = ''; app.innerHTML = '<div class="card"><h1>Подача показаний</h1>' + banner('b-warn',
        'Приём показаний сейчас закрыт. Окно подачи: с ' + esc(w.start) + ' по ' + esc(w.end) +
        ' число месяца (сегодня ' + esc(w.today) + ').') + '</div>';
      return;
    }
    showForm(state);
  }

  function load() {
    app.className = 'center'; app.innerHTML = '<span class="spin" style="margin-top:80px;"></span>';
    api('/state').then(render).catch(function (e) {
      app.className = '';
      app.innerHTML = '<div class="card">' + banner('b-err',
        e.status === 404 ? 'Код не найден или больше не действует.' : esc(e.message)) + '</div>';
    });
  }

  if (!token) {
    app.className = '';
    app.innerHTML = '<div class="card">' + banner('b-err', 'Неверная ссылка: нет кода квартиры.') + '</div>';
  } else {
    load();
  }
})();
