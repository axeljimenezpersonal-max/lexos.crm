// Habeas Corpus — buscador inteligente (lista de resultados mientras escribes)
(function () {
  const ICONOS = {
    expediente: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    persona: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
    documento: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M9 13h6M9 17h6"/>',
    evento: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M16 3v4M8 3v4M3 10h18"/>',
    cobro: '<rect x="2.5" y="6" width="19" height="12" rx="2"/><circle cx="12" cy="12" r="2.5"/>',
  };
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const sinAcentos = s => s.normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();

  // Resalta las palabras buscadas sin importar acentos
  function resaltar(texto, tokens) {
    texto = String(texto ?? '');
    if (!tokens || !tokens.length) return esc(texto);
    const plano = sinAcentos(texto);
    const marcas = new Array(texto.length).fill(false);
    tokens.forEach(t => {
      let i = plano.indexOf(t);
      while (i > -1) {
        if (i === 0 || !/[a-z0-9]/.test(plano[i - 1])) for (let k = i; k < i + t.length; k++) marcas[k] = true;
        i = plano.indexOf(t, i + 1);
      }
    });
    let out = '', abierto = false;
    for (let i = 0; i < texto.length; i++) {
      if (marcas[i] && !abierto) { out += '<mark>'; abierto = true; }
      if (!marcas[i] && abierto) { out += '</mark>'; abierto = false; }
      out += esc(texto[i]);
    }
    return out + (abierto ? '</mark>' : '');
  }

  function item(it, tokens) {
    const lado = it.tipo === 'evento' && it.dia
      ? `<span class="bz-dia"><b>${esc(it.dia)}</b><small>${esc(it.mes)}</small></span>`
      : `<span class="bz-ico t-${it.tipo}"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${ICONOS[it.tipo] || ''}</svg></span>`;
    return `<a class="bz-item" role="option" href="${esc(it.url)}">${lado}
      <span class="bz-tx"><b>${resaltar(it.titulo, tokens)}</b>${it.sub ? `<small>${it.numero ? `<em>${resaltar(it.numero, tokens)}</em> · ` : ''}${resaltar(it.sub, tokens)}</small>` : ''}</span>
      ${it.chip ? `<span class="bz-est e-${esc(it.chip_clave || '')}">${esc(it.chip)}</span>` : ''}
      ${it.extra ? `<span class="bz-extra">${esc(it.extra)}</span>` : ''}</a>`;
  }

  function pintar(panel, d, verTodo) {
    let html = '';
    if (d.chips && d.chips.length) {
      html += `<div class="bz-p-entendi"><span>Entendí</span>${d.chips.map(c => `<i>${esc(c.etiqueta)}</i>`).join('')}</div>`;
    }
    if (d.sugerencia) {
      html += `<div class="bz-p-nota">No encontré «${esc(d.tokens.join(' '))}»; te muestro resultados de <b>${esc(d.sugerencia)}</b>.</div>`;
    }
    if (!d.grupos.length) {
      html += `<div class="bz-p-vacio"><b>Sin resultados</b><span>Prueba con otro nombre, número de expediente o una frase como «audiencias de la próxima semana».</span></div>`;
    }
    const tokens = (d.sugerencia ? d.sugerencia.split(' ') : d.tokens) || [];
    d.grupos.forEach(g => {
      html += `<div class="bz-grupo"><div class="bz-g-t"><span>${esc(g.titulo)}</span>${g.total > g.items.length ? `<a href="${esc(g.ver_todo)}">Ver los ${g.total}</a>` : ''}</div>
        ${g.items.map(it => item(it, tokens)).join('')}</div>`;
    });
    if (d.grupos.length && verTodo) {
      html += `<a class="bz-todo" href="${esc(verTodo)}">Ver todos los resultados de «${esc(d.q)}» <kbd>↵</kbd></a>`;
    }
    panel.innerHTML = html;
  }

  function conectar({ input, panel, en, alAbrir, alCerrar, urlTodo }) {
    let timer = null, pedido = 0, foco = -1;
    const opciones = () => [...panel.querySelectorAll('.bz-item, .bz-todo')];
    const abrir = () => { panel.hidden = false; input.setAttribute('aria-expanded', 'true'); alAbrir && alAbrir(); };
    const cerrar = () => { panel.hidden = true; input.setAttribute('aria-expanded', 'false'); foco = -1; alCerrar && alCerrar(); };
    function marcar(i) {
      const ops = opciones();
      ops.forEach(o => o.classList.remove('foco'));
      foco = Math.max(-1, Math.min(i, ops.length - 1));
      if (foco >= 0) { ops[foco].classList.add('foco'); ops[foco].scrollIntoView({ block: 'nearest' }); }
    }
    async function buscar() {
      const q = input.value.trim();
      if (q.length < 2) { cerrar(); return; }
      const n = ++pedido;
      panel.classList.add('cargando');
      try {
        const r = await fetch(`/buscar/api?q=${encodeURIComponent(q)}&en=${encodeURIComponent(en)}`, { headers: { 'X-Requested-With': 'XMLHttpRequest' } });
        const d = await r.json();
        if (n !== pedido) return;
        pintar(panel, d, urlTodo(q));
        foco = -1;
        abrir();
      } catch (_) { /* sin conexión: se queda la búsqueda normal con Enter */ }
      finally { panel.classList.remove('cargando'); }
    }
    input.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(buscar, 170); });
    input.addEventListener('focus', () => { if (input.value.trim().length >= 2 && panel.innerHTML) abrir(); });
    input.addEventListener('keydown', e => {
      if (panel.hidden) return;
      if (e.key === 'ArrowDown') { e.preventDefault(); marcar(foco + 1); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); marcar(foco - 1); }
      else if (e.key === 'Enter' && foco >= 0) { e.preventDefault(); opciones()[foco].click(); }
      else if (e.key === 'Escape') { e.stopPropagation(); cerrar(); }
    });
    return { buscar, cerrar };
  }

  // Buscadores dentro de las páginas
  document.querySelectorAll('form.bz').forEach(form => {
    const input = form.querySelector('.bz-input'), panel = form.querySelector('.bz-panel');
    const en = form.dataset.en || 'todo';
    const accion = form.getAttribute('action');
    const ctl = conectar({
      input, panel, en,
      alAbrir: () => form.classList.add('abierto'), alCerrar: () => form.classList.remove('abierto'),
      urlTodo: q => en === 'todo' ? `${accion}?q=${encodeURIComponent(q)}` : null,
    });
    document.addEventListener('click', e => { if (!form.contains(e.target)) ctl.cerrar(); });
    form.querySelectorAll('.bz-sugerencias [data-frase]').forEach(b => b.addEventListener('click', () => {
      const actual = input.value.trim();
      const frase = b.dataset.frase;
      if (sinAcentos(actual).includes(sinAcentos(frase))) return;
      input.value = (actual ? actual + ' ' : '') + frase;
      if (form.dataset.enviar) { form.submit(); return; }
      input.focus();
      ctl.buscar();
    }));
  });

  // Búsqueda global (⌘K)
  const overlay = document.getElementById('searchOverlay');
  const gInput = document.getElementById('searchInput'), gBody = document.getElementById('searchBody');
  if (overlay && gInput && gBody) {
    const hint = gBody.innerHTML;
    conectar({
      input: gInput, panel: gBody, en: 'todo',
      urlTodo: q => `/buscar/?q=${encodeURIComponent(q)}`,
      // En la ventana global el panel siempre se ve: si no hay búsqueda, muestra la ayuda
      alCerrar: () => { gBody.hidden = false; if (gInput.value.trim().length < 2) gBody.innerHTML = hint; },
    });
    gBody.addEventListener('click', e => {
      const b = e.target.closest('[data-ejemplo]');
      if (!b) return;
      gInput.value = b.dataset.ejemplo;
      gInput.dispatchEvent(new Event('input'));
      gInput.focus();
    });
    gInput.addEventListener('keydown', e => {
      if (e.key === 'Enter' && !gBody.querySelector('.foco') && gInput.value.trim()) {
        location.href = `/buscar/?q=${encodeURIComponent(gInput.value.trim())}`;
      }
    });
    new MutationObserver(() => { if (!overlay.classList.contains('open')) { gInput.value = ''; gBody.innerHTML = hint; } })
      .observe(overlay, { attributes: true, attributeFilter: ['class'] });
  }
})();
