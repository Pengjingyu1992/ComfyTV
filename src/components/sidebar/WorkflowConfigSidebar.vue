<template>
  <div class="ctv:flex ctv:flex-col ctv:size-full ctv:box-border ctv:overflow-auto ctv:text-xs
              ctv:pt-2 ctv:pb-6 ctv:px-2.5 ctv:text-base-foreground">
    <div class="ctv:sticky ctv:-top-2 ctv:z-[1] ctv:-mx-2.5 ctv:-mt-2 ctv:mb-2 ctv:py-1.5 ctv:px-2.5
                ctv:bg-interface-panel-surface ctv:border-b ctv:border-border-subtle">
      <span class="ctv:font-semibold ctv:text-sm">{{ $t('configSidebar.title') }}</span>
    </div>

    <div v-if="!selected" :class="emptyClass">
      {{ $t('configSidebar.empty') }}
    </div>

    <div v-else-if="!selected.workflowLabel" :class="emptyClass">
      {{ $t('configSidebar.noWorkflowPicked') }}
    </div>

    <div v-else-if="loadError"
         class="ctv:my-1.5 ctv:py-1.5 ctv:px-2 ctv:text-xs ctv:rounded
                ctv:bg-destructive-background/15 ctv:border ctv:border-destructive-background/50 ctv:text-destructive-background">
      {{ loadError }}
    </div>

    <div v-else-if="config" class="ctv:flex ctv:flex-col ctv:gap-3">
      <div class="ctv:flex ctv:flex-col ctv:gap-0.5 ctv:pt-1 ctv:pb-2 ctv:border-b ctv:border-border-subtle">
        <div class="ctv:flex ctv:items-start ctv:gap-2">
          <div class="ctv:flex ctv:flex-col ctv:gap-0.5 ctv:flex-1 ctv:min-w-0">
            <span class="ctv:text-3xs ctv:uppercase ctv:tracking-wide ctv:text-muted-foreground">{{ config.kind }}</span>
            <span class="ctv:text-xs ctv:font-semibold ctv:break-all">{{ config.label }}</span>
          </div>
          <button
            :class="exportBtn"
            :disabled="config.file_exists === false || reconvertBusy"
            :title="$t('configSidebar.reconvertApiTooltip')"
            @click="onReconvertApi"
          ><i :class="['pi pi-refresh', reconvertBusy && 'pi-spin']" /> {{ $t('configSidebar.reconvertApi') }}</button>
        </div>
        <span v-if="reconvertError" class="ctv:mt-1 ctv:text-2xs ctv:break-all ctv:text-destructive-background">{{ reconvertError }}</span>
        <span v-if="!config.has_api" class="ctv:mt-1 ctv:text-2xs ctv:italic ctv:text-warning-background">
          {{ $t('configSidebar.pickWorkflowFirst') }}
        </span>
      </div>

      <section v-if="isLinked"
               class="ctv:flex ctv:flex-col ctv:gap-1.5 ctv:py-1.5 ctv:px-2 ctv:rounded"
               :class="linkBroken
                 ? 'ctv:bg-destructive-background/10 ctv:border ctv:border-destructive-background/40'
                 : 'ctv:bg-primary-background/[0.06] ctv:border ctv:border-primary-background/25'">
        <div class="ctv:flex ctv:items-center ctv:gap-1.5">
          <i :class="['pi', linkBroken ? 'pi-exclamation-triangle' : 'pi-link',
                      'ctv:text-2xs', linkBroken ? 'ctv:text-destructive-background' : 'ctv:text-primary-foreground']" />
          <span class="ctv:text-2xs ctv:font-semibold"
                :class="linkBroken ? 'ctv:text-destructive-background' : 'ctv:text-base-foreground'">
            {{ linkBroken ? $t('configSidebar.linkBroken') : $t('configSidebar.linkedSource') }}
          </span>
        </div>
        <p class="ctv:m-0 ctv:text-3xs ctv:leading-relaxed ctv:text-muted-foreground ctv:break-all">
          {{ linkBroken ? $t('configSidebar.linkBrokenHint') : $t('configSidebar.linkedSourceHint') }}
        </p>
        <button
          :class="resetBtn"
          :disabled="unlinkBusy"
          @click="onUnlink"
        ><i class="pi pi-link" /> {{ $t('configSidebar.unlink') }}</button>
        <span v-if="unlinkError" class="ctv:text-3xs ctv:text-destructive-background">{{ unlinkError }}</span>
      </section>

      <section v-if="config.gui_notes?.length"
               class="ctv:rounded ctv:overflow-hidden
                      ctv:bg-warning-background/[0.03] ctv:border ctv:border-warning-background/25">
        <button
          :class="[
            'ctv:flex ctv:items-center ctv:gap-1.5 ctv:w-full ctv:py-[5px] ctv:px-2 ctv:text-left ctv:cursor-pointer ctv:[font-family:inherit]',
            'ctv:bg-warning-background/5 ctv:border-0 ctv:border-b ctv:text-inherit',
            'ctv:hover:bg-warning-background/10',
            notesCollapsed ? 'ctv:border-b-transparent' : 'ctv:border-b-warning-background/15',
          ]"
          :aria-expanded="!notesCollapsed"
          @click="toggleNotesCollapsed"
        >
          <i :class="['pi', notesCollapsed ? 'pi-chevron-right' : 'pi-chevron-down', 'ctv:w-2.5 ctv:text-2xs ctv:text-warning-background/75']" />
          <span class="ctv:flex-1 ctv:text-2xs ctv:uppercase ctv:tracking-wide ctv:font-semibold ctv:text-warning-background">
            {{ $t('configSidebar.section.notes') }}
          </span>
          <span class="ctv:text-3xs ctv:font-mono ctv:py-px ctv:px-1.5 ctv:rounded-lg
                       ctv:bg-warning-background/10 ctv:text-warning-background/70">
            {{ config.gui_notes.length }}
          </span>
        </button>
        <div v-if="!notesCollapsed" class="ctv:flex ctv:flex-col ctv:gap-1 ctv:py-1.5 ctv:px-2">
          <div v-for="(note, i) in config.gui_notes" :key="i"
               class="ctv:py-1 ctv:px-2 ctv:rounded-sm ctv:border-l-2
                      ctv:bg-warning-background/5 ctv:border-warning-background/50">
            <pre class="ctv:m-0 ctv:text-xs ctv:whitespace-pre-wrap ctv:[font-family:inherit] ctv:text-base-foreground">{{ note.text }}</pre>
          </div>
        </div>
      </section>

      <WorkflowWidgetsSection
        v-if="config.exposed_widgets?.length"
        :config="config"
        :workflow-kind="selected.workflowKind"
        :post-binding="postBinding"
        :delete-binding="deleteBinding"
      />

      <div v-else class="ctv:p-2 ctv:text-xs ctv:text-left ctv:flex ctv:flex-col ctv:gap-2">
        <p class="ctv:m-0 ctv:italic ctv:text-muted-foreground/60">
          {{ $t('configSidebar.noExposedWidgets') }}
        </p>
        <template v-if="config && !config.has_api">
          <p class="ctv:m-0 ctv:text-muted-foreground/80">
            {{ $t('configSidebar.conversionMayHaveFailed') }}
          </p>
          <button
            :class="exportBtn"
            :disabled="uploadApiBusy"
            :title="$t('configSidebar.uploadApiTooltip')"
            @click="onUploadApiSidecar"
          ><i class="pi pi-upload" /> {{ $t('configSidebar.uploadApi') }}</button>
          <span v-if="uploadApiError" class="ctv:text-destructive-background">{{ uploadApiError }}</span>
        </template>
      </div>

      <section v-if="config.has_api && isCustom">
        <h3 :class="sectionHeading">{{ $t('configSidebar.section.result') }}</h3>
        <div class="ctv:flex ctv:flex-col ctv:gap-1 ctv:px-1 ctv:text-2xs">
          <div v-for="(o, i) in customOutputs" :key="i" class="ctv:flex ctv:items-center ctv:gap-1.5">
            <span class="ctv:font-mono ctv:text-3xs ctv:py-px ctv:px-1.5 ctv:rounded-lg ctv:bg-base-foreground/5 ctv:text-muted-foreground">{{ o.kind }}</span>
            <span>{{ o.label }}</span>
          </div>
          <p class="ctv:m-0 ctv:text-2xs ctv:italic ctv:text-muted-foreground/60">{{ $t('configSidebar.customOutputsHint') }}</p>
        </div>
      </section>

      <section v-if="config.has_api && !isCustom">
        <h3 :class="sectionHeading">{{ $t('configSidebar.section.result') }}</h3>
        <div class="ctv:flex ctv:flex-col ctv:gap-1.5 ctv:px-1">
          <div class="ctv:grid ctv:grid-cols-[42px_1fr] ctv:items-center ctv:gap-1.5">
            <span class="ctv:text-3xs ctv:uppercase ctv:tracking-wide ctv:text-muted-foreground">{{ $t('configSidebar.resultNode') }}</span>
            <ComfyTVSelect
              :model-value="resultNodeModel"
              :options="resultNodeOptions"
              @update:model-value="onResultNodeChange($event as string)"
            />
          </div>
          <div v-if="hasResultNode && resultTypeOptions.length > 1" class="ctv:grid ctv:grid-cols-[42px_1fr] ctv:items-center ctv:gap-1.5">
            <span class="ctv:text-3xs ctv:uppercase ctv:tracking-wide ctv:text-muted-foreground">{{ $t('configSidebar.resultType.label') }}</span>
            <ComfyTVSelect
              :model-value="resultType"
              :options="resultTypeOptions"
              @update:model-value="onResultTypeChange($event as string)"
            />
          </div>
          <p class="ctv:m-0 ctv:text-2xs ctv:italic ctv:text-muted-foreground/60">
            {{ hasResultNode ? $t('configSidebar.resultHintSet') : $t('configSidebar.resultHintAuto') }}
          </p>
        </div>
      </section>

      <section v-if="config.has_api">
        <h3 :class="sectionHeading">{{ $t('configSidebar.section.properties') }}</h3>
        <div class="ctv:flex ctv:flex-col ctv:gap-1.5 ctv:px-1">
          <div class="ctv:grid ctv:grid-cols-[42px_1fr] ctv:items-center ctv:gap-1.5">
            <span class="ctv:text-3xs ctv:uppercase ctv:tracking-wide ctv:text-muted-foreground">{{ $t('configSidebar.mentionStyle.label') }}</span>
            <ComfyTVSelect
              :model-value="mentionStyleModel"
              :options="mentionStyleOptions"
              @update:model-value="onMentionStyleChange($event as string)"
            />
          </div>
          <p class="ctv:m-0 ctv:text-2xs ctv:italic ctv:text-muted-foreground/60">
            {{ $t('configSidebar.mentionStyle.hint') }}
          </p>
        </div>
      </section>

      <section v-if="config.description">
        <h3 :class="sectionHeading">{{ $t('configSidebar.section.description') }}</h3>
        <p class="ctv:m-0 ctv:text-xs ctv:whitespace-pre-wrap ctv:text-muted-foreground">{{ config.description }}</p>
      </section>

      <div class="ctv:mt-4 ctv:pt-2.5 ctv:pb-3.5 ctv:px-3 ctv:flex ctv:flex-col ctv:gap-1 ctv:border-t ctv:border-border-subtle">
        <button
          :class="exportBtn"
          :disabled="!config.has_api || exportBusy"
          :title="$t('configSidebar.exportPresetTooltip')"
          @click="onExportPreset"
        ><i class="pi pi-download" /> {{ $t('configSidebar.exportPreset') }}</button>
        <button
          :class="exportBtn"
          :disabled="config.file_exists === false || openInComfyBusy"
          :title="$t('openInComfy.tooltip')"
          @click="onOpenInComfy"
        ><i class="pi pi-external-link" /> {{ $t('openInComfy.open') }}</button>
        <button
          :class="resetBtn"
          :disabled="resetBusy"
          :title="$t('configSidebar.resetToPresetTooltip')"
          @click="onResetToPreset"
        >{{ $t('configSidebar.resetToPreset') }}</button>
        <span v-if="exportError" class="ctv:text-xs ctv:text-destructive-background">{{ exportError }}</span>
        <span v-if="resetError" class="ctv:text-xs ctv:text-destructive-background">{{ resetError }}</span>
      </div>
    </div>

    <div v-else :class="emptyClass">{{ $t('configSidebar.loading') }}</div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

