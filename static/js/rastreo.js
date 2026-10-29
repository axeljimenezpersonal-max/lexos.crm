/* Habeas Corpus — medición de uso y aviso de cookies
   Solo mide si la persona aceptó. Registra qué pantalla abre, de dónde viene, cuánto tiempo se queda
   y qué pestañas usa. Nunca manda lo que se escribe ni títulos de expedientes. */
(function () {
  const yo = document.currentScript;
  const DESTINO = yo.dataset.destino, GA = yo.dataset.ga;
  const banner = document.getElementById('hcCookies');

  const leer = n => (document.cookie.match('(?:^|; )' + n + '=([^;]*)') || [])[1];
  const poner = (n, v, dias) => {
    document.cookie = `${n}=${v}; Max-Age=${dias * 86400}; Path=/; SameSite=Lax${location.protocol === 'https:' ? '; Secure' : ''}`;
  };
  const nuevoId = () => (crypto.randomUUID ? crypto.randomUUID() : Date.now().toString(36) + Math.random().toString(36).slice(2));

  function enviar(datos) {
    const cuerpo = JSON.stringify(datos);
    try {
      if (navigator.sendBeacon && navigator.sendBeacon(DESTINO, new Blob([cuerpo], { type: 'application/json' }))) return;
    } catch (e) {}
    fetch(DESTINO, { method: 'POST', body: cuerpo, keepalive: true, headers: { 'Content-Type': 'application/json' } }).catch(() => {});
  }

  let midiendo = false;
  function medir() {
    if (midiendo || !DESTINO) return;
    midiendo = true;
    const visitante = leer('hc_vid') || nuevoId();
    poner('hc_vid', visitante, 365);
    let sesion;
    try { sesion = sessionStorage.getItem('hc_ses') || nuevoId(); sessionStorage.setItem('hc_ses', sesion); } catch (e) { sesion = visitante; }
    const vid = nuevoId();
    const base = () => ({ vid, visitante, sesion, url: location.pathname + location.search, ancho: innerWidth });

    enviar({ ...base(), tipo: 'vista', desde: document.referrer });

    // Tiempo con la pantalla a la vista (no cuenta mientras está en otra pestaña)
    let acumulado = 0, desde = document.hidden ? null : Date.now();
    const cerrar = () => {
      if (desde !== null) { acumulado += Date.now() - desde; desde = null; }
      enviar({ tipo: 'tiempo', vid, ms: acumulado });
    };
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) cerrar(); else desde = Date.now();
    });
    addEventListener('pagehide', cerrar);

    // Pestañas internas y botones marcados con data-rastreo
    document.addEventListener('click', e => {
      const el = e.target.closest('[role="tab"], .xp-tab, [data-rastreo]');
      if (!el) return;
      const detalle = (el.dataset.rastreo || el.textContent || el.dataset.tab || '').replace(/\d+/g, '').trim().slice(0, 40);
      enviar({ ...base(), tipo: el.dataset.rastreo ? 'clic' : 'pestana', detalle });
    }, true);
    // Uso del buscador (solo cuántas veces y en qué pantalla, nunca lo que se busca)
    document.addEventListener('submit', e => {
      if (e.target.matches('.bz, form[role="search"]')) enviar({ ...base(), tipo: 'busqueda', detalle: 'Buscador' });
    }, true);
  }

  function cargarGA() {
    if (!GA || window.gtag) return;
    const s = document.createElement('script');
    s.async = true;
    s.src = 'https://www.googletagmanager.com/gtag/js?id=' + encodeURIComponent(GA);
    document.head.appendChild(s);
    window.dataLayer = window.dataLayer || [];
    window.gtag = function () { dataLayer.push(arguments); };
    gtag('js', new Date());
    gtag('config', GA, { anonymize_ip: true });
  }

  const empezar = () => { medir(); cargarGA(); };
  const decidir = si => {
    poner('hc_consent', si ? 'si' : 'no', 365);
    if (banner) banner.hidden = true;
    if (si) empezar();
  };

  const consentimiento = leer('hc_consent');
  if (consentimiento === 'si') empezar();
  else if (!consentimiento && banner) banner.hidden = false;

  if (banner) {
    banner.querySelector('[data-aceptar]').addEventListener('click', () => decidir(true));
    banner.querySelector('[data-rechazar]').addEventListener('click', () => decidir(false));
  }
  // Para volver a mostrar el aviso desde el menú de la cuenta o el pie de página
  window.hcCookies = () => { if (banner) banner.hidden = false; };
  document.addEventListener('click', e => {
    if (e.target.closest('[data-cookies]')) { e.preventDefault(); window.hcCookies(); }
  });
})();
