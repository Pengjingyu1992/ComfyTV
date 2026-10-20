import { slotColor } from '@/composables/stages/imageSlotMentions'
import type { VideoClip } from '@/composables/stages/videoClipInputs'
import { toImagePoolJson, type ResolvedInput } from '@/stores/stageStore'

const SLOT_RE = /^images\.image(\d+)$/

export function imageClipsFromInputs(inputs: ResolvedInput[]): VideoClip[] {
  const found: { key: string; url: string }[] = []
  for (const i of inputs) {
    if (i.source !== 'upstream' || !i.content) continue
    if (SLOT_RE.test(i.slot)) {
      found.push({ key: i.slot.split('.').pop()!, url: i.content })
    } else if (i.slot === 'batch') {
      const images = JSON.parse(toImagePoolJson(i.content)).images as Array<{ image_url?: string }>
      images.forEach((im, n) => {
        if (im.image_url) found.push({ key: `batch${n + 1}`, url: String(im.image_url) })
      })
    }
  }
  return found.map((c, n) => ({ ...c, color: slotColor(n + 1) }))
}

export function imageClipLabel(key: string): string {
  return key.replace('image', '#').replace('batch', 'B')
}
