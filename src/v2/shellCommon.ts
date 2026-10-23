import { useResizeObserver, useTimeoutFn } from '@vueuse/core'
import { effectScope, watch, type EffectScope } from 'vue'

import { t } from '@/i18n'
import { app, type ComfyNode } from '@/lib/comfyApp'
import { useServerStore } from '@/stores/serverStore'

const RING_FADE_MS = 400

export const I = (d: string, sw = 1.7) =>
  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="${sw}">${d}</svg>`
export const ICON_STOP = `<svg viewBox="0 0 24 24" fill="currentColor" stroke="none"><rect x="7" y="7" width="10" height="10" rx="2"/></svg>`
export const RUN_BUTTON_HTML = `<span class="v2-run__up">${I(`<path d="M12 19V5M5.5 11.5L12 5l6.5 6.5"/>`, 2.4)}</span><span class="v2-run__stop">${ICON_STOP}</span>`

export const ICON_GRIP = `<svg class="v2-grip" viewBox="0 0 24 24" fill="currentColor"><circle cx="8" cy="5" r="1.7"/><circle cx="16" cy="5" r="1.7"/><circle cx="8" cy="12" r="1.7"/><circle cx="16" cy="12" r="1.7"/><circle cx="8" cy="19" r="1.7"/><circle cx="16" cy="19" r="1.7"/></svg>`

export function el(tag: string, cls: string, html?: string) {
  const e = document.createElement(tag)
  e.className = cls
  if (html != null) e.innerHTML = html
  return e
}

function growNodeHeight(node: ComfyNode, delta: number) {
  if (Math.abs(delta) <= 1) return
  node.setSize([node.size[0], node.size[1] + delta])
  ;(app as any).graph?.setDirtyCanvas?.(true, true)
}

export function stopNativeAutoGrow(node: ComfyNode) {
  // V2 slots float outside layout, so litegraph's natural size would only fight the card.
  // Both core auto-size paths read computeSize(), so pinning it here covers them all.
  ;(node as any).computeSize = () => [node.size[0], node.size[1]]
}

// The card owns the node height: height = chrome + the flexible block's wanted height,
// where chrome is every fixed block measured together. Absolute, so calling it twice is a
// no-op and no trigger can double-count a chrome change. Nothing is persisted: litegraph
// already saves the node height and wanted is derived back out of it.
export function bindCardHeight(node: ComfyNode, opts: {
  scope: EffectScope
  card: HTMLElement
  flexible: HTMLElement
  min: number
}): () => void {
  const anyNode = node as any
  const { card, flexible, min } = opts
  const laidOut = () => card.offsetHeight > 0
  const measurable = () => laidOut() && flexible.offsetHeight > 0
  const chromeOf = () => card.offsetHeight - flexible.offsetHeight

  let chrome = -1
  let applied = -1
  let wanted = 0
  // the card is taller than node.size by a constant (the hidden title row); measure it
  // whenever the DOM and node.size are known to agree, and work in DOM space throughout.
  let offset = 0
  let live = false

  // Panels hydrate asynchronously after mount — islands, and a media strip that waits on the
  // server. Absorbing that into the preview is what flexbox would do and keeps a saved node at
  // the height it was saved at, so only take over once the user is demonstrably working here.
  const sample = () => {
    chrome = chromeOf()
    offset = card.offsetHeight - node.size[1]
    wanted = Math.max(min, flexible.offsetHeight)
    applied = node.size[1]
  }

  const goLive = () => {
    if (live || !measurable()) return
    live = true
    sample()
  }

  const apply = () => {
    goLive()
    if (!live || !laidOut()) return
    chrome = chromeOf()
    const h = chrome + wanted - offset
    applied = h
    if (Math.abs(h - node.size[1]) < 1) return
    node.setSize([node.size[0], h])
    ;(app as any).graph?.setDirtyCanvas?.(true, true)
  }

  card.dataset.v2Height = '1'
  card.addEventListener('pointerdown', goLive, { capture: true })
  const prevConn = anyNode.onConnectionsChange
  anyNode.onConnectionsChange = function (...args: unknown[]) {
    if (!anyNode.__comfytvRelinking) goLive()
    return prevConn?.apply(this, args)
  }

  opts.scope.run(() => {
    const onResize = () => {
      if (!laidOut()) return
      if (!live) { chrome = chromeOf(); return }
      // chrome moved: we drive the node. chrome steady but the card moved: the user did.
      if (chromeOf() !== chrome) apply()
      else if (measurable() && Math.abs(node.size[1] - applied) >= 1) sample()
    }
    useResizeObserver(card, onResize)
    useResizeObserver(flexible, onResize)
  })
  return apply
}

export function bindPromptResize(node: ComfyNode, promptAnchor: HTMLElement, scope: EffectScope) {
  scope.run(() => {
    let last = -1
    useResizeObserver(promptAnchor, (entries) => {
      const h = entries[0]?.contentRect.height ?? 0
      if (h <= 0) return
      if (last >= 0) {
        const delta = h - last
        growNodeHeight(node, delta)
      }
      last = h
    })
  })
}

export function hideNativeWidgets(node: ComfyNode) {
  for (const w of (node.widgets ?? []) as any[]) {
    if (w.type === 'v2') continue
    ;(w.options ??= {}).hidden = true
    w.hidden = true
  }
}

export function ensureMinSize(node: ComfyNode, minW: number, minH: number) {
  if ((node as any).__comfytvConfigured) return
  const [w0, h0] = node.size
  node.setSize([Math.max(w0, minW), Math.max(h0, minH)])
}

export function createNodeScope(node: ComfyNode): EffectScope {
  const scope = effectScope(true)
  const anyNode = node as any
  const prev = anyNode.onRemoved
  anyNode.onRemoved = function (...args: unknown[]) {
    scope.stop()
    prev?.apply(this, args)
  }
  return scope
}

export function bindLivePreview(
  node: ComfyNode,
  busy: HTMLElement,
  state: { running?: boolean; livePreview?: string | null },
) {
  const img = el('img', 'v2-preview__live') as HTMLImageElement
  img.alt = ''
  busy.before(img)
  const note = el('div', 'v2-preview__note', t('v2.livePreviewLocalOnly'))
  busy.append(note)
  const servers = useServerStore()
  watch(
    () => [state.livePreview, state.running] as const,
    ([url, running]) => {
      if (url) img.src = url
      else img.removeAttribute('src')
      img.dataset.on = url ? '1' : ''
      busy.dataset.live = url ? '1' : ''
      note.hidden = !running
        || servers.resolveSelection((node as any).properties?.comfytv_server) == null
    },
    { immediate: true },
  )
}

export function bindProgressRing(
  card: HTMLElement,
  state: { running?: boolean; progress?: { value: number; max: number; text?: string } | null },
) {
  let ring: HTMLDivElement | null = null
  const remove = useTimeoutFn(() => {
    ring?.remove()
    ring = null
    delete card.dataset.v2Running
  }, RING_FADE_MS, { immediate: false })
  watch(
    () => [state.running, state.progress?.value, state.progress?.max] as const,
    ([running, v, m]) => {
      if (!running) {
        if (ring) { ring.dataset.on = ''; remove.start() }
        return
      }
      remove.stop()
      if (!ring) {
        ring = document.createElement('div')
        ring.className = 'v2-ring'
        card.appendChild(ring)
      }
      card.dataset.v2Running = '1'
      const value = Number(v) || 0
      const max = Number(m) || 0
      const p = max > 0 ? Math.min(1, Math.max(0, value / max)) : 0
      ring.style.setProperty('--v2-p', p.toFixed(4))
      ring.dataset.on = '1'
      ring.dataset.indeterminate = p <= 0 ? '1' : ''
    },
    { immediate: true },
  )
}
