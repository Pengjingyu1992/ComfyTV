<template>
  <section>
    <h3 class="ctv:mt-1 ctv:mb-1.5 ctv:text-xs ctv:uppercase ctv:tracking-wide ctv:text-muted-foreground">
      {{ $t('configSidebar.section.widgets') }}
    </h3>

    <div v-if="showNodeFilter" class="ctv:flex ctv:flex-col ctv:gap-1.5 ctv:mb-2.5">
      <div class="ctv:relative">
        <IconSearch
          class="ctv:absolute ctv:left-2 ctv:top-1/2 ctv:-translate-y-1/2 ctv:size-3.5
                 ctv:text-muted-foreground ctv:pointer-events-none"
        />
        <input
          v-model="searchQuery"
          type="text"
          :placeholder="$t('configSidebar.searchNodes')"
          class="ctv:w-full ctv:h-7 ctv:box-border ctv:pl-7 ctv:pr-2 ctv:rounded-lg ctv:text-xs ctv:[font-family:inherit]
                 ctv:bg-secondary-background ctv:border ctv:border-border-subtle ctv:text-base-foreground
                 ctv:placeholder:text-muted-foreground ctv:focus-visible:outline-none ctv:focus:border-border-default"
        />
      </div>
      <div v-if="groupChips.length > 1" class="ctv:flex ctv:flex-wrap ctv:items-center ctv:gap-1">
        <button
          :class="chipClass(groupFilter === ALL_GROUPS)"
          @click="groupFilter = ALL_GROUPS"
        >
          {{ $t('configSidebar.groupAll') }}
          <span :class="chipCountClass">{{ totalNodeCount }}</span>
        </button>
        <button
          v-for="chip in groupChips"
          :key="chip.key"
          :class="chipClass(groupFilter === chip.key)"
          @click="groupFilter = groupFilter === chip.key ? ALL_GROUPS : chip.key"
        >
          {{ chip.label }}
          <span :class="chipCountClass">{{ chip.count }}</span>
        </button>
      </div>
    </div>

    <div v-if="!visibleGroups.length"
         class="ctv:py-5 ctv:px-1.5 ctv:text-center ctv:italic ctv:text-xs ctv:text-muted-foreground/60">
      {{ $t('configSidebar.noMatchingNodes') }}
    </div>

    <div v-for="(grp, gi) in visibleGroups" :key="gi"
         class="ctv:flex ctv:flex-col ctv:gap-1.5 ctv:mb-2.5">
      <div v-if="grp.title"
           class="ctv:py-1 ctv:text-3xs ctv:uppercase ctv:tracking-wide
                  ctv:text-muted-foreground ctv:border-b ctv:border-border-subtle">
        {{ grp.title }}
      </div>

      <div v-for="node in grp.nodes" :key="node.node_id"
           class="ctv:rounded-lg ctv:overflow-hidden ctv:bg-base-foreground/[0.03]">
        <button
          :class="[
            'ctv:flex ctv:items-center ctv:gap-1.5 ctv:w-full ctv:py-1.5 ctv:px-2 ctv:text-left ctv:cursor-pointer ctv:text-inherit ctv:[font-family:inherit]',
            'ctv:bg-transparent ctv:border-none ctv:hover:bg-secondary-background-hover',
          ]"
          :aria-expanded="!isNodeCollapsed(node.node_id)"
          @click="toggleCollapsed(node.node_id)"
        >
          <i :class="['pi', isNodeCollapsed(node.node_id) ? 'pi-chevron-right' : 'pi-chevron-down', 'ctv:w-2.5 ctv:text-2xs ctv:text-muted-foreground']" />
          <span class="ctv:text-xs ctv:font-semibold ctv:text-base-foreground">{{ node.node_title }}</span>
          <span v-if="node.node_title !== node.node_type"
                class="ctv:text-2xs ctv:font-mono ctv:text-muted-foreground/60">
            ({{ node.node_type }})
          </span>
          <span class="ctv:text-2xs ctv:font-mono ctv:text-muted-foreground/60">#{{ node.node_id }}</span>
          <span class="ctv:flex-1"></span>
          <span class="ctv:text-3xs ctv:font-mono ctv:py-px ctv:px-1.5 ctv:rounded-lg ctv:bg-base-foreground/5 ctv:text-muted-foreground">
            {{ boundWidgetCount(node, isStageBound) }} / {{ node.widgets.length }}
          </span>
        </button>

        <div v-if="!isNodeCollapsed(node.node_id)" class="ctv:flex ctv:flex-col ctv:gap-1.5 ctv:p-2">
          <span v-if="outputsTakenOver(node)" class="ctv:text-3xs ctv:italic ctv:text-muted-foreground">
            {{ $t('configSidebar.outputsTakenOver') }}
          </span>
          <div
            v-for="w in node.widgets.filter(w => !isOutputRow(w))"
            :key="`${w.node_id}/${w.widget_name}`"
            class="ctv:flex ctv:flex-col ctv:gap-1 ctv:[&_+_&]:pt-1.5 ctv:[&_+_&]:border-t ctv:[&_+_&]:border-border-subtle"
          >
            <div class="ctv:text-2xs">
              <span class="ctv:font-mono ctv:text-muted-foreground">.{{ w.widget_name }}</span>
            </div>
            <ComfyTVWidget
              :kind="w.widget_type"
              :model-value="effectiveValue(w)"
              :options="comboOptions(w)"
              :min="numProp(w, 'min')"
              :max="numProp(w, 'max')"
              :step="numProp(w, 'step')"
              :precision="numProp(w, 'precision')"
              :multiline="!!w.widget_props?.multiline"
              :disabled="isStageBound(w) || !!exposedOf(w) || outputsTakenOver(node)"
              @update:model-value="onValueChange(w, $event)"
            />
            <div v-if="exposedOf(w)" class="ctv:flex ctv:items-center ctv:gap-1.5 ctv:mt-0.5 ctv:text-3xs ctv:text-muted-foreground">
              <span class="ctv:uppercase ctv:tracking-wide">{{ $t('configSidebar.customExposed') }}</span>
              <span class="ctv:py-px ctv:px-1.5 ctv:rounded-lg ctv:bg-base-foreground/5 ctv:text-base-foreground">
                {{ exposedOf(w)!.label }} · {{ exposedOf(w)!.prompt ? $t('configSidebar.customExposedPrompt') : exposedOf(w)!.kind }}
              </span>
            </div>
            <div v-else class="ctv:grid ctv:grid-cols-[60px_1fr] ctv:items-center ctv:gap-1.5 ctv:mt-0.5">
              <span class="ctv:text-3xs ctv:uppercase ctv:tracking-wide ctv:text-muted-foreground">{{ $t('configSidebar.bindTo') }}</span>
              <ComfyTVSelect
                :model-value="dropdownValueFor(w)"
                :options="bindingOptions"
                @update:model-value="onBindingChange(w, $event as string)"
              />
            </div>
            <label v-if="!exposedOf(w) && isUpstreamBound(w)"
                   class="ctv:flex ctv:items-center ctv:gap-1.5 ctv:mt-0.5 ctv:cursor-pointer
                          ctv:text-3xs ctv:uppercase ctv:tracking-wide ctv:text-muted-foreground">
              <input
                type="checkbox"
                :checked="w.required !== false"
                class="ctv:m-0 ctv:cursor-pointer"
                @change="onRequiredToggle(w, ($event.target as HTMLInputElement).checked)"
              />
              {{ $t('configSidebar.bindingRequired') }}
            </label>
          </div>
          <NodeOutputBindings
            v-if="node.widgets.some(isOutputRow)"
            :outputs="node.widgets.filter(isOutputRow)"
            :options="bindingOptions"
            @change="onBindingChange"
          />
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, toRef } from 'vue'
import { useI18n } from 'vue-i18n'

