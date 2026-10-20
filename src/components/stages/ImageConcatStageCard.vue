<template>
  <div class="ctv:flex ctv:flex-col ctv:gap-1.5 ctv:size-full">
    <ImageConcatStrip :node="node" :clips="clips" @ordered="(v) => (ordered = v)" />

    <div class="ctv:text-2xs ctv:text-center ctv:py-0.5">
      <span v-if="!clips.length" class="ctv:text-muted-foreground">{{ $t('imageConcat.connectImages') }}</span>
      <span v-else-if="composing" class="ctv:text-muted-foreground">{{ $t('imageConcat.composing', { n: clips.length }) }}</span>
      <span v-else-if="state.output && outputSize" class="ctv:text-success-background">
        {{ $t('imageConcat.done', { w: outputSize.w, h: outputSize.h }) }}
      </span>
    </div>

    <div class="ctv:flex ctv:gap-1">
      <button
        v-for="l in LAYOUT_CHOICES"
        :key="l"
        type="button"
        class="ctv:flex-1 ctv:py-1 ctv:px-1.5 ctv:rounded ctv:text-xs ctv:cursor-pointer ctv:border"
        :class="layout === l ? CHIP_ON : CHIP_OFF"
        @click="layout = l"
      >{{ $t(`imageConcat.layout.${l}`) }}</button>
    </div>

    <div class="ctv:flex ctv:gap-2">
      <div
        v-for="ctl in controls"
        :key="ctl.label"
        class="ctv:flex-1 ctv:flex ctv:items-center ctv:gap-1.5 ctv:py-0.5 ctv:px-1.5 ctv:rounded
               ctv:bg-secondary-background ctv:border ctv:border-border-subtle"
      >
        <span class="ctv:text-2xs ctv:uppercase ctv:tracking-wide ctv:text-muted-foreground">{{ ctl.label }}</span>
        <button type="button" :class="STEP_BTN" @click="ctl.dec()">−</button>
        <span class="ctv:ml-auto ctv:min-w-6 ctv:text-center ctv:font-mono ctv:text-xs ctv:text-base-foreground">{{ ctl.value }}</span>
        <button type="button" :class="STEP_BTN" @click="ctl.inc()">+</button>
      </div>
    </div>

    <div class="ctv:flex ctv:gap-1 ctv:items-center">
      <label class="ctv:flex ctv:items-center ctv:gap-1.5 ctv:py-0.5 ctv:px-1.5 ctv:rounded ctv:cursor-pointer
                    ctv:bg-secondary-background ctv:border ctv:border-border-subtle ctv:select-none">
        <input
          type="checkbox"
          class="ctv:cursor-pointer ctv:accent-[var(--primary-background)]"
          :checked="outerGap"
          @change="outerGap = ($event.target as HTMLInputElement).checked"
        />
        <span class="ctv:text-2xs ctv:uppercase ctv:tracking-wide ctv:text-muted-foreground">{{ $t('imageConcat.outerGap') }}</span>
      </label>
      <span class="ctv:ml-auto ctv:text-2xs ctv:uppercase ctv:tracking-wide ctv:text-muted-foreground">{{ $t('imageConcat.bg') }}</span>
      <button
        v-for="c in BG_CHOICES"
        :key="c.value"
        type="button"
        class="ctv:py-0.5 ctv:px-1.5 ctv:rounded ctv:text-2xs ctv:cursor-pointer ctv:border"
        :class="bg === c.value ? CHIP_ON : CHIP_OFF"
        @click="bg = c.value"
      >{{ $t(c.label) }}</button>
      <input
        type="color"
        class="ctv:size-6 ctv:p-0 ctv:cursor-pointer ctv:bg-transparent ctv:border ctv:border-border-subtle ctv:rounded"
        :value="bg || '#000000'"
        @input="bg = ($event.target as HTMLInputElement).value"
      />
    </div>

    <StageCard
      :state="state"
      :node="node"
      :on-run-request="onRunRequest"
      :on-cancel-request="onCancelRequest"
      :on-disconnect="onDisconnect"
      :on-action="onAction"
      hide-context
    />
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

import ImageConcatStrip from '@/components/stages/ImageConcatStrip.vue'
import StageCard from '@/components/stages/StageCard.vue'
import { imageClipsFromInputs } from '@/composables/stages/imageConcatInputs'
import { BG_CHOICES, LAYOUT_CHOICES } from '@/composables/stages/imageConcatLayout'
import { useImageConcat } from '@/composables/stages/useImageConcat'
import type { VideoClip } from '@/composables/stages/videoClipInputs'
import { t } from '@/i18n'
import type { LGraphNode } from '@/lib/comfyApp'
import type { StageState } from '@/stores/stageStore'

const GAP_STEP = 4
const CHIP_ON = 'ctv:bg-secondary-background-selected ctv:border-primary-background ctv:text-primary-background ctv:font-semibold'
const CHIP_OFF = 'ctv:bg-secondary-background ctv:border-border-subtle ctv:text-base-foreground ctv:hover:bg-secondary-background-hover'
const STEP_BTN = 'ctv:size-5 ctv:rounded-sm ctv:border ctv:border-border-subtle ctv:bg-secondary-background ctv:text-base-foreground ctv:text-[13px] ctv:leading-none ctv:cursor-pointer ctv:hover:bg-secondary-background-hover'

const props = defineProps<{
  state: StageState
  onRunRequest: () => void
  onCancelRequest: () => void
  onDisconnect: (slot: string) => void
  onAction: (id: string) => void
  node: LGraphNode
}>()

const clips = computed(() => imageClipsFromInputs(props.state.inputs))
const ordered = ref<VideoClip[]>([])

const { layout, cols, setCols, gap, setGap, outerGap, bg, composing, outputSize } =
  useImageConcat(props.node, props.state, ordered)

const controls = computed(() => [
  ...(layout.value === 'grid'
    ? [{ label: t('imageConcat.cols'), value: cols.value,
         dec: () => setCols(cols.value - 1), inc: () => setCols(cols.value + 1) }]
    : []),
  { label: t('imageConcat.gap'), value: gap.value,
    dec: () => setGap(gap.value - GAP_STEP), inc: () => setGap(gap.value + GAP_STEP) },
])
</script>
