(function() {
  // Headless voice-UI bridge on the 小智 kiosk page (127.0.0.1:8090).
  // 2026-11-10 (Round 109 S21): bottom QQ音乐 dock/mini-player removed per user preference
  // (all music touch controls & seek bar live on the Home page); keeps only the lightweight
  // open_ui_seq watcher so voice commands ("小麦小麦，打开QQ音乐") still open #tsSubLayer.
  if (window.location.port !== '8090' || window.__tsQqVoiceBridge) return;
  window.__tsQqVoiceBridge = true;

  const API = 'http://127.0.0.1:8091';
  let lastBootId = null;
  let lastSeq = null;
  let tickBusy = false;
  let tickPending = false;
  let tickTimer = 0;
  let lastVoiceBusyAt = 0;
  let pendingOpenTimer = 0;
  let wantOpenOnWake = false;
  let pendingSubCmd = '';

  function tabToSubCmd(tab) {
    return tab === 'radio' ? 'openRadio' : (tab === 'account' ? 'openAccount' : '');
  }

  function openQqMusic(retries = 0, subCmd = '') {
    clearTimeout(pendingOpenTimer);
    pendingOpenTimer = 0;
    wantOpenOnWake = false;
    const cmd = subCmd || pendingSubCmd || '';
    if (typeof window.__tsOpenSub === 'function') {
      pendingSubCmd = '';
      window.__tsOpenSub(API + '/', cmd);
    } else if (retries < 25) {
      pendingSubCmd = cmd;
      pendingOpenTimer = setTimeout(() => openQqMusic(retries + 1, cmd), 200);
    }
  }

  const xzActive = () =>
    (document.body.dataset.state || 'idle') !== 'idle' || document.body.classList.contains('ringing');

  function scheduleTick(delayMs) {
    clearTimeout(tickTimer);
    tickTimer = 0;
    if (document.hidden) return;
    if (xzActive()) lastVoiceBusyAt = Date.now();
    const ms = delayMs !== undefined
      ? delayMs
      : ((xzActive() || Date.now() - lastVoiceBusyAt < 12000) ? 1200 : 6000);
    tickTimer = setTimeout(() => { tickTimer = 0; tick(); }, ms);
  }

  async function tick() {
    if (document.hidden) return;
    if (tickBusy) { tickPending = true; return; }
    tickBusy = true;
    tickPending = false;
    const ctrl = new AbortController();
    const tm = setTimeout(() => ctrl.abort(), 3000);
    try {
      const r = await fetch(API + '/api/music/status?lite=1', { cache: 'no-store', signal: ctrl.signal });
      if (!r.ok) return;
      const st = await r.json();
      if (document.hidden) return;
      const seq = Number(st.open_ui_seq) || 0;
      const fresh = st.open_ui_fresh === undefined ? seq > 0 : st.open_ui_fresh === true;
      if (lastSeq === null || (st.boot_id && lastBootId !== null && st.boot_id !== lastBootId)) {
        const restarted = lastBootId !== null && !!st.boot_id && st.boot_id !== lastBootId;
        lastBootId = st.boot_id || lastBootId;
        lastSeq = seq;
        if (((restarted && seq > 0 && fresh) || (st.open_ui_fresh === true && seq > 0 && lastVoiceBusyAt > 0)) && !st.alarm_ringing && !document.body.classList.contains('ringing')) {
          openQqMusic(0, tabToSubCmd(st.open_ui_tab));
        }
      } else {
        lastBootId = st.boot_id || lastBootId;
        if (seq !== lastSeq) {
          const increased = seq > lastSeq;
          lastSeq = seq;
          if (increased && fresh && !st.alarm_ringing && !document.body.classList.contains('ringing')) {
            openQqMusic(0, tabToSubCmd(st.open_ui_tab));
          }
        }
      }
    } catch (e) {} finally {
      clearTimeout(tm);
      tickBusy = false;
      if (tickPending && !document.hidden) {
        tickPending = false;
        tick();
      } else {
        scheduleTick();
      }
    }
  }

  new MutationObserver(() => {
    if (xzActive()) {
      lastVoiceBusyAt = Date.now();
      scheduleTick(250);
    }
  }).observe(document.body, { attributes: true, attributeFilter: ['data-state', 'class'] });

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      if (wantOpenOnWake) { wantOpenOnWake = false; openQqMusic(); }
      scheduleTick(0);
    } else {
      clearTimeout(tickTimer); tickTimer = 0;
      if (pendingOpenTimer) { clearTimeout(pendingOpenTimer); pendingOpenTimer = 0; wantOpenOnWake = true; }
    }
  });

  tick();
})();
