export function parseOrder(raw: string): string[] {
  try {
    const v = JSON.parse(raw)
    return Array.isArray(v) ? v.map(String) : []
  } catch {
    return []
  }
}

export function reconcileOrder(saved: string[], currentKeys: string[]): string[] {
  return [...saved, ...currentKeys.filter(k => !saved.includes(k))]
}
