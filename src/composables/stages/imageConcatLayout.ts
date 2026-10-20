export type ConcatLayout = 'horizontal' | 'vertical' | 'grid'

export const LAYOUT_CHOICES: ConcatLayout[] = ['horizontal', 'vertical', 'grid']

export const BG_CHOICES = [
  { value: '', label: 'imageConcat.transparent' },
  { value: '#ffffff', label: 'imageConcat.white' },
  { value: '#000000', label: 'imageConcat.black' },
]

export interface ConcatRect {
  x: number
  y: number
  w: number
  h: number
}

export interface ConcatPlan {
  width: number
  height: number
  rects: ConcatRect[]
}

export const MAX_CANVAS_SIDE = 16384

export function planConcat(
  sizes: { w: number; h: number }[],
  layout: ConcatLayout,
  cols: number,
  gap: number,
  outerGap: boolean,
): ConcatPlan | null {
  if (sizes.length === 0 || sizes.some(s => s.w <= 0 || s.h <= 0)) return null
  const g = Math.max(0, Math.round(gap))
  const o = outerGap ? g : 0
  const ref = sizes[0]
  const rects: ConcatRect[] = []

  if (layout === 'horizontal') {
    let x = o
    for (const s of sizes) {
      const w = Math.max(1, Math.round(s.w * ref.h / s.h))
      rects.push({ x, y: o, w, h: ref.h })
      x += w + g
    }
    return { width: x - g + o, height: ref.h + 2 * o, rects }
  }

  if (layout === 'vertical') {
    let y = o
    for (const s of sizes) {
      const h = Math.max(1, Math.round(s.h * ref.w / s.w))
      rects.push({ x: o, y, w: ref.w, h })
      y += h + g
    }
    return { width: ref.w + 2 * o, height: y - g + o, rects }
  }

  const c = Math.max(1, Math.min(Math.round(cols), sizes.length))
  const r = Math.ceil(sizes.length / c)
  sizes.forEach((s, i) => {
    const scale = Math.min(ref.w / s.w, ref.h / s.h)
    const w = Math.max(1, Math.round(s.w * scale))
    const h = Math.max(1, Math.round(s.h * scale))
    const cellX = o + (i % c) * (ref.w + g)
    const cellY = o + Math.floor(i / c) * (ref.h + g)
    rects.push({ x: cellX + Math.floor((ref.w - w) / 2), y: cellY + Math.floor((ref.h - h) / 2), w, h })
  })
  return { width: c * ref.w + (c - 1) * g + 2 * o, height: r * ref.h + (r - 1) * g + 2 * o, rects }
}

export function canvasScale(width: number, height: number): number {
  return Math.min(1, MAX_CANVAS_SIDE / width, MAX_CANVAS_SIDE / height)
}
