/* Habeas Corpus — avisos de la campana
   Se actualizan cada minuto, suenan cuando llega uno nuevo y se marcan como leídos al abrirlos. */
(function () {
  const boton = document.getElementById('notifToggle');
  const panel = document.getElementById('notifDropdown');
  const lista = document.getElementById('avLista');
  if (!boton || !panel || !lista) return;

  const punto = document.getElementById('notifDot');
  const btnSonido = document.getElementById('avSonido');
  const btnPermiso = document.getElementById('avPermiso');
  const API = boton.dataset.api, LEER = boton.dataset.leer;
  const CADA = 60 * 1000;

  const leerLS = (k, d) => { try { return localStorage.getItem(k) ?? d; } catch (e) { return d; } };
  const guardarLS = (k, v) => { try { localStorage.setItem(k, v); } catch (e) {} };

  // Avisos que ya sonaron en este equipo, para no repetirlos en cada página
  let sonados = new Set(JSON.parse(leerLS('hc-avisos-sonados', '[]')));
  const recordar = claves => {
    claves.forEach(c => sonados.add(c));
    sonados = new Set([...sonados].slice(-300));
    guardarLS('hc-avisos-sonados', JSON.stringify([...sonados]));
  };

  /* ── Sonido ── */
  let conSonido = leerLS('hc-avisos-sonido', '1') !== '0';
  const pintarSonido = () => btnSonido && btnSonido.setAttribute('aria-pressed', conSonido ? 'true' : 'false');
  pintarSonido();

  let audio = null, pendiente = null;
  function campanada(urgente) {
    if (!conSonido) return;
    try {
      audio = audio || new (window.AudioContext || window.webkitAudioContext)();
      if (audio.state === 'suspended') {
        // El navegador no deja sonar hasta que la persona toca la página; suena en cuanto lo haga
        pendiente = urgente;
        audio.resume().catch(() => {});
        if (audio.state === 'suspended') return;
      }
      const notas = urgente ? [880, 660, 880] : [659.3, 987.8];
      notas.forEach((f, i) => {
        const t = audio.currentTime + i * 0.17;
        const o = audio.createOscillator(), g = audio.createGain();
        o.type = 'sine'; o.frequency.value = f;
        g.gain.setValueAtTime(0.0001, t);
        g.gain.exponentialRampToValueAtTime(0.22, t + 0.02);
        g.gain.exponentialRampToValueAtTime(0.0001, t + 0.55);
        o.connect(g).connect(audio.destination);
        o.start(t); o.stop(t + 0.6);
      });
      pendiente = null;
    } catch (e) {}
  }
  const alTocar = () => {
    if (pendiente === null || !audio) return;
    const u = pendiente;
    audio.resume().then(() => campanada(u)).catch(() => {});
  };
  ['pointerdown', 'keydown'].forEach(ev => document.addEventListener(ev, alTocar, { passive: true }));

  if (btnSonido) btnSonido.addEventListener('click', e => {
    e.stopPropagation();
    conSonido = !conSonido;
    guardarLS('hc-avisos-sonido', conSonido ? '1' : '0');
    pintarSonido();
    if (conSonido) campanada(false);
  });

  /* ── Avisos del navegador (cuando la pestaña no está a la vista) ── */
  const puedePedir = () => 'Notification' in window && Notification.permission === 'default';
  if (btnPermiso) {
    btnPermiso.hidden = !puedePedir();
    btnPermiso.addEventListener('click', e => {
      e.stopPropagation();
      Notification.requestPermission().then(() => { btnPermiso.hidden = !puedePedir(); });
    });
  }
  function avisoDelSistema(a) {
    if (!('Notification' in window) || Notification.permission !== 'granted' || !document.hidden) return;
    try {
      const n = new Notification(a.titulo, { body: [a.cuando, a.detalle].filter(Boolean).join(' · '), tag: a.clave, icon: '/static/brand/favicon.svg' });
      n.onclick = () => { window.focus(); location.href = a.url; };
    } catch (e) {}
  }

  /* ── Lista ── */
  const ICONOS = {
    audiencia: '<path d="m14 13-7.5 7.5a2.1 2.1 0 0 1-3-3L11 10M16 16l6-6M8 8l6-6M9 7l8 8"/>',
    termino: '<path d="M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/>',
    cobro: '<rect x="2.5" y="6" width="19" height="12" rx="2"/><circle cx="12" cy="12" r="2.5"/>',
    agenda: '<rect x="3" y="4.5" width="18" height="16" rx="2"/><path d="M3 9.5h18M8 2.5v4M16 2.5v4"/>',
  };
  ICONOS.ahora = ICONOS.termino;
  const esc = t => String(t ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  function pintar(avisos, sinLeer) {
    if (punto) {
      punto.hidden = !sinLeer;
      punto.textContent = sinLeer < 10 ? sinLeer : '9+';
    }
    lista.innerHTML = avisos.length ? avisos.map(a => `
      <a href="${esc(a.url)}" class="av-it t-${a.tipo}${a.urgente ? ' urgente' : ''}${a.leido ? ' leido' : ''}" data-clave="${esc(a.clave)}">
        <span class="av-ico" aria-hidden="true"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">${ICONOS[a.tipo] || ICONOS.agenda}</svg></span>
        <span class="av-tx"><b>${esc(a.titulo)}</b>${a.detalle ? `<small>${esc(a.detalle)}</small>` : ''}<em>${esc(a.cuando)}</em></span>
      </a>`).join('')
      : '<div class="av-vacio">Todo al día. Aquí te avisaremos de audiencias, términos y cobros.</div>';
  }

  function nuevos(avisos) {
    const frescos = avisos.filter(a => !a.leido && !sonados.has(a.clave));
    if (!frescos.length) return;
    recordar(frescos.map(a => a.clave));
    const urgente = frescos.some(a => a.urgente);
    campanada(urgente);
    boton.classList.remove('suena'); void boton.offsetWidth; boton.classList.add('suena');
    const primero = frescos[0];
    if (window.showToast) {
      showToast(frescos.length === 1 ? `${primero.titulo} · ${primero.cuando}`
        : `Tienes ${frescos.length} avisos nuevos. El primero: ${primero.titulo}`, urgente ? 'warning' : 'info', 6000);
    }
    frescos.slice(0, 3).forEach(avisoDelSistema);
  }

  async function actualizar() {
    try {
      const r = await fetch(API, { headers: { 'X-Requested-With': 'XMLHttpRequest' }, cache: 'no-store' });
      if (!r.ok) return;
      const d = await r.json();
      pintar(d.avisos, d.sin_leer);
      nuevos(d.avisos);
    } catch (e) {}
  }

  const marcar = (cuerpo) => fetch(LEER, {
    method: 'POST', keepalive: true,
    headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
    body: JSON.stringify(cuerpo),
  }).catch(() => {});

  // Abrir un aviso lo marca como leído y lleva a su expediente
  lista.addEventListener('click', e => {
    const it = e.target.closest('.av-it');
    if (it && !it.classList.contains('leido')) marcar({ claves: [it.dataset.clave] });
  });
  panel.addEventListener('click', e => { if (!e.target.closest('a')) e.stopPropagation(); });

  const btnTodos = document.getElementById('avTodos');
  if (btnTodos) btnTodos.addEventListener('click', e => {
    e.stopPropagation();
    lista.querySelectorAll('.av-it').forEach(it => it.classList.add('leido'));
    if (punto) punto.hidden = true;
    marcar({ todas: true });
  });

  // Lo que ya venía en la página cuenta como recién llegado si nunca había sonado
  const iniciales = [...lista.querySelectorAll('.av-it:not(.leido)')].map(it => ({
    clave: it.dataset.clave, leido: false, urgente: it.classList.contains('urgente'),
    titulo: it.querySelector('b').textContent, cuando: it.querySelector('em').textContent,
    detalle: (it.querySelector('small') || {}).textContent || '', url: it.getAttribute('href'),
  }));
  nuevos(iniciales);

  setInterval(actualizar, CADA);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) actualizar(); });
})();
