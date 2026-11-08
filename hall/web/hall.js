// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (C) 2026 Becky Northaven and the Debate Hall contributors
// The Debate Hall page. The seat token lives in this tab's sessionStorage and goes only in an Authorization
// header (never a cookie, never a URL). Everyone's words are put on the page as text, never as HTML; the paper
// is the only HTML, rendered by the hall from Markdown with raw HTML switched off.
'use strict'
;(function () {
  const $ = id => document.getElementById(id)
  const KEY = 'debateHall.token'
  const ROOM_NAMES = { hall: 'Hall', gallery: 'Gallery', team_a: 'Team A huddle', team_b: 'Team B huddle', aide: "Judge's aide" }
  let token = null
  try { token = sessionStorage.getItem(KEY) } catch (e) {}
  // An invite link from the keeper page: /#c=<code>&d=<debate> (11-07). The code goes into the sign-in box and the
  // fragment is cleared at once, so it isn't left in the address bar; the debate is remembered for this tab.
  const hashArgs = new URLSearchParams(location.hash.slice(1))
  const linkCode = hashArgs.get('c')
  let debate = hashArgs.get('d') || null
  try { if (debate) sessionStorage.setItem(KEY + '.debate', debate); else debate = sessionStorage.getItem(KEY + '.debate') } catch (e) {}
  if (location.hash) history.replaceState(null, '', location.pathname + location.search)
  if (linkCode) token = null
  const auth = () => Object.assign({ Authorization: 'Bearer ' + token }, debate ? { 'X-Debate': debate } : {})
  let me = null
  let room = 'hall'
  let after = 0
  let lines = []
  const unread = new Set()
  let finds = []
  let findAt = -1

  function el (tag, attrs, ...kids) {
    const n = document.createElement(tag)
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === 'text') n.textContent = v
      else if (k === 'class') n.className = v
      else if (k.startsWith('on')) n.addEventListener(k.slice(2), v)
      else n.setAttribute(k, v)
    }
    for (const kid of kids) if (kid != null) n.append(kid)
    return n
  }

  async function api (method, path, body) {
    const r = await fetch(path, {
      method,
      headers: Object.assign(auth(), body ? { 'Content-Type': 'application/json' } : {}),
      body: body ? JSON.stringify(body) : undefined
    })
    if (r.status === 401) { signOut(); throw new Error('That token is not a seat here.') }
    const data = await r.json().catch(() => ({}))
    if (!r.ok) throw new Error(data.message || data.detail || ('error ' + r.status))
    return data
  }

  async function download (path, name) {
    const r = await fetch(path, { headers: auth() })
    if (!r.ok) {             // a refusal is a message to show, never a file to save (the keeper, 10-30)
      const d = await r.json().catch(() => ({}))
      alert('The download did not work: ' + (d.message || d.detail || r.status))
      return
    }
    const blob = await r.blob()
    const a = el('a', { href: URL.createObjectURL(blob), download: name })
    document.body.append(a); a.click(); a.remove()
    setTimeout(() => URL.revokeObjectURL(a.href), 1000)
  }

  function signOut () {
    try { sessionStorage.removeItem(KEY) } catch (e) {}
    token = null
    location.reload()
  }

  // The format, so a judge new to this can see what pressing Continue sets in motion (the keeper, 10-25).
  const PHASES = ['setup', 'openings', 'objections', 'rebuttals', 'rulings', 'closings', 'done']
  const PHASE_WORDS = {
    setup: 'getting ready',
    openings: "each side's opening statement",
    objections: 'the adversary files objections; the advocate answers them',
    rebuttals: "each side answers the other's points",
    rulings: 'the witness checks disputed wording against the paper; then you rule on what is still open',
    closings: "each side's closing statement",
    done: 'the debate is over; you close it'
  }

  function nextStep (f) {
    if (f.status === 'closed') return ''
    const i = PHASES.indexOf(f.phase)
    const next = PHASES[i + 1]
    const now = 'Now: ' + f.phase + ' (' + PHASE_WORDS[f.phase] + ').'
    if (f.holder) return now + ' ' + f.holder + ' has the floor.'
    if (f.queue.length) return now + ' Next, likely: the moderator gives the floor to ' + f.queue[0] + ' (asked first).'
    if (next) return now + ' Next, likely: the moderator opens ' + next + ' (' + PHASE_WORDS[next] + ').'
    return now
  }

  // ------------------------------------------------------------------ room to read (the keeper, 10-25)
  // Text size, the debate/paper divider, and the paper in its own window (e.g. a second monitor). Sizes are kept
  // in this browser; the paper's window and this page talk over a same-site BroadcastChannel.
  const SCALE_KEY = 'debateHall.scale'
  const SIDE_KEY = 'debateHall.sideWidth'
  const PAPER_KEY = 'debateHall.paperWidth'
  const SIDE_HIDDEN_KEY = 'debateHall.sideHidden'
  let popped = false
  let channel = null
  const store = {
    get: k => { try { return localStorage.getItem(k) } catch (e) { return null } },
    set: (k, v) => { try { localStorage.setItem(k, v) } catch (e) {} }
  }

  function setScale (v) {
    const s = Math.min(2.2, Math.max(0.8, Math.round(v * 10) / 10))
    document.body.style.setProperty('--scale', s)
    store.set(SCALE_KEY, String(s))
  }

  // A column's width as a share of the row, kept in this browser. The side panel's divider sits on its right
  // edge, the paper's on its left, so a divider always moves the way you drag it (or press the arrow keys).
  const WIDTHS = {
    side: { key: SIDE_KEY, prop: '--side', div: 'divider-side', min: 15, max: 50, dflt: 26, fromLeft: true },
    paper: { key: PAPER_KEY, prop: '--paperw', div: 'divider', min: 20, max: 60, dflt: 36, fromLeft: false }
  }

  function setWidth (which, pct) {
    const w = WIDTHS[which]
    const p = Math.min(w.max, Math.max(w.min, Math.round(pct)))
    document.querySelector('.cols').style.setProperty(w.prop, p + '%')
    $(w.div).setAttribute('aria-valuenow', String(p))
    store.set(w.key, String(p))
  }

  function wireDivider (which) {
    const w = WIDTHS[which]
    const div = $(w.div)
    const now = () => parseFloat(div.getAttribute('aria-valuenow')) || w.dflt
    div.addEventListener('pointerdown', ev => {
      ev.preventDefault()
      div.setPointerCapture(ev.pointerId)
      const cols = document.querySelector('.cols').getBoundingClientRect()
      const move = e => setWidth(which, (w.fromLeft ? e.clientX - cols.left : cols.right - e.clientX) / cols.width * 100)
      const up = () => { div.removeEventListener('pointermove', move); div.removeEventListener('pointerup', up) }
      div.addEventListener('pointermove', move)
      div.addEventListener('pointerup', up)
    })
    div.addEventListener('keydown', ev => {
      const step = (ev.key === 'ArrowRight' ? 5 : ev.key === 'ArrowLeft' ? -5 : 0) * (w.fromLeft ? 1 : -1)
      if (step) { ev.preventDefault(); setWidth(which, now() + step) }
    })
  }

  function setSideHidden (hidden) {
    document.querySelector('.cols').classList.toggle('side-hidden', hidden)
    $('toggle-side').textContent = hidden ? 'Show the ledger panel' : 'Hide the ledger panel'
    $('toggle-side').setAttribute('aria-expanded', String(!hidden))
    store.set(SIDE_HIDDEN_KEY, hidden ? '1' : '0')
  }

  function wireRoom () {
    setScale(parseFloat(store.get(SCALE_KEY)) || 1)
    $('smaller').addEventListener('click', () => setScale((parseFloat(store.get(SCALE_KEY)) || 1) - 0.1))
    $('bigger').addEventListener('click', () => setScale((parseFloat(store.get(SCALE_KEY)) || 1) + 0.1))
    for (const which of Object.keys(WIDTHS)) {
      setWidth(which, parseFloat(store.get(WIDTHS[which].key)) || WIDTHS[which].dflt)
      wireDivider(which)
    }
    setSideHidden(store.get(SIDE_HIDDEN_KEY) === '1')
    $('toggle-side').addEventListener('click', () =>
      setSideHidden(!document.querySelector('.cols').classList.contains('side-hidden')))
    if ('BroadcastChannel' in window) {
      channel = new BroadcastChannel('debateHall.paper')
      channel.addEventListener('message', ev => {
        if (ev.data && ev.data.closed) setPopped(false)
        if (ev.data && ev.data.hello) setPopped(true)
      })
    }
    $('pop-out').addEventListener('click', () => {
      // The paper's window gets this tab's sign-in (a window opened this way starts with a copy of it).
      const w = window.open('/web/paper.html', 'debateHallPaper', 'width=900,height=1000')
      if (w) setPopped(true)
    })
    $('pop-back').addEventListener('click', () => {
      channel && channel.postMessage({ close: true })
      setPopped(false)
    })
  }

  function setPopped (on) {
    popped = on
    document.querySelector('.cols').classList.toggle('popped', on)
    $('popped-note').hidden = !on
  }

  let wasWaiting = false
  const CHIME_KEY = 'debateHall.chime'
  function chimeOn () {
    try { return localStorage.getItem(CHIME_KEY) === 'on' } catch (e) { return false }
  }
  function chime () {
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)()
      const o = ctx.createOscillator()
      const g = ctx.createGain()
      o.frequency.value = 660
      g.gain.setValueAtTime(0.0001, ctx.currentTime)
      g.gain.exponentialRampToValueAtTime(0.15, ctx.currentTime + 0.02)
      g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.6)
      o.connect(g).connect(ctx.destination)
      o.start()
      o.stop(ctx.currentTime + 0.65)
    } catch (e) {}
  }

  const when = t => new Date(t * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })

  // ------------------------------------------------------------------ floor and header
  function showFloor (f) {
    $('phase').textContent = 'Phase: ' + f.phase + (f.status === 'closed' ? ' (closed)' : '')
    // Who may speak in the Hall right now, in so many words (an advocate, 10-25).
    let floor = f.status === 'closed' ? 'The debate is closed'
      : f.huddle ? 'Huddle time: the Hall waits while both teams confer (Team A ' + f.huddle.team_a + ', Team B ' + f.huddle.team_b + '; until ' + when(f.huddle.ends_at) + ')'
      : f.holder ? 'Speaking: ' + f.holder + ' and the chair' : 'Speaking: only the chair (the floor is free)'
    if (f.holder && f.until) floor += ' (floor until ' + when(f.until) + ')'
    $('floor').textContent = floor
    $('queue').textContent = f.queue.length ? 'Asking for the floor: ' + f.queue.join(', ') : ''
    const mine = me && f.holder === me.seat.name
    $('yield').hidden = !mine
    const asking = !!me && f.queue.includes(me.seat.name)       // Security review: take back an ask that's become moot
    $('withdraw').hidden = !asking
    $('req').hidden = asking || mine
    // The judge paces (the keeper, 10-25): when the debate waits on them, they see it plainly; everyone else sees why.
    const judge = me && me.seat.role === 'judge'
    $('close-row').hidden = !judge || f.status === 'closed'      // closed once: no second Close (debrief, 10-30)
    $('draft-all-row').hidden = !judge || f.status === 'closed'
    $('huddle-row').hidden = !(me && ['judge', 'moderator'].includes(me.seat.role)) || f.status === 'closed'

    const banner = $('waiting')
    banner.hidden = !f.waiting
    const step = nextStep(f)
    if (f.waiting) {
      banner.replaceChildren(judge
        ? 'Waiting on your Continue (or another action).'
        : 'Waiting on the judge to continue.')
      if (judge) banner.append(el('button', { type: 'button', text: 'Moderator, please continue', onclick: () => $('nudge').click() }))
      if (step) banner.append(el('div', { class: 'next-step', text: step }))
    }
    $('next-step').hidden = !judge || !step
    $('next-step').textContent = step
    $('nudge').classList.toggle('urgent', !!f.waiting)
    // An advocate: if the judge looks away, a waiting hall stalls quietly. The tab's title says so, and an optional chime.
    if (judge) {
      document.title = f.waiting ? '⏸ Waiting on you · Debate Hall' : (me ? me.debate.title + ' · Debate Hall' : 'Debate Hall')
      if (f.waiting && !wasWaiting && chimeOn()) chime()
      wasWaiting = !!f.waiting
    }
    if (judge) $('paced').checked = !!f.paced
    $('req').hidden = mine || !me || ['judge', 'moderator', 'aide', 'audience'].includes(me.seat.role)
  }

  // ------------------------------------------------------------------ rooms and the log
  function tabs () {
    const nav = $('tabs')
    nav.replaceChildren(...me.rooms.map(r => el('button', {
      type: 'button', role: 'tab', 'aria-selected': String(r === room),
      // The judge/moderator room asks for attention loudly when it has something unread (the keeper, 10-25).
      class: (unread.has(r) ? 'unread' : '') + (unread.has(r) && r === 'chair' ? ' alert' : '') + ' tab-' + r,
      'aria-label': (r === 'chair' ? roomName(r) : (ROOM_NAMES[r] || r)) + (unread.has(r) ? ' (new)' : ''),
      text: r === 'chair' ? roomName(r) : (ROOM_NAMES[r] || r), onclick: () => { room = r; unread.delete(r); tabs(); drawLog() }
    })))
  }

  // Who hears you in each room, said plainly above the message box, in the room's color.
  const HEARS = {
    hall: 'HALL: everyone in the debate hears this',
    gallery: 'GALLERY: the audience and the moderator',
    team_a: "TEAM A HUDDLE: your team only (until the debate ends)",
    team_b: "TEAM B HUDDLE: your team only (until the debate ends)",
    aide: 'PRIVATE: only you and your aide'
  }
  const PLACEHOLDER = {
    hall: 'Speak to the whole hall…', gallery: 'Say something in the Gallery…', team_a: 'To your team…',
    team_b: 'To your team…', aide: 'Privately, to your aide…', chair: 'Privately, about running the debate…'
  }

  // The judge/moderator room is named for the other person in it; the clerk asks them both there (the keeper, 10-30).
  function roomName (r) {
    if (r !== 'chair') return ROOM_NAMES[r] || r
    if (me && me.seat.role === 'clerk') return 'Judge & Moderator'
    return me && me.seat.role === 'moderator' ? 'Judge' : 'Moderator'
  }

  function markRoom () {
    document.querySelector('.left').dataset.room = room
    // The audience watches the Hall and asks in the Gallery; the moderator brings questions to the floor (10-30:
    // a Hall message box for the audience only ever answered with a refusal).
    const watching = me && me.seat.role === 'audience' && room === 'hall'
    $('text').hidden = $('say').querySelector('button[type=submit]').hidden = watching
    $('audience-note').textContent = watching
      ? 'You are watching the Hall. To ask a question, use the Gallery tab: the moderator brings questions to the floor.'
      : room === 'chair'
        ? 'PRIVATE: the judge, the moderator and the clerk (about running the debate; the clerk asks here)'
        : (HEARS[room] || room)
    $('text').placeholder = PLACEHOLDER[room] || 'Speak…'
  }

  function drawLog () {
    markRoom()
    const log = $('log')
    const mine = lines.filter(m => m.room === room)
    log.setAttribute('aria-live', 'off')
    log.replaceChildren(...mine.map(lineEl))
    log.setAttribute('aria-live', 'polite')
    log.scrollTop = log.scrollHeight
  }

  function lineEl (m) {
    // The moderator can bring an audience question from the Gallery into the Hall (the keeper, 10-25).
    const relay = me && me.seat.role === 'moderator' && m.room === 'gallery' && m.kind === 'say' && m.role === 'audience'
      ? el('button', { type: 'button', class: 'quiet', text: 'Bring to the Hall', onclick: ev => {
        api('POST', '/relay', { id: m.id }).then(() => { ev.target.disabled = true }).catch(e => { $('say-error').textContent = e.message })
      } })
      : null
    return el('li', { class: m.kind },
      el('span', { class: 'when', text: when(m.at) }),
      el('span', { class: 'who', text: m.who }),
      m.role ? el('span', { class: 'role', text: m.role }) : null,
      el('span', { class: 'body', text: m.text }), relay)
  }

  // replay: lines already in the log (the first load, or a reload) never trigger a reload themselves.
  function take (items, replay) {
    let ledgerChanged = false
    let roleChanged = false
    for (const m of items) {
      lines.push(m)
      after = Math.max(after, m.id)
      if (m.room === room) {
        const log = $('log')
        const atEnd = log.scrollHeight - log.scrollTop - log.clientHeight < 40
        log.append(lineEl(m))
        if (atEnd) log.scrollTop = log.scrollHeight
      } else {
        unread.add(m.room)
      }
      if (m.kind === 'show') {
        if (popped) channel && channel.postMessage({ show: m.data })   // the paper's window follows on its own terms
        else if ($('follow').checked) point(m.data)
      }
      if (['objection', 'answer', 'concede', 'draft', 'ruling'].includes(m.kind)) ledgerChanged = true
      if (!replay && m.data && (m.data.paper || m.data.status)) { loadPaper(); refreshMe() }
      if (!replay && m.data && m.data.role_change) roleChanged = true
    }
    if (roleChanged) {   // a new role can mean different rooms: start the log over, once, after this batch
      refreshMe().then(() => api('GET', '/feed?after=0&limit=500')).then(r => {
        lines = []
        after = 0
        take(r.items, true)
        drawLog()
      }).catch(() => {})
      return
    }
    if (items.length) tabs()
    if (ledgerChanged) loadLedger()
  }

  async function listen () {
    for (;;) {
      try {
        const r = await api('GET', '/wait?after=' + after + '&timeout=25')
        showFloor(r.floor)
        take(r.items)
      } catch (e) {
        if (!token) return
        await new Promise(resolve => setTimeout(resolve, 3000))
      }
    }
  }

  // ------------------------------------------------------------------ the paper
  async function loadPaper () {
    const r = await api('GET', '/paper').catch(e => ({ error: e.message }))
    const art = $('paper')
    if (r.error) { art.replaceChildren(el('p', { class: 'error', text: r.error })); return }
    if (!r.paper) return
    art.innerHTML = r.html   // rendered by the hall with raw HTML off; see render_paper
    $('paper-ver').textContent = r.paper + ' · version ' + r.sha.slice(0, 12)
    $('toc').replaceChildren(...r.headings.map(h => el('a', {
      href: '#' + h.id, class: 'l' + h.level, text: h.title,
      onclick: ev => { ev.preventDefault(); point({ id: h.id }) }
    })))
  }

  function blocks () {
    return Array.from($('paper').querySelectorAll('p, li, td, th, h1, h2, h3, h4, h5, h6, blockquote, pre'))
  }

  const norm = s => s.toLowerCase().replace(/\s+/g, ' ')

  function point (target) {
    const art = $('paper')
    art.querySelectorAll('.hl').forEach(n => n.classList.remove('hl'))
    let node = null
    if (target.id) node = document.getElementById(target.id)
    if (target.section) node = document.getElementById('s-' + target.section.replace(/^§/, ''))
    if (target.phrase) {
      const want = norm(target.phrase)
      node = blocks().find(b => norm(b.textContent).includes(want)) || null
    }
    if (node) {
      node.classList.add('hl')
      node.scrollIntoView({ block: 'center', behavior: 'smooth' })
    }
    return !!node
  }

  function find (next) {
    const q = norm($('find').value.trim())
    $('paper').querySelectorAll('.found').forEach(n => n.classList.remove('found'))
    if (!q) { finds = []; $('find-count').textContent = ''; return }
    if (!next) {
      finds = blocks().filter(b => norm(b.textContent).includes(q))
      findAt = -1
    }
    finds.forEach(n => n.classList.add('found'))
    $('find-count').textContent = finds.length ? finds.length + ' found' : 'not found'
    if (finds.length) {
      findAt = (findAt + 1) % finds.length
      finds[findAt].scrollIntoView({ block: 'center' })
    }
  }

  // ------------------------------------------------------------------ the ledger
  async function loadLedger () {
    const r = await api('GET', '/ledger').catch(() => null)
    if (!r) return
    const ready = r.objections.filter(o => o.revision_ready).length
    $('ledger-count').textContent = r.objections.length
      ? '(' + r.objections.length + (ready ? ' · ' + ready + ' revision' + (ready > 1 ? 's' : '') + ' ready' : '') + ')'
      : ''
    // Keep what's being typed across the redraw (the keeper, a debate on 10-30: a ruling's wording vanished when a
    // reply arrived mid-typing, so the ruling went in with no text and adopted the clerk's draft instead).
    const typed = {}
    let focused = null
    let caret = null
    for (const i of $('ledger').querySelectorAll('input[aria-label]')) {
      const k = i.getAttribute('aria-label')
      if (i.value) typed[k] = i.value
      if (document.activeElement === i) { focused = k; caret = i.selectionStart }
    }
    $('ledger').replaceChildren(...r.objections.map(objEl))
    for (const i of $('ledger').querySelectorAll('input[aria-label]')) {
      const k = i.getAttribute('aria-label')
      if (typed[k]) i.value = typed[k]
      if (k === focused) { i.focus(); if (caret !== null) i.setSelectionRange(caret, caret) }
    }
  }

  const STATUS_WORDS = {
    open: 'open', answered: 'answered', partial: 'partly conceded (rewording offered)', conceded: 'conceded',
    sustained: 'ruled: objection is right', overruled: 'ruled: paper stands', revise: 'ruled: fix with wording'
  }

  const LINKERS = ['advocate', 'adversary', 'reader_a', 'reader_b', 'witness', 'clerk', 'judge']

  // Each objection folds (the keeper's debrief, 10-30): open or closed stays as the reader left it across redraws,
  // new ones start open unless already ruled, and the heading with its number stays in view while it's read (the
  // keeper read #2 as #1 when the number had scrolled away).
  const openState = new Map()
  function collapsible (li, o) {
    const head = li.firstChild
    const sum = el('summary', { class: 'ob-head' })
    while (head.firstChild) sum.append(head.firstChild)
    head.remove()
    const d = el('details', { class: 'ob' }, sum)
    while (li.firstChild) d.append(li.firstChild)
    d.open = openState.has(o.id) ? openState.get(o.id) : !['sustained', 'overruled', 'revise'].includes(o.status)
    d.addEventListener('toggle', () => openState.set(o.id, d.open))
    li.append(d)
    return li
  }

  function objEl (o) {
    const role = me.seat.role
    // The heading takes you to the passage (the keeper, 10-25: in a long paper the ledger is how you navigate):
    // the quoted sentence when there is one, otherwise the section.
    const goTo = () => {
      const section = String(o.anchor).replace(/^§\s*/, '')
      if (popped) { channel && channel.postMessage({ go: { phrase: o.quote || null, section } }); return }
      if (!(o.quote && point({ phrase: o.quote }))) point({ section })
    }
    // Links between objections (the keeper, 10-25: "complex topics may have complex objections"): each number jumps
    // to that entry in the ledger.
    const jump = n => el('button', { type: 'button', class: 'link-to', text: '#' + n, onclick: () => {
      const t = $('ob-' + n)
      if (t) { t.scrollIntoView({ block: 'start', behavior: 'smooth' }); t.classList.add('flash'); setTimeout(() => t.classList.remove('flash'), 1500) }
    } })
    const links = (word, nums) => nums && nums.length
      ? el('div', { class: 'links' }, word + ' ', ...nums.flatMap((n, i) => i ? [', ', jump(n)] : [jump(n)]))
      : null
    const li = el('li', { id: 'ob-' + o.id },
      el('div', {},
        el('button', { type: 'button', class: 'anchor-link', title: 'Show this passage in the paper',
          text: '#' + o.id + ' at ' + o.anchor, onclick: ev => { ev.preventDefault(); goTo() } }),
        ' ', el('span', { class: 'status ' + o.status, text: STATUS_WORDS[o.status] || o.status }),
        // the keeper: "I should know when there's a revision ready" (never where the idea came from).
        o.revision_ready ? el('span', { class: 'ready', text: 'Revision ready' }) : null,
        // A clerk's draft written before a later answer or reply (debrief, 10-30).
        o.draft_stale ? el('span', { class: 'stale', text: "Draft is older than " + o.draft_stale.since + "'s " + o.draft_stale.kind }) : null,
        el('span', { class: 'hint', text: ' by ' + o.who })),
      links('Depends on', o.depends_on),
      links('Needed by', o.needed_by),
      el('div', { text: 'Claim attacked: ' + o.claim }),
      el('div', { text: 'Why: ' + o.why }),
      o.quote ? el('blockquote', { text: o.quote }) : null,
      el('ul', { class: 'events' }, ...o.events.map(e => el('li', {
        text: e.who + ', ' + e.kind + (e.verdict ? ' (' + e.verdict + ')' : '') + ': ' + e.text
      }))))
    const ruled = ['sustained', 'overruled', 'revise'].includes(o.status)
    const act = el('div', { class: 'act' })
    const input = el('input', { 'aria-label': 'Text for objection #' + o.id })
    const err = el('span', { class: 'error', role: 'alert' })
    const go = (path, body) => api('POST', '/objections/' + o.id + '/' + path, body).then(() => { input.value = ''; return loadLedger() }).catch(e => { err.textContent = e.message })
    if (!ruled && (['advocate', 'reader_a', 'witness'].includes(role))) {
      act.append(input, el('button', { type: 'button', text: 'Answer', onclick: () => go('answer', { text: input.value }) }))
      if (role === 'advocate') act.append(el('button', { type: 'button', text: 'Concede', onclick: () => go('answer', { text: input.value, concede: true }) }))
    }
    // The objecting side's one reply to each new answer (security review), kept with the objection.
    if (!ruled && ['adversary', 'reader_b'].includes(role) && o.events.length && o.events[o.events.length - 1].kind !== 'reply') {
      act.append(input, el('button', { type: 'button', text: 'Reply to the answer', onclick: () => go('reply', { text: input.value }) }))
    }
    // Plain words on the buttons (the keeper, 10-25: the courtroom words read backwards mid-debate).
    const PLAIN = {
      overruled: ['Paper stands (overrule)', 'The objection is wrong: no change to the paper.'],
      revise: ['Fix it with this wording (revise)', "The objection has a point and the fix is known: usually the advocate's proposed text."],
      sustained: ['Objection is right (sustain)', "The claim can't stand as written; how to fix it is still open."]
    }
    for (const v of ['overruled', 'revise', 'sustained']) {
      const [label, help] = PLAIN[v]
      if (role === 'clerk') act.append(el('button', { type: 'button', title: help, text: 'Draft: ' + label, onclick: () => go('draft', { verdict: v, text: input.value }) }))
      if (role === 'judge') act.append(el('button', { type: 'button', title: help, text: label, onclick: () => go('rule', { verdict: v, text: input.value }) }))
    }
    if (role === 'judge' && !ruled) {
      act.append(el('button', { type: 'button', class: 'quiet', text: 'Clerk, draft now',
        onclick: () => api('POST', '/draft-now', { objection: o.id }).catch(e => { err.textContent = e.message }) }))
    }
    if (role === 'judge' || role === 'clerk') {
      act.append(el('p', { class: 'hint rule-help', text: ruled
        ? 'Ruled. You can rule again; the latest ruling counts, and the earlier one stays in the history.'
        : 'Paper fine: Paper stands. Paper wrong and the proposed wording fixes it: Fix it. Paper wrong, fix not yet known: Objection is right. A reason is optional.' }))
    }
    if (role === 'clerk' || role === 'judge') {
      act.prepend(input)
      // An advocate: a ruling's text is most useful as what the revision must do.
      input.placeholder = role === 'judge' ? 'Reason, or what the revision must do (optional)' : 'Evidence, and what the revision must do'
    }
    if (act.childNodes.length) li.append(act, err)
    if (LINKERS.includes(role) && me.floor.status !== 'closed') {
      const on = el('input', { type: 'number', min: '1', class: 'link-num', 'aria-label': 'Objection #' + o.id + ' depends on objection number' })
      li.append(el('div', { class: 'row link-row' },
        el('span', { class: 'hint', text: 'This depends on #' }), on,
        el('button', { type: 'button', class: 'quiet', text: 'Link', onclick: () => go('link', { depends_on: Number(on.value) }) }),
        el('button', { type: 'button', class: 'quiet', text: 'Unlink', onclick: () => go('link', { depends_on: Number(on.value), remove: true }) })))
    }
    return collapsible(li, o)
  }

  // ------------------------------------------------------------------ start
  async function refreshMe () {
    me = await api('GET', '/me')
    $('title').textContent = me.debate.title
    $('me').textContent = me.seat.name + ' · ' + me.seat.role
    if (!me.rooms.includes(room)) room = 'hall'
    tabs()
    showFloor(me.floor)
    const chair = ['judge', 'moderator'].includes(me.seat.role)
    $('chair').hidden = !chair
    $('close-row').hidden = me.seat.role !== 'judge'
    $('role-row').hidden = me.seat.role !== 'judge'
    $('nudge').hidden = me.seat.role !== 'judge'
    $('pace-row').hidden = me.seat.role !== 'judge'
    $('role-who').replaceChildren(...me.seats.filter(s => s.role !== 'judge').map(s => el('option', { value: s.name, text: s.name + ' (' + s.role + ')' })))
    $('give-who').replaceChildren(...me.seats.filter(s => s.role !== 'audience').map(s => el('option', { value: s.name, text: s.name + ' (' + s.role + ')' })))
    $('object').hidden = ['judge', 'clerk', 'moderator', 'aide', 'audience'].includes(me.seat.role) || me.floor.status === 'closed'
    // The audience door serves no exports; the downloads are for the seats on the hall's own computer.
    $('export-ledger').hidden = $('export-log').hidden = me.seat.role === 'audience'
  }

  async function start () {
    if (!token) { $('signin').hidden = false; if (linkCode) $('token').value = linkCode; $('token').focus(); return }
    try { await refreshMe() } catch (e) { return }
    $('app').hidden = false
    const first = await api('GET', '/feed?after=0&limit=500')
    take(first.items, true)
    drawLog()
    loadPaper()
    loadLedger()
    listen()
  }

  function wire () {
    $('signin-form').addEventListener('submit', async ev => {
      ev.preventDefault()
      let value = $('token').value.trim()
      // A claim code (XXXX-XXXX-XXXX-XXXX) swaps once for the seat's token, so a person never handles the token
      // itself (security review, 10-28).
      if (/^[0-9a-f]{4}(-[0-9a-f]{4}){3}$/i.test(value)) {
        try {
          const r = await fetch('/claim', { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ code: value }) })
          const d = await r.json()
          if (!r.ok) throw new Error(d.message || d.detail || 'that claim code did not work')
          value = d.token
        } catch (e) { $('signin-error').textContent = e.message; return }
      }
      token = value
      try { sessionStorage.setItem(KEY, token) } catch (e) {}
      $('signin').hidden = true
      start()
    })
    $('signout').addEventListener('click', signOut)
    $('say').addEventListener('submit', async ev => {
      ev.preventDefault()
      const text = $('text').value.trim()
      if (!text) return
      $('say-error').textContent = ''
      try {
        if (text.startsWith('/show ')) await api('POST', '/show', { target: text.slice(6).trim() })
        else await api('POST', '/say', { room, text })
        $('text').value = ''
      } catch (e) { $('say-error').textContent = e.message }
    })
    $('text').addEventListener('keydown', ev => {
      if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); $('say').requestSubmit() }
    })
    $('chime').checked = chimeOn()
    $('chime').addEventListener('change', ev => {
      try { localStorage.setItem(CHIME_KEY, ev.target.checked ? 'on' : 'off') } catch (e) {}
      if (ev.target.checked) chime()      // a sample, so you know what it sounds like
    })
    $('paced').addEventListener('change', ev => chairDo(api('POST', '/pacing', { on: ev.target.checked })))
    $('nudge').addEventListener('click', () => api('POST', '/nudge').then(showFloor).catch(e => { $('say-error').textContent = e.message }))
    $('req').addEventListener('click', () => api('POST', '/floor/request').then(showFloor).catch(e => { $('say-error').textContent = e.message }))
    $('yield').addEventListener('click', () => api('POST', '/floor/yield').then(showFloor).catch(e => { $('say-error').textContent = e.message }))
    $('withdraw').addEventListener('click', () => api('POST', '/floor/withdraw').then(showFloor).catch(e => { $('say-error').textContent = e.message }))
    const chairDo = p => p.then(r => { if (r && r.phase) showFloor(r) }).catch(e => { $('chair-error').textContent = e.message })
    $('give').addEventListener('click', () => chairDo(api('POST', '/floor/give', { name: $('give-who').value, minutes: $('give-min').value || null })))
    $('take').addEventListener('click', () => chairDo(api('POST', '/floor/take')))
    $('set-phase').addEventListener('click', () => chairDo(api('POST', '/phase', { phase: $('phase-pick').value })))
    $('set-paper').addEventListener('click', () => chairDo(api('POST', '/paper', { path: $('paper-path').value.trim() })))
    $('set-role').addEventListener('click', () => {
      const who = $('role-who').value
      const role = $('role-new').value
      if (confirm('Make ' + who + ' the ' + role + '?')) chairDo(api('POST', '/seats/role', { name: who, role }).then(refreshMe))
    })
    $('draft-all').addEventListener('click', () => chairDo(api('POST', '/draft-now', {})))
    // Huddle time (the keeper, 11-07): the chair grants a block; the Hall waits while both halves of each team confer.
    $('huddle-call').addEventListener('click', () => chairDo(api('POST', '/huddle/call', { minutes: Number($('huddle-min').value) || 10 })))
    $('huddle-end').addEventListener('click', () => chairDo(api('POST', '/huddle/end')))
    $('close').addEventListener('click', () => {
      if (confirm('Close the debate? Speaking ends, and everyone can read the huddles (no one can post in them).')) chairDo(api('POST', '/close'))
    })
    $('object').addEventListener('submit', async ev => {
      ev.preventDefault()
      $('ob-error').textContent = ''
      try {
        const deps = $('ob-deps').value.split(/[\s,#]+/).filter(Boolean).map(Number)
        await api('POST', '/objections', { anchor: $('ob-anchor').value, quote: $('ob-quote').value, claim: $('ob-claim').value, why: $('ob-why').value, depends_on: deps })
        for (const id of ['ob-anchor', 'ob-quote', 'ob-claim', 'ob-why', 'ob-deps']) $(id).value = ''
        loadLedger()
      } catch (e) { $('ob-error').textContent = e.message }
    })
    $('find').addEventListener('input', () => find(false))
    $('find').addEventListener('keydown', ev => { if (ev.key === 'Enter') { ev.preventDefault(); find(true) } })
    $('export-ledger').addEventListener('click', () => download('/export/ledger.md', 'ledger.md'))
    $('export-log').addEventListener('click', () => download('/export/transcript.md', 'transcript.md'))
  }

  wire()
  wireRoom()
  start()
})()
