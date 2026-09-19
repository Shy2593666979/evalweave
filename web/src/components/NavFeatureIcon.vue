<script setup lang="ts">
defineProps<{
  kind:
    | 'overview'
    | 'assistant'
    | 'evaluations'
    | 'schedules'
    | 'human-tasks'
    | 'admin-projects'
    | 'admin-users'
    | 'user-types'
    | 'evaluation-models'
}>()
</script>

<template>
  <svg class="nav-feature-icon" :class="{ 'nav-feature-icon-assistant': kind === 'assistant' }" viewBox="0 0 24 24" aria-hidden="true">
    <template v-if="kind === 'overview'">
      <path d="M3.75 10.35 12 3.8l8.25 6.55v8.05a1.85 1.85 0 0 1-1.85 1.85H5.6a1.85 1.85 0 0 1-1.85-1.85v-8.05Z"/>
      <path d="M9.25 20.1v-5.8h5.5v5.8"/>
    </template>
    <template v-else-if="kind === 'assistant'">
      <path class="nav-icon-soft" d="M7.2 5.1h12.1a2.2 2.2 0 0 1 2.2 2.2v8.1a2.2 2.2 0 0 1-2.2 2.2h-2.1v2.9l-3.8-2.9H7.2A2.2 2.2 0 0 1 5 15.4V7.3a2.2 2.2 0 0 1 2.2-2.2Z"/>
      <path class="nav-icon-solid" d="M4.4 2.5h12.2a2.4 2.4 0 0 1 2.4 2.4v8.2a2.4 2.4 0 0 1-2.4 2.4H9l-5 3.8v-3.8A2.4 2.4 0 0 1 1.6 13V4.9a2.4 2.4 0 0 1 2.8-2.4Z"/>
      <g class="nav-icon-dots"><circle cx="6.2" cy="9" r="1"/><circle cx="10.3" cy="9" r="1"/><circle cx="14.4" cy="9" r="1"/></g>
    </template>
    <template v-else-if="kind === 'evaluations'">
      <path d="M3.25 7.45h17.5v10.7a2.1 2.1 0 0 1-2.1 2.1H5.35a2.1 2.1 0 0 1-2.1-2.1V7.45Z"/>
      <path d="M3.65 7.45V5.8a2.05 2.05 0 0 1 2.05-2.05h4.15l2.05 2.1h6.75a2.1 2.1 0 0 1 2.1 1.6"/>
      <path d="M7.3 12.1h6.9"/>
    </template>
    <template v-else-if="kind === 'schedules'">
      <circle cx="12" cy="12" r="8.35"/>
      <path d="M12 7.35v5.05l3.5 2.05"/>
    </template>
    <template v-else-if="kind === 'human-tasks'">
      <circle cx="12" cy="7.25" r="3.35"/>
      <path d="M5.1 19.85v-1.3A5.65 5.65 0 0 1 10.75 12.9h2.5a5.65 5.65 0 0 1 5.65 5.65v1.3H5.1Z"/>
    </template>
    <template v-else-if="kind === 'admin-users'">
      <circle cx="9.2" cy="8" r="3.1"/>
      <path d="M3.4 19.5v-1.2a5.1 5.1 0 0 1 5.1-5.1h1.4a5.1 5.1 0 0 1 5.1 5.1v1.2"/>
      <path d="M15.1 5.6a2.75 2.75 0 0 1 0 5.1M16.7 13.5a4.7 4.7 0 0 1 3.9 4.65v1.35"/>
    </template>
    <template v-else-if="kind === 'admin-projects'">
      <path d="M3.25 7.45h17.5v10.7a2.1 2.1 0 0 1-2.1 2.1H5.35a2.1 2.1 0 0 1-2.1-2.1V7.45Z"/>
      <path d="M3.65 7.45V5.8a2.05 2.05 0 0 1 2.05-2.05h4.15l2.05 2.1h6.75a2.1 2.1 0 0 1 2.1 1.6"/>
    </template>
    <template v-else-if="kind === 'user-types'">
      <rect x="3.2" y="4.1" width="17.6" height="15.8" rx="3"/>
      <circle cx="8.2" cy="10" r="2.1"/>
      <path d="M5.5 16.1a3.15 3.15 0 0 1 5.4 0M13.8 9h3.8M13.8 12.2h3.8M13.8 15.4h2.5"/>
    </template>
    <template v-else>
      <rect x="5.1" y="5.1" width="13.8" height="13.8" rx="3.2"/>
      <rect x="8.7" y="8.7" width="6.6" height="6.6" rx="1.6"/>
      <path d="M9 2.7v2.4M15 2.7v2.4M9 18.9v2.4M15 18.9v2.4M2.7 9h2.4M2.7 15h2.4M18.9 9h2.4M18.9 15h2.4"/>
    </template>
  </svg>
</template>
