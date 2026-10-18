<script setup lang="ts">
import type { Interrupt } from "../api/endpoints";
import { useChat } from "../stores/chat";

const props = defineProps<{ interrupt: Interrupt }>();
const chat = useChat();

const options = () => props.interrupt.value?.options ?? [];
</script>

<template>
  <div class="interrupt">
    <div class="row" style="margin-bottom: 8px">
      <span class="badge">需要确认</span>
      <span class="muted">{{ interrupt.value?.header }}</span>
    </div>
    <p class="q">{{ interrupt.value?.question }}</p>
    <button
      v-for="o in options()"
      :key="o.label"
      class="option"
      :disabled="chat.running"
      @click="chat.resume(o.label)"
    >
      {{ o.label }}
      <span v-if="o.description" class="desc">{{ o.description }}</span>
    </button>
    <p v-if="!options().length" class="muted">没有可选项，请在输入框直接回答。</p>
  </div>
</template>
