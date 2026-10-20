import { describe, expect, it } from 'vitest'

import type { ResolvedInput } from '@/stores/stageStore'

import { imageClipLabel, imageClipsFromInputs } from './imageConcatInputs'
import { canvasScale, planConcat } from './imageConcatLayout'

describe('planConcat', () => {
  it('matches heights to the first image in a row', () => {
    const plan = planConcat([{ w: 100, h: 50 }, { w: 50, h: 100 }], 'horizontal', 2, 10, false)!
    expect(plan.rects).toEqual([{ x: 0, y: 0, w: 100, h: 50 }, { x: 110, y: 0, w: 25, h: 50 }])
    expect([plan.width, plan.height]).toEqual([135, 50])
  })

  it('matches widths to the first image in a column with an outer gap', () => {
    const plan = planConcat([{ w: 100, h: 50 }, { w: 200, h: 200 }], 'vertical', 2, 4, true)!
    expect(plan.rects).toEqual([{ x: 4, y: 4, w: 100, h: 50 }, { x: 4, y: 58, w: 100, h: 100 }])
    expect([plan.width, plan.height]).toEqual([108, 162])
  })

  it('rebuilds the Grid Split source size from equal cells', () => {
    const cells = Array.from({ length: 6 }, () => ({ w: 98, h: 64 }))
    const plan = planConcat(cells, 'grid', 3, 3, true)!
    expect([plan.width, plan.height]).toEqual([3 * 98 + 4 * 3, 2 * 64 + 3 * 3])
    expect(plan.rects[4]).toEqual({ x: 3 + 98 + 3, y: 3 + 64 + 3, w: 98, h: 64 })
  })

  it('fits odd-sized images into grid cells and caps cols at the image count', () => {
    const plan = planConcat([{ w: 100, h: 100 }, { w: 200, h: 100 }], 'grid', 5, 0, false)!
    expect([plan.width, plan.height]).toEqual([200, 100])
    expect(plan.rects[1]).toEqual({ x: 100, y: 25, w: 100, h: 50 })
  })

  it('returns null without usable sizes', () => {
    expect(planConcat([], 'horizontal', 2, 0, false)).toBeNull()
    expect(planConcat([{ w: 0, h: 10 }], 'horizontal', 2, 0, false)).toBeNull()
  })
})

describe('canvasScale', () => {
  it('only shrinks past the canvas side limit', () => {
    expect(canvasScale(4000, 3000)).toBe(1)
    expect(canvasScale(32768, 1000)).toBe(0.5)
  })
})

describe('imageClipsFromInputs', () => {
  const inp = (slot: string, content: string | null, source: ResolvedInput['source'] = 'upstream'): ResolvedInput =>
    ({ slot, type: 'COMFYTV_IMAGE', source, content })

  it('collects single images and every batch item with stable keys', () => {
    const batch = JSON.stringify({ images: [{ index: '1', image_url: '/b1' }, { index: '2', image_url: '/b2' }] })
    const clips = imageClipsFromInputs([
      inp('batch', batch),
      inp('images.image0', '/a'),
      inp('images.image1', null, 'empty'),
    ])
    expect(clips.map(c => [c.key, c.url])).toEqual([['batch1', '/b1'], ['batch2', '/b2'], ['image0', '/a']])
    expect(clips.map(c => imageClipLabel(c.key))).toEqual(['B1', 'B2', '#0'])
  })
})
