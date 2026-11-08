// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (C) 2026 Becky Northaven and the Debate Hall contributors
// The keeper's page (11-07, made like AI-Beach's): debates, their paper and roles, who sits where (a person or an
// AI in any role), people and their devices, and invites as a code, a link and a QR code. Seating never matches by
// name: the keeper picks someone already here, or adds someone new (security review).
'use strict'
;(() => {
  const KEY = 'debate-hall.keeper'
  const $ = id => document.getElementById(id)
  let token = null
  let data = null

  function el (tag, attrs, ...kids) {
    const n = document.createElement(tag)
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === 'text') n.textContent = v
      else if (k === 'class') n.className = v
      else if (k.startsWith('on')) n.addEventListener(k.slice(2), v)
      else if (v === true) n.setAttribute(k, '')
      else if (v !== false && v != null) n.setAttribute(k, v)
    }
    for (const kid of kids.flat()) if (kid != null && kid !== false) n.append(kid)
    return n
  }

  async function api (method, path, body) {
    const r = await fetch(path, {
      method,
      headers: Object.assign({ Authorization: 'Bearer ' + token }, body ? { 'Content-Type': 'application/json' } : {}),
      body: body ? JSON.stringify(body) : undefined
    })
    const d = await r.json().catch(() => ({}))
    if (r.status === 401 || r.status === 403) {
      if (path === '/admin/overview') { signOut(); throw new Error(d.detail || 'not the keeper') }
    }
    if (!r.ok) throw new Error(d.message || d.detail || ('error ' + r.status))
    return d
  }

  function signOut () {
    token = null
    try { sessionStorage.removeItem(KEY) } catch (e) {}
    $('main').hidden = true
    $('signin').hidden = false
  }

  const ROLE_WORDS = {
    judge: 'Judge', clerk: 'Clerk', moderator: 'Moderator', advocate: 'Advocate', adversary: 'Adversary',
    reader_a: 'Reader (advocate side)', reader_b: 'Reader (adversary side)', witness: 'Witness',
    aide: "Judge's aide", audience: 'Audience'
  }
  const roleWord = r => ROLE_WORDS[r] || r
  const when = t => t ? new Date(t * 1000).toLocaleString() : 'never'

  // ---- an invite: the code, a link, and a QR code of the link (scanned by a phone; nothing leaves this page)
  function invitePanel (title, inv) {
    const link = location.origin + inv.link
    const box = el('div', { class: 'invite', role: 'status' },
      el('strong', { text: title }),
      el('p', {}, 'Code: ', el('code', { class: 'code', text: inv.code })),
      el('p', {}, 'Or this link: ', el('code', { class: 'link', text: link })),
      el('p', { class: 'hint', text: 'It works once, within 7 days. Give it privately (in person is best). An agent ' +
        'claims it with its helper: hall.py claim <code> --token-file <its own file>.' }))
    if (window.HallQR) {
      try { box.append(el('div', { class: 'qr' }, HallQR.canvas(link, 4))) } catch (e) {}
    }
    box.append(el('button', { type: 'button', class: 'quiet', text: 'Done', onclick: () => box.remove() }))
    return box
  }

  // ---- a debate: title, paper, roles on or off, and its seats
  function debateCard (d) {
    const out = el('div', { class: 'out' })
    const off = new Set(d.roles_off)
    const roleBoxes = data.roles.filter(r => r !== 'judge').map(r => {
      const box = el('input', { type: 'checkbox', id: `r-${d.id}-${r}`, checked: !off.has(r), disabled: d.status === 'closed' })
      box.addEventListener('change', async () => {
        const roles = data.roles.filter(x => x !== 'judge' && !document.getElementById(`r-${d.id}-${x}`).checked)
        try { await api('POST', `/admin/debates/${d.id}/roles-off`, { roles }); await load() } catch (e) { box.checked = !box.checked; alert(e.message) }
      })
      return el('label', { for: `r-${d.id}-${r}`, class: 'pick' }, box, ' ' + roleWord(r))
    })
    const paperSel = el('select', { 'aria-label': 'Paper for ' + d.title, disabled: d.status === 'closed' },
      el('option', { value: '', text: d.paper ? d.paper : 'choose a paper…' }),
      data.papers.filter(p => p !== d.paper).map(p => el('option', { value: p, text: p })))
    paperSel.addEventListener('change', async () => {
      if (!paperSel.value) return
      try { await api('POST', `/admin/debates/${d.id}/paper`, { paper: paperSel.value }); await load() } catch (e) { alert(e.message) }
    })
    const seats = el('table', { class: 'seats' },
      el('thead', {}, el('tr', {}, ['Who', 'Role', 'Person or AI', ''].map(h => el('th', { text: h })))),
      el('tbody', {}, d.seats.length ? d.seats.map(s => el('tr', { class: s.revoked ? 'revoked' : '' },
        el('td', { text: s.name }), el('td', { text: roleWord(s.role) }),
        el('td', { text: s.kind === 'person' ? 'person' : 'AI' }),
        el('td', {}, s.revoked ? el('span', { class: 'hint', text: 'revoked' })
          : el('button', { type: 'button', class: 'quiet', text: 'Revoke', onclick: async () => {
            if (!confirm(`Revoke ${s.name}'s seat? Their sign-in stops reaching this debate at once.`)) return
            try { await api('POST', `/admin/seats/${s.id}/revoke`); await load() } catch (e) { alert(e.message) }
          } }))))
        : [el('tr', {}, el('td', { colspan: '4', class: 'hint', text: 'Nobody is seated yet.' }))]))
    return el('section', { class: 'card' },
      el('h2', { text: `${d.title}` }),
      el('p', { class: 'hint', text: `Debate ${d.id} · ${d.status} · phase: ${d.phase}` }),
      el('p', {}, el('strong', { text: 'Paper: ' }), paperSel),
      el('fieldset', { class: 'roles' }, el('legend', { text: 'Roles this debate uses (the judge always sits)' }), roleBoxes),
      el('h3', { text: 'Seats' }), seats,
      d.status === 'closed' ? null : seatForm(d, out),
      out)
  }

  // Seat someone: pick who's already here, or add someone new. Never matched by name (security review).
  function seatForm (d, out) {
    const off = new Set(d.roles_off)
    const free = data.roles.filter(r => !off.has(r) && !(['judge', 'clerk', 'moderator', 'aide'].includes(r) &&
      d.seats.some(s => s.role === r && !s.revoked)))
    const seated = new Set(d.seats.map(s => s.participant))
    const who = el('select', { 'aria-label': 'Who' },
      el('option', { value: 'new', text: 'someone new…' }),
      data.people.filter(p => !seated.has(p.id) && !p.keeper).map(p =>
        el('option', { value: String(p.id), text: `${p.name} (${p.kind === 'person' ? 'person' : 'AI'}${p.access === 'visitor' ? ', visitor' : ''})` })))
    const role = el('select', { 'aria-label': 'Role' }, free.map(r => el('option', { value: r, text: roleWord(r) })))
    const name = el('input', { type: 'text', maxlength: '32', placeholder: 'name', 'aria-label': 'Name' })
    const kind = el('select', { 'aria-label': 'Person or AI' }, el('option', { value: 'agent', text: 'an AI' }), el('option', { value: 'person', text: 'a person' }))
    const access = el('select', { 'aria-label': 'Resident or visitor' },
      el('option', { value: 'resident', text: 'household (resident)' }), el('option', { value: 'visitor', text: 'a visitor (this debate only)' }))
    const newBits = el('span', { class: 'new-bits' }, name, kind, access)
    who.addEventListener('change', () => { newBits.hidden = who.value !== 'new' })
    const go = el('button', { type: 'submit', text: 'Seat them' })
    const form = el('form', { class: 'row seat-form' }, el('span', { text: 'Seat ' }), who, newBits, el('span', { text: ' as ' }), role, go)
    form.addEventListener('submit', async ev => {
      ev.preventDefault()
      const body = who.value === 'new'
        ? { name: name.value.trim(), kind: kind.value, access: access.value, role: role.value }
        : { participant: Number(who.value), role: role.value }
      try {
        const r = await api('POST', `/admin/debates/${d.id}/seats`, body)
        await load()
        const card = [...document.querySelectorAll('section.card')].find(c => c.dataset.debate === String(d.id))
        ;(card ? card.querySelector('.out') : out).replaceChildren(invitePanel('Their claim code', r))
      } catch (e) { alert(e.message) }
    })
    if (!free.length) return el('p', { class: 'hint', text: 'Every role this debate uses is taken.' })
    return form
  }

  // ---- people and their devices
  function peopleCard () {
    const out = el('div', { class: 'out' })
    const rows = data.people.map(p => {
      const live = p.devices.filter(x => !x.revoked)
      return el('tr', {},
        el('td', {}, el('strong', { text: p.name }), p.keeper ? el('span', { class: 'hint', text: ' (keeper)' }) : null),
        el('td', { text: (p.kind === 'person' ? 'person' : 'AI') + (p.access === 'visitor' ? ', visitor' : '') }),
        el('td', {}, live.length ? el('ul', { class: 'devices' }, live.map(x => el('li', {},
          `${x.label || 'a device'}${x.debate ? ` (debate ${x.debate} only)` : ''}, last used ${when(x.last_used)} `,
          el('button', { type: 'button', class: 'quiet', text: 'Sign out', onclick: async () => {
            if (!confirm(`Sign out this device of ${p.name}'s? It stops working at once.`)) return
            try { await api('POST', `/admin/sign-ins/${x.id}/revoke`); await load() } catch (e) { alert(e.message) }
          } })))) : el('span', { class: 'hint', text: 'no devices' })),
        el('td', {}, el('button', { type: 'button', class: 'quiet', text: 'Invite a device', onclick: async () => {
          let debate = null
          if (p.access === 'visitor') {
            const v = prompt(`${p.name} is a visitor: which debate is this for? (its number)`)
            if (!v) return
            debate = Number(v)
          }
          try { out.replaceChildren(invitePanel(`A code for ${p.name}`, await api('POST', `/admin/people/${p.id}/invite`, debate ? { debate } : {}))) } catch (e) { alert(e.message) }
        } })))
    })
    return el('section', { class: 'card' }, el('h2', { text: 'People' }),
      el('p', { class: 'hint', text: 'Everyone who has a seat, or had one. Each device signs in on its own; signing one out leaves the others.' }),
      el('div', { class: 'table-wrap' }, el('table', {}, el('thead', {}, el('tr', {}, ['Name', '', 'Devices', ''].map(h => el('th', { text: h })))), el('tbody', {}, rows))),
      out)
  }

  function newDebateCard () {
    const title = el('input', { type: 'text', maxlength: '200', placeholder: 'What is being debated?', 'aria-label': 'Title', required: true })
    const paper = el('select', { 'aria-label': 'Paper' }, el('option', { value: '', text: 'paper: choose later' }),
      data.papers.map(p => el('option', { value: p, text: p })))
    const form = el('form', { class: 'row' }, title, paper, el('button', { type: 'submit', text: 'Make the debate' }))
    form.addEventListener('submit', async ev => {
      ev.preventDefault()
      try { await api('POST', '/admin/debates', { title: title.value.trim(), paper: paper.value || null }); await load() } catch (e) { alert(e.message) }
    })
    return el('section', { class: 'card' }, el('h2', { text: 'A new debate' }),
      el('p', { class: 'hint', text: 'Papers are the .md files in the hall\'s debate_docs folder.' }), form)
  }

  async function load () {
    data = await api('GET', '/admin/overview')
    const debates = data.debates.map(d => { const c = debateCard(d); c.dataset.debate = String(d.id); return c })
    $('main').replaceChildren(...[
      el('header', { class: 'keeper-bar' }, el('h1', { text: 'Debate Hall: keeper' }),
        el('span', { class: 'hint', text: 'signed in as ' + data.me.name }),
        el('button', { type: 'button', class: 'quiet', text: 'Sign out of this page', onclick: signOut })),
      newDebateCard(), ...debates, peopleCard()
    ].filter(Boolean))
    $('main').hidden = false
  }

  $('signin-form').addEventListener('submit', async ev => {
    ev.preventDefault()
    $('signin-error').textContent = ''
    const value = $('code').value.trim()
    try {
      if (/^[0-9a-f]{4}(-[0-9a-f]{4}){3}$/i.test(value)) {
        const r = await fetch('/claim', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code: value }) })
        const d = await r.json().catch(() => ({}))
        if (!r.ok) throw new Error(d.message || d.detail || 'that code did not work')
        token = d.token
      } else token = value
      await load()
      try { sessionStorage.setItem(KEY, token) } catch (e) {}
      $('signin').hidden = true
    } catch (e) { token = null; $('signin-error').textContent = e.message }
  })

  try { token = sessionStorage.getItem(KEY) } catch (e) {}
  if (token) load().then(() => { $('signin').hidden = true }).catch(() => signOut())
  else signOut()
})()
