import { ref, watch, type Ref } from 'vue'
import { useDebounceFn } from '@vueuse/core'

import type { LGraphNode } from '@/lib/comfyApp'
import { canvasScale, LAYOUT_CHOICES, planConcat, type ConcatLayout } from '@/composables/stages/imageConcatLayout'
import type { VideoClip } from '@/composables/stages/videoClipInputs'
import { useStageStore, type StageState } from '@/stores/stageStore'
import { uploadCanvas } from '@/utils/uploadCanvas'
import { bindWidgetCallback, getWidget, onNodeConfigure, readWidgetNum, readWidgetStr, writeWidget } from '@/utils/widget'

const SCHEDULE_DELAY_MS = 250

function readLayout(node: LGraphNode): ConcatLayout {
  const v = readWidgetStr(node, 'layout', 'horizontal') as ConcatLayout
  return LAYOUT_CHOICES.includes(v) ? v : 'horizontal'
}

export function useImageConcat(node: LGraphNode, state: StageState, ordered: Ref<VideoClip[]>) {
  const store = useStageStore()

  const layout = ref<ConcatLayout>(readLayout(node))
  const cols = ref(readWidgetNum(node, 'cols', 2))
  const gap = ref(readWidgetNum(node, 'gap', 0))
  const outerGap = ref(!!getWidget(node, 'outer_gap')?.value)
  const bg = ref(String(getWidget(node, 'bg')?.value ?? ''))

  function syncFromWidgets() {
    layout.value = readLayout(node)
    cols.value = readWidgetNum(node, 'cols', cols.value)
    gap.value = readWidgetNum(node, 'gap', gap.value)
    outerGap.value = !!getWidget(node, 'outer_gap')?.value
    bg.value = String(getWidget(node, 'bg')?.value ?? '')
  }
  bindWidgetCallback(node, 'layout', () => { layout.value = readLayout(node) })
  bindWidgetCallback(node, 'cols', (v) => { cols.value = Number(v) || 1 })
  bindWidgetCallback(node, 'gap', (v) => { gap.value = Number(v) || 0 })
  bindWidgetCallback(node, 'outer_gap', (v) => { outerGap.value = !!v })
  bindWidgetCallback(node, 'bg', (v) => { bg.value = String(v ?? '') })
  onNodeConfigure(node, syncFromWidgets)

  const composing = ref(false)
  const outputSize = ref<{ w: number; h: number } | null>(null)
  const images = new Map<string, Promise<HTMLImageElement>>()
  let seq = 0

  function loadImage(url: string): Promise<HTMLImageElement> {
    let p = images.get(url)
    if (!p) {
      p = new Promise((resolve, reject) => {
        const img = new Image()
        img.crossOrigin = 'anonymous'
        img.onload = () => resolve(img)
        img.onerror = reject
        img.src = url
      })
      images.set(url, p)
      p.catch(() => images.delete(url))
    }
    return p
  }

  async function run() {
    const urls = ordered.value.map(c => c.url)
    if (urls.length === 0) return
    for (const k of images.keys()) if (!urls.includes(k)) images.delete(k)
    const mySeq = ++seq
    composing.value = true
    try {
      const imgs = await Promise.all(urls.map(loadImage))
      if (mySeq !== seq) return
      const plan = planConcat(imgs.map(i => ({ w: i.naturalWidth, h: i.naturalHeight })),
        layout.value, cols.value, gap.value, outerGap.value)
      if (!plan) return
      const s = canvasScale(plan.width, plan.height)
      const canvas = document.createElement('canvas')
      canvas.width = Math.max(1, Math.round(plan.width * s))
      canvas.height = Math.max(1, Math.round(plan.height * s))
      const ctx = canvas.getContext('2d')
      if (!ctx) throw new Error('2d context unavailable')
      if (bg.value) {
        ctx.fillStyle = bg.value
        ctx.fillRect(0, 0, canvas.width, canvas.height)
      }
      ctx.imageSmoothingQuality = 'high'
      ctx.scale(s, s)
      plan.rects.forEach((r, i) => ctx.drawImage(imgs[i], r.x, r.y, r.w, r.h))

      const imageUrl = await uploadCanvas(canvas, {
        subfolder: 'comfytv/imageconcat',
        filename: `comfytv-concat-${String(node?.id ?? 'unknown')}-${Date.now()}.png`,
      })
      if (mySeq !== seq) return
      outputSize.value = { w: canvas.width, h: canvas.height }
      store.applyExecutedPayload(state, { output: [imageUrl] })
    } catch (e) {
      console.error('[ComfyTV/imageconcat] concat failed', e)
    } finally {
      if (mySeq === seq) composing.value = false
    }
  }

  const schedule = useDebounceFn(() => { void run() }, SCHEDULE_DELAY_MS)

  watch([layout, cols, gap, outerGap, bg], ([l, c, g, o, b]) => {
    writeWidget(node, 'layout', l)
    writeWidget(node, 'cols', c)
    writeWidget(node, 'gap', g)
    writeWidget(node, 'outer_gap', o)
    writeWidget(node, 'bg', b)
    schedule()
  })
  watch(() => ordered.value.map(c => c.url).join('\n'), () => schedule(), { immediate: true })

  return {
    layout, cols, gap, outerGap, bg,
    setCols: (v: number) => { cols.value = Math.max(1, Math.min(12, Math.round(v))) },
    setGap: (v: number) => { gap.value = Math.max(0, Math.round(Number.isFinite(v) ? v : 0)) },
    composing, outputSize,
  }
}
