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

  // Пароль портала: второй фактор к токену (QR могут сфотографировать).
  // Запоминаем на устройстве жильца — вводится один раз, чужому телефону
  // пароль неизвестен. localStorage может быть недоступен (приватный режим).
  var KEY_STORE = 'qrkey:' + token;
  function getKey() { try { return localStorage.getItem(KEY_STORE) || ''; } catch (e) { return ''; } }
  function setKey(v) { try { localStorage.setItem(KEY_STORE, v); } catch (e) {} }
  function clearKey() { try { localStorage.removeItem(KEY_STORE); } catch (e) {} }

  function api(path, opts) {
    opts = opts || {};
    var headers = opts.headers || {};
    var k = getKey();
    if (k) headers['X-QR-Key'] = k;
    opts.headers = headers;
    return fetch('/api/q/' + encodeURIComponent(token) + path, opts).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (body) {
        if (!r.ok) { var e = new Error(body.detail || ('Ошибка ' + r.status)); e.status = r.status; throw e; }
        return body;
      });
    });
  }

  function banner(cls, html) { return '<div class="banner ' + cls + '">' + html + '</div>'; }

  // Общий футер: скачать квитанцию (если готова) + связаться с админом.
  function footer(state) {
    var s = state || {};
    var parts = [];
    if (s.receipt_available) {
      // Кнопка + fetch (не <a href>): квитанция за паролем, нужен заголовок X-QR-Key.
      parts.push('<button class="ghost" data-act="receipt">📄 Скачать квитанцию' +
        (s.receipt_period ? ' за ' + esc(s.receipt_period) : '') + '</button>');
    }
    parts.push('<button class="ghost" data-act="contact-toggle">✉️ Связаться с администратором</button>');
    parts.push('<div data-contact-box style="display:none; margin-top:10px;">' +
      '<div data-contact-history style="margin-bottom:10px;"></div>' +
      '<textarea data-contact-msg rows="4" style="width:100%; box-sizing:border-box; border:2px solid #cbd5e1; border-radius:10px; padding:10px; font-size:15px;" placeholder="Опишите вопрос администратору…"></textarea>' +
      '<button class="primary" data-act="contact-send" style="margin-top:8px;">Отправить</button>' +
      '<div data-contact-out></div></div>');
    return '<div class="card">' + parts.join('') + '</div>';
  }

  // Переписка с админом (вопрос жильца + ответ админа). Авто-удаляется через 5 дней.
  function renderMessages(list) {
    var box = app.querySelector('[data-contact-history]');
    if (!box) return;
    if (!list || !list.length) {
      box.innerHTML = '<div style="font-size:12px; color:var(--muted);">Здесь появится ваша переписка с администратором (хранится 5 дней).</div>';
      return;
    }
    box.innerHTML = list.slice().reverse().map(function (m) {
      var you = '<div style="background:#eef2ff; border-radius:8px; padding:8px 10px; margin-bottom:6px; font-size:13px;"><b>Вы:</b> ' + esc(m.message) + '</div>';
      var adm = m.admin_response
        ? '<div style="background:#dcfce7; border-radius:8px; padding:8px 10px; margin:0 0 12px 12px; font-size:13px;"><b>Администратор:</b> ' + esc(m.admin_response) + '</div>'
        : '<div style="font-size:11px; color:var(--muted); margin:0 0 12px 12px;">⏳ Ожидает ответа администратора…</div>';
      return you + adm;
    }).join('');
  }

  function loadMessages() {
    api('/messages').then(function (r) { renderMessages(r.messages || []); }).catch(function () {});
  }

  var footerWired = false;
  function wireFooterOnce() {
    if (footerWired) return; footerWired = true;
    app.addEventListener('click', function (e) {
      var rc = e.target.closest('[data-act="receipt"]');
      if (rc) {
        rc.disabled = true; var was = rc.textContent; rc.textContent = 'Скачиваем…';
        fetch('/api/q/' + encodeURIComponent(token) + '/receipt', { headers: { 'X-QR-Key': getKey() } })
          .then(function (r) {
            if (!r.ok) throw new Error('Не удалось скачать квитанцию (' + r.status + ').');
            return r.blob();
          })
          .then(function (blob) {
            var a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = 'kvitanciya.pdf';
            document.body.appendChild(a); a.click(); a.remove();
            setTimeout(function () { URL.revokeObjectURL(a.href); }, 30000);
          })
          .catch(function (err) { alert(err.message); })
          .then(function () { rc.disabled = false; rc.textContent = was; });
        return;
      }
      if (e.target.closest('[data-act="contact-toggle"]')) {
        var box = app.querySelector('[data-contact-box]');
        if (box) {
          var opening = (box.style.display === 'none');
          box.style.display = opening ? 'block' : 'none';
          if (opening) loadMessages();   // подтянуть переписку + ответы админа
        }
        return;
      }
      var send = e.target.closest('[data-act="contact-send"]');
      if (!send) return;
      var ta = app.querySelector('[data-contact-msg]');
      var out = app.querySelector('[data-contact-out]');
      var msg = ((ta && ta.value) || '').trim();
      if (msg.length < 5) { if (out) out.innerHTML = banner('b-err', 'Напишите вопрос — хотя бы пару слов.'); return; }
      send.disabled = true; send.textContent = 'Отправляем…';
      api('/contact', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message: msg }) })
        .then(function () {
          if (out) out.innerHTML = banner('b-ok', 'Отправлено. Ответ администратора появится здесь.');
          if (ta) ta.value = '';
          send.disabled = false; send.textContent = 'Отправить';
          loadMessages();   // показать только что отправленное + будущие ответы
        })
        .catch(function (e2) {
          send.disabled = false; send.textContent = 'Отправить';
          if (out) out.innerHTML = banner('b-err', esc(e2.message));
        });
    });
  }

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

  // Строгая проверка 5+3: РОВНО 5 чёрных и 3 красные цифры (8 всего).
  // empty — оба поля пусты; error — заполнено частично/не 5+3.
  function readMeter(key) {
    var b = (app.querySelector('[data-k="' + key + '-b"]') || {}).value || '';
    var r = (app.querySelector('[data-k="' + key + '-r"]') || {}).value || '';
    b = b.replace(/\D/g, ''); r = r.replace(/\D/g, '');
    if (!b && !r) return { empty: true };
    if (b.length !== 5 || r.length !== 3) return { error: true };
    return { value: b + '.' + r };
  }

  // Какие счётчики спрашивать у этой квартиры (state.meters). Дом «только
  // вода» → электричество не рендерим вовсе (его вносят электрики через
  // админку). Старый сервер без поля meters → все три (обратная совместимость).
  function metersOf(state) {
    var m = (state && state.meters) || {};
    return {
      hot: m.hot !== false,
      cold: m.cold !== false,
      el: m.el !== false,
    };
  }

  // Блок «Ваши последние показания» + пометка про норматив за пропуски (#2).
  function lastActualBlock(state) {
    var out = '';
    var la = state.last_actual;
    var m = metersOf(state);
    if (la) {
      var vals = [];
      if (m.hot) vals.push('🔥 ГВС ' + esc(la.hot_water));
      if (m.cold) vals.push('💧 ХВС ' + esc(la.cold_water));
      if (m.el) vals.push('⚡ ' + esc(la.electricity));
      out += '<div style="background:#f0f9ff; border-left:3px solid var(--pri); border-radius:8px; padding:10px 12px; margin:10px 0; font-size:13px; line-height:1.6;">' +
        '<b>Ваши последние показания</b>' + (la.period ? ' (за ' + esc(la.period) + ')' : '') + ':<br>' +
        vals.join(' &nbsp; ') +
        '<div style="color:var(--muted); margin-top:4px;">Новые показания не могут быть меньше этих.</div>' +
        '</div>';
    }
    var ns = state.norm_since || [];
    if (ns.length) {
      var lines = ns.map(function (n) {
        var amt = (n.amount != null) ? (' — по нормативу ' + n.amount.toLocaleString('ru-RU') + ' ₽') : '';
        return esc(n.period) + amt;
      }).join('<br>');
      out += banner('b-warn', '⚠️ За эти периоды вы не подавали показания — начислено по нормативу:<br>' + lines);
    }
    return out;
  }

  function money(v) {
    return Number(v || 0).toLocaleString('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  // Долг/переплата по квартире (по данным 1С, обновляется раз в день).
  function balanceBlock(state) {
    var b = state && state.balance;
    if (!b) return '';
    var base = 'margin:10px 0; padding:12px 14px; border-radius:10px; font-size:15px;';
    if (b.debt > 0.005) {
      return '<div style="' + base + ' background:#fef2f2; border:1px solid #fecaca; color:#991b1b;">' +
        'Ваш долг: <b>' + money(b.debt) + ' ₽</b>' +
        '<div style="font-size:11px; color:#9ca3af; margin-top:2px;">по данным 1С · обновляется раз в день</div></div>';
    }
    if (b.overpayment > 0.005) {
      return '<div style="' + base + ' background:#f0fdf4; border:1px solid #bbf7d0; color:#166534;">' +
        'Ваша переплата (аванс): <b>' + money(b.overpayment) + ' ₽</b>' +
        '<div style="font-size:11px; color:#9ca3af; margin-top:2px;">по данным 1С · обновляется раз в день</div></div>';
    }
    return '<div style="' + base + ' background:#f8fafc; border:1px solid #e2e8f0; color:#475569;">Задолженности нет ✓ <span style="font-size:11px; color:#9ca3af;">(по данным 1С)</span></div>';
  }

  function showForm(state) {
    var cur = state.current || {};
    var editing = state.editable;
    var m = metersOf(state);
    app.className = '';
    app.innerHTML = '' +
      '<div class="card">' +
      '  <h1>Подача показаний</h1>' +
      '  <p class="sub">Период: ' + esc(state.period || '—') + '</p>' +
      balanceBlock(state) +
      (editing ? banner('b-warn', 'Показания за этот период уже переданы. Можно исправить — измените и отправьте снова.') : '') +
      lastActualBlock(state) +
      '  <div class="hint">Вводите как на счётчике: крупные (чёрные) цифры до запятой, мелкие (красные, 3 шт.) — после. Красные пишите, даже если их «не считают».</div>' +
      (m.hot ? meterBlock('hot', 'Горячая вода (ГВС)', '🔥', cur.hot_water) : '') +
      (m.cold ? meterBlock('cold', 'Холодная вода (ХВС)', '💧', cur.cold_water) : '') +
      (m.el ? meterBlock('el', 'Электричество', '⚡', cur.electricity) : '') +
      '  <button class="primary" id="send">' + (editing ? 'Исправить показания' : 'Передать показания') + '</button>' +
      '  <div id="msg"></div>' +
      '</div>' + footer(state);

    document.getElementById('send').addEventListener('click', function () {
      var btn = this, msg = document.getElementById('msg');
      var errs = [];
      var FMT = 'введите ВСЕ цифры: 5 чёрных и 3 красные';
      var payload = {};
      if (m.hot) {
        var hot = readMeter('hot');
        if (hot.empty || hot.error) errs.push('Горячая вода — ' + FMT + '.');
        else payload.hot_water = hot.value;
      }
      if (m.cold) {
        var cold = readMeter('cold');
        if (cold.empty || cold.error) errs.push('Холодная вода — ' + FMT + '.');
        else payload.cold_water = cold.value;
      }
      if (m.el) {
        var el = readMeter('el');
        if (el.empty || el.error) errs.push('Электричество — ' + FMT + '.');
        else payload.electricity = el.value;
      }
      if (errs.length) { msg.innerHTML = banner('b-err', errs.join('<br>')); return; }
      btn.disabled = true; btn.textContent = 'Отправляем…';
      api('/submit', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      }).then(function () {
        showThanks(state);
      }).catch(function (e) {
        if (e.status === 401) { showEnterPassword('Сессия истекла — введите пароль ещё раз.'); return; }
        btn.disabled = false; btn.textContent = editing ? 'Исправить показания' : 'Передать показания';
        msg.innerHTML = banner('b-err', esc(e.message));
      });
    });
  }

  function showThanks(state) {
    var period = (state && state.period) || 'период';
    app.className = '';
    app.innerHTML = '' +
      '<div class="card center">' +
      '  <div style="font-size:54px;">✅</div>' +
      '  <h1>Спасибо!</h1>' +
      '  <p class="sub">Показания за ' + esc(period) + ' приняты. Их проверит бухгалтерия.</p>' +
      banner('b-ok', 'Если ошиблись — можно исправить, пока бухгалтерия не приняла показания.') +
      '  <button class="ghost" id="fix">Исправить показания</button>' +
      '</div>' + footer(state);
    document.getElementById('fix').addEventListener('click', load);
  }

  // Первый вход: пароль ещё не установлен — жилец придумывает его здесь.
  function showSetPassword() {
    app.className = '';
    app.innerHTML = '' +
      '<div class="card">' +
      '  <div style="font-size:44px; text-align:center;">🔐</div>' +
      '  <h1 style="text-align:center;">Придумайте пароль</h1>' +
      '  <p class="sub" style="text-align:center;">Это первый вход по QR-коду вашей квартиры. ' +
      'Пароль защитит подачу показаний от посторонних — его будете знать только вы.</p>' +
      '  <input type="password" id="pw1" autocomplete="new-password" placeholder="Пароль (минимум 4 символа)" ' +
      '     style="width:100%; box-sizing:border-box; border:2px solid #cbd5e1; border-radius:10px; padding:12px; font-size:16px; margin-bottom:8px;">' +
      '  <input type="password" id="pw2" autocomplete="new-password" placeholder="Повторите пароль" ' +
      '     style="width:100%; box-sizing:border-box; border:2px solid #cbd5e1; border-radius:10px; padding:12px; font-size:16px;">' +
      '  <button class="primary" id="pwSave" style="margin-top:10px;">Сохранить пароль</button>' +
      '  <div id="pwMsg"></div>' +
      '  <div style="font-size:12px; color:var(--muted); margin-top:10px;">Запишите пароль. Если забудете — администратор сбросит его, и вы придумаете новый.</div>' +
      '</div>';
    document.getElementById('pwSave').addEventListener('click', function () {
      var btn = this, msg = document.getElementById('pwMsg');
      var p1 = document.getElementById('pw1').value, p2 = document.getElementById('pw2').value;
      if (p1.length < 4) { msg.innerHTML = banner('b-err', 'Пароль слишком короткий — минимум 4 символа.'); return; }
      if (p1 !== p2) { msg.innerHTML = banner('b-err', 'Пароли не совпадают.'); return; }
      btn.disabled = true; btn.textContent = 'Сохраняем…';
      api('/password', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password: p1 }) })
        .then(function () { setKey(p1); load(); })
        .catch(function (e) {
          btn.disabled = false; btn.textContent = 'Сохранить пароль';
          // 409 — кто-то успел установить раньше (другой жилец квартиры).
          if (e.status === 409) { showEnterPassword(e.message); return; }
          msg.innerHTML = banner('b-err', esc(e.message));
        });
    });
  }

  // Пароль установлен, на этом устройстве его нет/он неверный — спросить.
  function showEnterPassword(errText) {
    clearKey();
    app.className = '';
    app.innerHTML = '' +
      '<div class="card">' +
      '  <div style="font-size:44px; text-align:center;">🔒</div>' +
      '  <h1 style="text-align:center;">Введите пароль</h1>' +
      '  <p class="sub" style="text-align:center;">Подача показаний по этой квартире защищена паролем.</p>' +
      (errText ? banner('b-err', esc(errText)) : '') +
      '  <input type="password" id="pwIn" autocomplete="current-password" placeholder="Пароль" ' +
      '     style="width:100%; box-sizing:border-box; border:2px solid #cbd5e1; border-radius:10px; padding:12px; font-size:16px;">' +
      '  <button class="primary" id="pwGo" style="margin-top:10px;">Войти</button>' +
      '  <div style="font-size:12px; color:var(--muted); margin-top:10px;">Забыли пароль? Обратитесь к администратору — он сбросит, и вы придумаете новый.</div>' +
      '</div>';
    var go = function () {
      var v = document.getElementById('pwIn').value;
      if (!v) return;
      setKey(v);
      load();
    };
    document.getElementById('pwGo').addEventListener('click', go);
    document.getElementById('pwIn').addEventListener('keydown', function (e) { if (e.key === 'Enter') go(); });
  }

  function render(state) {
    if (state.password_setup_required) { showSetPassword(); return; }
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
        'По этой квартире показания счётчиков не подаются — сумма фиксированная.') + balanceBlock(state) + '</div>' + footer(state);
      return;
    }
    if (state.approved) {
      app.className = ''; app.innerHTML = '<div class="card center">' +
        '<div style="font-size:54px;">📋</div><h1>Показания приняты</h1>' +
        banner('b-ok', 'Показания за ' + esc(state.period) + ' уже проверены бухгалтерией. Изменить нельзя.') +
        balanceBlock(state) + '</div>' + footer(state);
      return;
    }
    if (!state.window_open) {
      var w = state.window || {};
      app.className = ''; app.innerHTML = '<div class="card"><h1>Подача показаний</h1>' + banner('b-warn',
        'Приём показаний сейчас закрыт. Окно подачи: с ' + esc(w.start) + ' по ' + esc(w.end) +
        ' число месяца (сегодня ' + esc(w.today) + ').') + balanceBlock(state) + '</div>' + footer(state);
      return;
    }
    showForm(state);
  }

  function load() {
    app.className = 'center'; app.innerHTML = '<span class="spin" style="margin-top:80px;"></span>';
    var hadKey = !!getKey();
    api('/state').then(render).catch(function (e) {
      if (e.status === 401) {
        // Был сохранён ключ и он не подошёл (пароль сброшен/изменён) либо
        // ключа нет вовсе — спросить. Текст ошибки только если ключ БЫЛ.
        showEnterPassword(hadKey ? 'Пароль не подошёл. Попробуйте ещё раз.' : '');
        return;
      }
      app.className = '';
      app.innerHTML = '<div class="card">' + banner('b-err',
        e.status === 404 ? 'Код не найден или больше не действует.' : esc(e.message)) + '</div>';
    });
  }

  if (!token) {
    app.className = '';
    app.innerHTML = '<div class="card">' + banner('b-err', 'Неверная ссылка: нет кода квартиры.') + '</div>';
  } else {
    wireFooterOnce();
    load();
  }
})();