import ComfyTVSelect from '@/components/widgets/ComfyTVSelect.vue'
import WorkflowWidgetsSection from '@/components/sidebar/WorkflowWidgetsSection.vue'
import { useCollapsedFlag } from '@/composables/sidebar/useCollapsedState'
import { useResultMeta, useSelectionConfigSync } from '@/composables/sidebar/useConfigSidebar'
import { useWorkflowConfig } from '@/composables/sidebar/useWorkflowConfig'
import { normalizeMentionStyle } from '@/composables/stages/imageSlotMentions'
import { LINK_TYPE_NATIVE } from '@/api'
import { customExposedOutputs } from '@/composables/sidebar/workflowConfigCatalog'

const { t } = useI18n()

const {
  config, loadError,
  exportBusy, exportError,
  resetBusy,  resetError,
  uploadApiBusy, uploadApiError,
  reconvertBusy, reconvertError,
  unlinkBusy, unlinkError,
  openInComfyBusy,
  loadConfig,
  onExportPreset,
  onOpenInComfy,
  onResetToPreset,
  onUploadApiSidecar,
  onReconvertApi,
  onUnlink,
  postBinding,
  deleteBinding,
  postMeta,
} = useWorkflowConfig(t)

const { selected } = useSelectionConfigSync(config, loadConfig)

