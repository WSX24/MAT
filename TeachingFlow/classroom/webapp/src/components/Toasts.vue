<script setup lang="ts">
import { dismissToast, toastItems } from '@/state/toast'
</script>

<template>
  <div class="toast-region" aria-live="polite" aria-atomic="false">
    <div
      v-for="item in toastItems"
      :key="item.id"
      class="ui-toast"
      :class="`ui-toast--${item.level}`"
      :role="item.level === 'error' ? 'alert' : 'status'"
    >
      <span class="ui-toast-body">{{ item.message }}</span>
      <button class="ui-toast-close" type="button" aria-label="关闭提示" @click="dismissToast(item.id)">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18" /></svg>
      </button>
    </div>
  </div>
</template>

<style scoped>
.toast-region {
  position: fixed;
  z-index: 80;
  right: 20px;
  bottom: 20px;
  display: flex;
  width: min(360px, calc(100vw - 32px));
  flex-direction: column;
  gap: 8px;
  pointer-events: none;
}
.ui-toast {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 11px 12px;
  border: 1px solid var(--border);
  border-left: 3px solid var(--accent);
  border-radius: 10px;
  background: var(--bg);
  box-shadow: 0 14px 34px color-mix(in oklch, var(--fg) 13%, transparent);
  color: var(--fg);
  pointer-events: auto;
}
.ui-toast--ok { border-left-color: var(--success); }
.ui-toast--warn { border-left-color: var(--warn); }
.ui-toast--error { border-left-color: var(--danger); }
.ui-toast-body { flex: 1; font-size: 12px; line-height: 1.6; }
.ui-toast-close {
  display: grid;
  width: 20px;
  height: 20px;
  flex: none;
  place-items: center;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--meta);
  cursor: pointer;
  padding: 0;
}
.ui-toast-close:hover { background: var(--surface); color: var(--fg); }
.ui-toast-close svg { width: 13px; height: 13px; fill: none; stroke: currentColor; stroke-linecap: round; stroke-width: 1.8; }
</style>
