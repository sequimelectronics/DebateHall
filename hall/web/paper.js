// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (C) 2026 Becky Northaven and the Debate Hall contributors
// The paper in its own window (the keeper, 10-25: room to read, e.g. on a second monitor). It signs in with the copy of
// the hall tab's session that a window opened from it starts with, follows the speaker's /show and the ledger's
// links over a same-site BroadcastChannel, and hands the paper back to the hall page when it closes.
'use strict'
;(function () {
  const $ = id => document.getElementById(id)
  let token = null
  try { token = sessionStorage.getItem('debateHall.token') } catch (e) {}
  const channel = 'BroadcastChannel' in window ? new BroadcastChannel('debateHall.paper') : null
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

  function scale (v) {
    const s = Math.min(2.2, Math.max(0.8, Math.round(v * 10) / 10))
    document.body.style.setProperty('--scale', s)
    try { localStorage.setItem('debateHall.scale', String(s)) } catch (e) {}
  }
  const currentScale = () => { try { return parseFloat(localStorage.getItem('debateHall.scale')) || 1 } catch (e) { return 1 } }

  async function load () {
    const art = $('paper')
    if (!token) {
      art.replaceChildren(el('p', { class: 'error', text: 'Open this window from the hall page ("Open in its own window").' }))
      return
    }
    try {
      let debate = null
      try { debate = sessionStorage.getItem('debateHall.token.debate') } catch (e) {}
      const r = await fetch('/paper', { headers: Object.assign({ Authorization: 'Bearer ' + token }, debate ? { 'X-Debate': debate } : {}) })
      const d = await r.json()
      if (!r.ok) throw new Error(d.message || d.detail || 'error ' + r.status)
      if (!d.paper) { art.replaceChildren(el('p', { class: 'hint', text: 'No paper yet.' })); return }
      art.innerHTML = d.html   // rendered by the hall with raw HTML off; see render_paper
      $('paper-ver').textContent = d.paper + ' · version ' + d.sha.slice(0, 12)
      document.title = d.paper + ' · Debate Hall'
      $('toc').replaceChildren(...d.headings.map(h => el('a', {
        href: '#' + h.id, class: 'l' + h.level, text: h.title,
        onclick: ev => { ev.preventDefault(); point({ id: h.id }) }
      })))
    } catch (e) { art.replaceChildren(el('p', { class: 'error', text: e.message })) }
  }

  const norm = s => s.toLowerCase().replace(/\s+/g, ' ')
  const blocks = () => Array.from($('paper').querySelectorAll('p, li, td, th, h1, h2, h3, h4, h5, h6, blockquote, pre'))

  function point (t) {
    $('paper').querySelectorAll('.hl').forEach(n => n.classList.remove('hl'))
    let node = null
    if (t.id) node = document.getElementById(t.id)
    if (!node && t.phrase) node = blocks().find(b => norm(b.textContent).includes(norm(t.phrase))) || null
    if (!node && t.section) node = document.getElementById('s-' + String(t.section).replace(/^§\s*/, ''))
    if (node) { node.classList.add('hl'); node.scrollIntoView({ block: 'center', behavior: 'smooth' }) }
    return !!node
  }

  function find (next) {
    const q = norm($('find').value.trim())
    $('paper').querySelectorAll('.found').forEach(n => n.classList.remove('found'))
    if (!q) { finds = []; $('find-count').textContent = ''; return }
    if (!next) { finds = blocks().filter(b => norm(b.textContent).includes(q)); findAt = -1 }
    finds.forEach(n => n.classList.add('found'))
    $('find-count').textContent = finds.length ? finds.length + ' found' : 'not found'
    if (finds.length) { findAt = (findAt + 1) % finds.length; finds[findAt].scrollIntoView({ block: 'center' }) }
  }

  if (channel) {
    channel.addEventListener('message', ev => {
      const m = ev.data || {}
      if (m.show && $('follow').checked) point(m.show)        // the speaker's /show, if you follow
      if (m.go) point(m.go)                                     // a ledger link: always
      if (m.close) window.close()
    })
    channel.postMessage({ hello: true })
    window.addEventListener('pagehide', () => channel.postMessage({ closed: true }))
  }
  scale(currentScale())
  $('smaller').addEventListener('click', () => scale(currentScale() - 0.1))
  $('bigger').addEventListener('click', () => scale(currentScale() + 0.1))
  $('reload').addEventListener('click', load)
  $('find').addEventListener('input', () => find(false))
  $('find').addEventListener('keydown', ev => { if (ev.key === 'Enter') { ev.preventDefault(); find(true) } })
  load()
})()
