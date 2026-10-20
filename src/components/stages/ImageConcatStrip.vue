<template>
  <div
    class="ctv:flex ctv:flex-col ctv:gap-1"
    @pointerdown.stop
    @pointermove.stop
    @pointerup.stop
  >
    <div
      v-if="clips.length === 0"
      class="ctv:flex ctv:flex-col ctv:items-center ctv:justify-center ctv:gap-1.5 ctv:h-20
             ctv:rounded-md ctv:bg-black/40 ctv:border ctv:border-dashed ctv:border-border-subtle ctv:text-white/50"
    >
      <i class="pi pi-images ctv:text-[24px] ctv:opacity-60" />
      <div class="ctv:text-xs">{{ $t('imageConcat.noInputs') }}</div>
    </div>

    <div
      v-else
      ref="stripEl"
      class="ctv:flex ctv:gap-1.5 ctv:p-1.5 ctv:rounded-md ctv:overflow-x-auto ctv:bg-black/40
             ctv:border ctv:border-border-subtle ctv:select-none"
    >
      <div
        v-for="(clip, idx) in orderedClips"
        :key="clip.key"
        class="ctv:relative ctv:shrink-0 ctv:w-24 ctv:rounded-md ctv:overflow-hidden ctv:bg-black
               ctv:border-2 ctv:touch-none"
        :class="dragKey === clip.key
          ? 'ctv:border-dashed ctv:opacity-40 ctv:cursor-grabbing'
          : 'ctv:cursor-grab'"
        :style="{ borderColor: clip.color }"
        @pointerdown="(e) => onTileDown(e, idx)"
        @pointermove="onTileMove"
        @pointerup="onTileUp"
        @pointercancel="onTileUp"
      >
        <img
          :src="clip.url"
          class="ctv:block ctv:w-full ctv:h-16 ctv:object-cover ctv:pointer-events-none"
          draggable="false"
        />
        <span class="ctv:absolute ctv:top-0.5 ctv:left-0.5 ctv:py-px ctv:px-1.5 ctv:text-3xs ctv:font-bold
                     ctv:rounded ctv:text-black ctv:pointer-events-none"
              :style="{ background: clip.color }">
          {{ idx + 1 }}
        </span>
        <span class="ctv:absolute ctv:bottom-0.5 ctv:right-0.5 ctv:py-px ctv:px-1 ctv:text-3xs ctv:font-bold
                     ctv:rounded ctv:bg-black/70 ctv:pointer-events-none"
              :style="{ color: clip.color }">
          {{ imageClipLabel(clip.key) }}
        </span>
      </div>
    </div>

    <div v-if="clips.length > 1" class="ctv:text-3xs ctv:text-center ctv:text-muted-foreground ctv:tracking-wide">
      {{ $t('imageConcat.dragToReorder') }}
    </div>

    <Teleport to="body">
      <div
        v-if="dragClip"
        class="ctv:fixed ctv:z-[9999] ctv:rounded-md ctv:overflow-hidden ctv:bg-black ctv:border-2
               ctv:pointer-events-none ctv:shadow-[0_8px_24px_rgb(0_0_0/0.6)] ctv:rotate-2"
        :style="{
          left: `${cloneX}px`,
          top: `${cloneY}px`,
          width: `${cloneW}px`,
          height: `${cloneH}px`,
          borderColor: dragClip.color,
        }"
      >
        <img :src="dragClip.url" class="ctv:block ctv:size-full ctv:object-cover" draggable="false" />
        <span class="ctv:absolute ctv:top-0.5 ctv:left-0.5 ctv:py-px ctv:px-1.5 ctv:text-3xs ctv:font-bold
                     ctv:rounded ctv:text-black"
              :style="{ background: dragClip.color }">
          {{ dragTargetIdx + 1 }}
        </span>
      </div>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { ref, toRef, watch } from 'vue'

import { imageClipLabel } from '@/composables/stages/imageConcatInputs'
import { useClipReorder } from '@/composables/stages/useClipReorder'
import type { VideoClip } from '@/composables/stages/videoClipInputs'
import type { LGraphNode } from '@/lib/comfyApp'

const props = defineProps<{
  node: LGraphNode
  clips: VideoClip[]
}>()

const emit = defineEmits<{ ordered: [clips: VideoClip[]] }>()

const stripEl = ref<HTMLDivElement | null>(null)

const {
  orderedClips, dragKey, dragClip, dragTargetIdx,
  cloneX, cloneY, cloneW, cloneH,
  onTileDown, onTileMove, onTileUp,
} = useClipReorder(props.node, { clips: toRef(props, 'clips'), stripEl })

watch(orderedClips, (v) => emit('ordered', v), { immediate: true })
</script>