const isLinked   = computed(() => config.value?.link_type === LINK_TYPE_NATIVE)
const linkBroken = computed(() => isLinked.value && config.value?.file_exists === false)

const {
  hasResultNode,
  resultNodeModel,
  resultType,
  resultNodeOptions,
  resultTypeOptions,
  onResultNodeChange,
  onResultTypeChange,
} = useResultMeta(config, postMeta, t)

const isCustom = computed(() => config.value?.kind === 'custom')
const customOutputs = computed(() => customExposedOutputs(config.value))

const workflowId = computed(() => config.value?.id ?? null)
const { collapsed: notesCollapsed, toggle: toggleNotesCollapsed } =
  useCollapsedFlag(workflowId, 'comfytv:sidebar:notes-collapsed:')

const mentionStyleModel = computed(() =>
  normalizeMentionStyle(config.value?.meta?.mention_style),
)
const mentionStyleOptions = computed(() => [
  { value: 'natural', label: t('configSidebar.mentionStyle.natural') },
  { value: 'minimax_tags', label: t('configSidebar.mentionStyle.minimaxTags') },
  { value: 'qwen_tags', label: t('configSidebar.mentionStyle.qwenTags') },
  { value: 'flux3_tags', label: t('configSidebar.mentionStyle.flux3Tags') },
])
async function onMentionStyleChange(style: string) {
  const meta = { ...(config.value?.meta ?? {}) }
  if (normalizeMentionStyle(style) === 'natural') delete meta.mention_style
  else meta.mention_style = style
  await postMeta({ meta })
}

const emptyClass     = 'ctv:py-5 ctv:px-1.5 ctv:text-center ctv:italic ctv:text-xs ctv:text-muted-foreground/60'
const sectionHeading = 'ctv:mt-1 ctv:mb-1.5 ctv:text-xs ctv:uppercase ctv:tracking-wide ctv:text-muted-foreground'

const COMFY_BTN_SM = [
  'ctv:inline-flex ctv:items-center ctv:justify-center ctv:gap-1 ctv:cursor-pointer ctv:whitespace-nowrap ctv:appearance-none',
  'ctv:border-none ctv:transition-colors ctv:focus-visible:outline-none',
  'ctv:disabled:pointer-events-none ctv:disabled:opacity-50',
  'ctv:h-6 ctv:rounded-sm ctv:px-2 ctv:py-1 ctv:text-xs ctv:font-medium',
].join(' ')

const exportBtn = COMFY_BTN_SM
  + ' ctv:self-start ctv:text-secondary-foreground ctv:bg-secondary-background ctv:hover:bg-secondary-background-hover'

const resetBtn = COMFY_BTN_SM
  + ' ctv:self-start ctv:bg-transparent ctv:text-muted-foreground ctv:hover:bg-warning-background/10 ctv:hover:text-warning-background'
</script>
