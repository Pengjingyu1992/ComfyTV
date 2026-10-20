<template>
  <div class="v2-ed" @pointerdown.stop>
    <div class="v2-ed__canvas">
      <div class="v2-ed__fit">
        <img v-if="state.output && clips.length" :src="state.output" draggable="false" @dragstart.prevent />
      </div>
      <div v-if="!clips.length" class="v2-ed__empty">{{ $t('imageConcat.connectImages') }}</div>
    </div>

    <ImageConcatStrip :node="node" :clips="clips" @ordered="(v) => (ordered = v)" />

    <div class="v2-ed__panel">
      <div class="v2-ed__chips">
        <button
          v-for="l in LAYOUT_CHOICES"
          :key="l"
          type="button"
          class="v2-ed__chip"
          :data-on="layout === l ? '1' : ''"
          @click="layout = l"
        >{{ $t(`imageConcat.layout.${l}`) }}</button>
      </div>
      <div class="v2-ed__chips">
        <div v-for="ctl in controls" :key="ctl.label" class="v2-ed__step">
          <span class="v2-ed__label">{{ ctl.label }}</span>
          <span class="v2-ed__stepval">{{ ctl.value }}</span>
          <button type="button" class="v2-ed__stepbtn" @click="ctl.dec()">−</button>
          <button type="button" class="v2-ed__stepbtn" @click="ctl.inc()">+</button>
        </div>
        <button
          type="button"
          class="v2-ed__chip"
          :data-on="outerGap ? '1' : ''"
          @click="outerGap = !outerGap"
        >{{ $t('imageConcat.outerGap') }}</button>
      </div>
      <div class="v2-ed__chips">
        <span class="v2-ed__label v2-concat__bglabel">{{ $t('imageConcat.bg') }}</span>
        <button
          v-for="c in BG_CHOICES"
          :key="c.value"
          type="button"
          class="v2-ed__chip"
          :data-on="bg === c.value ? '1' : ''"
          @click="bg = c.value"
        >{{ $t(c.label) }}</button>
        <input
          type="color"
          class="v2-concat__color"
          :value="bg || '#000000'"
          @input="bg = ($event.target as HTMLInputElement).value"
        />
      </div>
    </div>

    <div class="v2-ed__status">
      <span v-if="!clips.length">{{ $t('imageConcat.connectImages') }}</span>
      <span v-else-if="composing" class="v2-ed__busy">{{ $t('imageConcat.composing', { n: clips.length }) }}</span>
      <span v-else-if="state.output && outputSize" class="v2-ed__ok">{{ $t('imageConcat.done', { w: outputSize.w, h: outputSize.h }) }}</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

import ImageConcatStrip from '@/components/stages/ImageConcatStrip.vue'
import { imageClipsFromInputs } from '@/composables/stages/imageConcatInputs'
import { BG_CHOICES, LAYOUT_CHOICES } from '@/composables/stages/imageConcatLayout'
import { useImageConcat } from '@/composables/stages/useImageConcat'
import type { VideoClip } from '@/composables/stages/videoClipInputs'
import { t } from '@/i18n'
import type { LGraphNode } from '@/lib/comfyApp'
import type { StageState } from '@/stores/stageStore'

const GAP_STEP = 4

const props = defineProps<{
  node: LGraphNode
  state: StageState
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

<style scoped>
.v2-concat__bglabel {
  align-self: center;
  margin-right: 2px;
}
.v2-concat__color {
  width: 30px;
  height: 28px;
  padding: 0;
  border: 1px solid var(--v2-chip-border);
  border-radius: 9px;
  background: transparent;
  cursor: pointer;
}
</style>
