<template>
  <div class="ctv:flex ctv:flex-col ctv:gap-1.5 ctv:size-full">
    <PainterCanvas
      ref="painterRef"
      :node="node"
      :source-image-url="sourceImageUrl"
    />

    <StageCard
      :state="state"
      :node="node"
      :on-run-request="onRunWithMaskCommit"
      :on-cancel-request="onCancelRequest"
      :on-disconnect="onDisconnect"
      :on-action="onAction"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import type { LGraphNode } from '@/lib/comfyApp'
import type { StageState } from '@/stores/stageStore'
import StageCard from '@/components/stages/StageCard.vue'
import { type MediaTable, readMediaTable, subscribeMediaTable } from '@/composables/stages/mediaOrder'
import { mediaEntryUrl } from '@/composables/stages/mediaOrderSync'
import { pickSourceImageUrl } from '@/composables/stages/stageInputs'
import PainterCanvas from '@/components/widgets/PainterCanvas.vue'

const props = defineProps<{
  state: StageState
  onRunRequest: () => void
  onCancelRequest: () => void
  onDisconnect: (slot: string) => void
  onAction: (id: string) => void
  node: LGraphNode
}>()

const painterRef = ref<InstanceType<typeof PainterCanvas> | null>(null)

const media = ref<MediaTable>(readMediaTable(props.node))
const unsubMedia = subscribeMediaTable(props.node, () => { media.value = readMediaTable(props.node) })

const sourceImageUrl = computed(() => {
  const wired = pickSourceImageUrl(props.state.inputs)
  if (wired) return wired
  const first = media.value.image[0]
  return first ? mediaEntryUrl(props.node, first) : null
})

async function commit() {
  try {
    await painterRef.value?.commitMask()
  } catch (e) {
    console.warn('[ComfyTV/painter] commitMask failed, running anyway:', e)
  }
}

let unregisterPreRun: (() => void) | null = null
onMounted(() => {
  unregisterPreRun =
    (props.node as any).__comfytvStageApi?.registerPreRun?.(commit) ?? null
})
onUnmounted(() => {
  unregisterPreRun?.()
  unsubMedia()
})

async function onRunWithMaskCommit() {
  if (!unregisterPreRun) await commit()
  props.onRunRequest()
}
</script>