import IconSearch from '~icons/lucide/search'

import ComfyTVWidget from '@/components/widgets/ComfyTVWidget.vue'
import ComfyTVSelect from '@/components/widgets/ComfyTVSelect.vue'
import NodeOutputBindings from '@/components/sidebar/NodeOutputBindings.vue'
import { useBindingWriter } from '@/composables/sidebar/useBindingWriter'
import { useCollapsedNodeIds } from '@/composables/sidebar/useCollapsedState'
import { boundWidgetCount, useNodeFilter } from '@/composables/sidebar/useConfigSidebar'
import {
  ALL_GROUPS,
  buildBindingOptions,
  customExposureIndex,
  isOutputRow,
  outputsTakenOver,
  type ConfigPayload,
  type ExposedWidget,
} from '@/composables/sidebar/workflowConfigCatalog'

const props = defineProps<{
  config: ConfigPayload
  workflowKind: string | undefined
  postBinding: (payload: Record<string, unknown>) => Promise<void>
  deleteBinding: (nodeId: string, widgetName: string) => Promise<void>
}>()

const { t } = useI18n()

const config = toRef(props, 'config')
const workflowId = computed(() => props.config.id)

const exposureIndex = computed(() => customExposureIndex(props.config))
function exposedOf(w: ExposedWidget) {
  return exposureIndex.value.get(`${w.node_id}/${w.widget_name}`)
}

const bindingOptions = computed(() =>
  buildBindingOptions(props.config.exposed_widgets ?? [], props.workflowKind),
)

const { isCollapsed, toggle: toggleCollapsed } = useCollapsedNodeIds(workflowId)

const {
  searchQuery,
  groupFilter,
  totalNodeCount,
  showNodeFilter,
  groupChips,
  visibleGroups,
} = useNodeFilter(config, workflowId, t)

function isNodeCollapsed(nodeId: string): boolean {
  if (searchQuery.value.trim()) return false
  return isCollapsed(nodeId)
}

const {
  isStageBound,
  isUpstreamBound,
  dropdownValueFor,
  effectiveValue,
  comboOptions,
  numProp,
  onValueChange,
  onBindingChange,
  onRequiredToggle,
} = useBindingWriter(props.postBinding, props.deleteBinding)

function chipClass(active: boolean) {
  return [
    'ctv:inline-flex ctv:items-center ctv:gap-1 ctv:cursor-pointer ctv:[font-family:inherit]',
    'ctv:rounded-lg ctv:border ctv:px-2 ctv:py-0.5 ctv:text-2xs ctv:transition-colors',
    active
      ? 'ctv:bg-secondary-background-selected ctv:border-primary-background/60 ctv:text-base-foreground'
      : 'ctv:bg-secondary-background ctv:border-border-subtle ctv:text-muted-foreground ctv:hover:bg-secondary-background-hover ctv:hover:text-base-foreground',
  ].join(' ')
}

const chipCountClass = 'ctv:py-0 ctv:px-1 ctv:rounded-lg ctv:text-3xs ctv:bg-base-foreground/10'
</script>
