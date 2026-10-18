import { describe, it, expect } from 'vitest'

import {
  collectReachableNodeIds,
  serializeNodeEntry,
  buildScopedPrompt,
} from './graphSerialize'

function makeGraph(nodes: any[]) {
  const byId = new Map<number, any>(nodes.map(n => [n.id, n]))
  const links = new Map<number, any>()
  for (const n of nodes) {
    n.graph = { links, getNodeById: (id: any) => byId.get(Number(id)) }
  }
  return {
    _nodes: nodes,
    links,
    getNodeById: (id: any) => byId.get(Number(id)),
  }
}

describe('collectReachableNodeIds', () => {
  it('returns only the target for non-bridge nodes', () => {
    const target = { id: 3, comfyClass: 'ComfyTV.ImageStage', inputs: [] }
    const app = { graph: makeGraph([target]) }
    const reachable = collectReachableNodeIds(app, target)
    expect([...reachable]).toEqual([3])
  })

  it('includes the chainable fx upstream for non-bridge targets', () => {
    const src = { id: 1, comfyClass: 'ComfyTV.VideoLoaderStage', inputs: [] }
    const fx1 = { id: 2, comfyClass: 'ComfyTV.VideoCurvesStage', inputs: [{ name: 'video', link: 10 }] }
    const fx2 = { id: 3, comfyClass: 'ComfyTV.VideoColorStage', inputs: [{ name: 'video', link: 20 }] }
    const target = { id: 4, comfyClass: 'ComfyTV.FXChainStage', inputs: [{ name: 'video', link: 30 }] }
    const graph = makeGraph([src, fx1, fx2, target])
    graph.links.set(30, { origin_id: 3 })
    graph.links.set(20, { origin_id: 2 })
    graph.links.set(10, { origin_id: 1 })
    const reachable = collectReachableNodeIds({ graph }, target)
    expect([...reachable].sort()).toEqual([2, 3, 4])
  })

  it('does not cross a keyer whose side inputs are wired (renders locally)', () => {
    const keyer = {
      id: 2,
      comfyClass: 'ComfyTV.KeyerStage',
      inputs: [{ name: 'video', link: null }, { name: 'in_mask', link: 7 }],
    }
    const target = { id: 3, comfyClass: 'ComfyTV.FXChainStage', inputs: [{ name: 'video', link: 30 }] }
    const graph = makeGraph([keyer, target])
    graph.links.set(30, { origin_id: 2 })
    const reachable = collectReachableNodeIds({ graph }, target)
    expect([...reachable]).toEqual([3])
  })

  it('crosses Reroute nodes to reach the chainable fx upstream', () => {
    const fx = { id: 2, comfyClass: 'ComfyTV.VideoCurvesStage', inputs: [] }
    const reroute = { id: 5, type: 'Reroute', inputs: [{ link: 10 }] }
    const target = { id: 4, comfyClass: 'ComfyTV.FXChainStage', inputs: [{ name: 'video', link: 30 }] }
    const graph = makeGraph([fx, reroute, target])
    graph.links.set(30, { origin_id: 5, origin_slot: 0 })
    graph.links.set(10, { origin_id: 2, origin_slot: 0 })
    const reachable = collectReachableNodeIds({ graph }, target)
    expect([...reachable].sort()).toEqual([2, 4])
  })

  it('walks upstream links for BridgeTo nodes', () => {
    const src = { id: 1, comfyClass: 'X', inputs: [] }
    const mid = { id: 2, comfyClass: 'Y', inputs: [{ link: 10 }] }
    const target = { id: 3, comfyClass: 'ComfyTV.BridgeToFoo', inputs: [{ link: 20 }] }
    const graph = makeGraph([src, mid, target])
    graph.links.set(20, { origin_id: 2 })
    graph.links.set(10, { origin_id: 1 })
    const app = { graph }
    const reachable = collectReachableNodeIds(app, target)
    expect([...reachable].sort()).toEqual([1, 2, 3])
  })
})

describe('serializeNodeEntry', () => {
  it('serializes widget values and link inputs', async () => {
    const graph = { links: new Map([[5, { origin_id: 9, origin_slot: 1 }]]) }
    const node = {
      comfyClass: 'ComfyTV.ImageStage',
      title: 'My Stage',
      graph,
      widgets: [
        { name: 'main_prompt', value: 'hi' },
        { name: 'steps', type: 'INT', value: 20 },
      ],
      inputs: [{ name: 'image', link: 5 }],
    }
    const entry = await serializeNodeEntry(node)
    expect(entry.class_type).toBe('ComfyTV.ImageStage')
    expect(entry._meta.title).toBe('My Stage')
    expect(entry.inputs.main_prompt).toBe('hi')
    expect(entry.inputs.steps).toBe(20)
    expect(entry.inputs.image).toEqual(['9', 1])
  })

  it('resolves link inputs through Reroute chains to the real source', async () => {
    const stage = { id: 1, comfyClass: 'ComfyTV.ImageStage', inputs: [] }
    const r1 = { id: 150, type: 'Reroute', inputs: [{ link: 11 }] }
    const r2 = { id: 151, type: 'Reroute', inputs: [{ link: 12 }] }
    const target = { id: 3, comfyClass: 'ComfyTV.ImageEditStage', inputs: [{ name: 'image', link: 13 }] }
    const graph = makeGraph([stage, r1, r2, target])
    graph.links.set(13, { origin_id: 151, origin_slot: 0 })
    graph.links.set(12, { origin_id: 150, origin_slot: 0 })
    graph.links.set(11, { origin_id: 1, origin_slot: 2 })
    const entry = await serializeNodeEntry(target)
    expect(entry.inputs.image).toEqual(['1', 2])
  })

  it('drops inputs fed by an unconnected or cyclic Reroute', async () => {
    const dangling = { id: 150, type: 'Reroute', inputs: [{ link: null }] }
    const loopA = { id: 160, type: 'Reroute', inputs: [{ link: 21 }] }
    const loopB = { id: 161, type: 'Reroute', inputs: [{ link: 20 }] }
    const target = {
      id: 3,
      comfyClass: 'ComfyTV.ImageEditStage',
      inputs: [{ name: 'image', link: 13 }, { name: 'mask', link: 22 }],
    }
    const graph = makeGraph([dangling, loopA, loopB, target])
    graph.links.set(13, { origin_id: 150, origin_slot: 0 })
    graph.links.set(22, { origin_id: 160, origin_slot: 0 })
    graph.links.set(21, { origin_id: 161, origin_slot: 0 })
    graph.links.set(20, { origin_id: 160, origin_slot: 0 })
    const entry = await serializeNodeEntry(target)
    expect('image' in entry.inputs).toBe(false)
    expect('mask' in entry.inputs).toBe(false)
  })

  it('coerces invalid numeric widget to default or zero', async () => {
    const node = {
      comfyClass: 'X',
      graph: { links: new Map() },
      widgets: [
        { name: 'a', type: 'FLOAT', value: '', options: { default: 4 } },
        { name: 'b', type: 'INT', value: 'nope' },
      ],
      inputs: [],
    }
    const entry = await serializeNodeEntry(node)
    expect(entry.inputs.a).toBe(4)
    expect(entry.inputs.b).toBe(0)
  })

  it('skips widgets with serialize=false', async () => {
    const node = {
      comfyClass: 'X',
      graph: { links: new Map() },
      widgets: [{ name: 'hidden', value: 1, options: { serialize: false } }],
      inputs: [],
    }
    const entry = await serializeNodeEntry(node)
    expect('hidden' in entry.inputs).toBe(false)
  })

  it('skips button widgets and direct serialize=false (e.g. workflow buttons)', async () => {
    const node = {
      comfyClass: 'ComfyTV.ImageStage',
      graph: { links: new Map() },
      widgets: [
        { name: '⬆ Upload workflow', type: 'button', serialize: false, value: null },
        { name: '🔗 Link workflow', type: 'button', serialize: false, value: null },
        { name: 'steps', type: 'INT', value: 20 },
      ],
      inputs: [],
    }
    const entry = await serializeNodeEntry(node)
    expect('⬆ Upload workflow' in entry.inputs).toBe(false)
    expect('🔗 Link workflow' in entry.inputs).toBe(false)
    expect(entry.inputs.steps).toBe(20)
  })

  it('wraps array values, tagging curve type', async () => {
    const node = {
      comfyClass: 'X',
      graph: { links: new Map() },
      widgets: [
        { name: 'pts', type: 'curve', value: [1, 2] },
        { name: 'list', value: [3, 4] },
      ],
      inputs: [],
    }
    const entry = await serializeNodeEntry(node)
    expect(entry.inputs.pts).toEqual({ __type__: 'CURVE', __value__: [1, 2] })
    expect(entry.inputs.list).toEqual({ __value__: [3, 4] })
  })
})

describe('buildScopedPrompt', () => {
  it('includes only reachable, non-muted, non-virtual nodes', async () => {
    const nodes = [
      { id: 1, comfyClass: 'A', graph: { links: new Map() }, widgets: [], inputs: [] },
      { id: 2, comfyClass: 'B', mode: 2, graph: { links: new Map() }, widgets: [], inputs: [] },
      { id: 3, comfyClass: 'C', isVirtualNode: true, graph: { links: new Map() }, widgets: [], inputs: [] },
      { id: 4, comfyClass: 'D', graph: { links: new Map() }, widgets: [], inputs: [] },
    ]
    const app = { graph: { _nodes: nodes } }
    const reachable = new Set([1, 2, 3])
    const pm = await buildScopedPrompt(app, reachable)
    expect(Object.keys(pm.output)).toEqual(['1'])
    expect(pm.workflow).toEqual({ nodes: [{ id: 1, type: 'A', properties: {} }], links: [], version: 0.4 })
  })

  it('carries each included stage uid so the server can stamp outputs without the page', async () => {
    const nodes = [
      { id: 7, comfyClass: 'ComfyTV.ImageStage', properties: { comfytv_stage_uid: 'uid-7', other: 1 },
        graph: { links: new Map() }, widgets: [], inputs: [] },
    ]
    const pm = await buildScopedPrompt({ graph: { _nodes: nodes } }, new Set([7]))
    expect(pm.workflow.nodes).toEqual([{ id: 7, type: 'ComfyTV.ImageStage', properties: { comfytv_stage_uid: 'uid-7' } }])
  })
})
