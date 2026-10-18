<script setup lang="ts">
import { computed } from "vue";
import type { ViewMessage } from "../stores/chat";
import { renderMarkdown } from "../utils/markdown";

const props = defineProps<{ message: ViewMessage }>();

const who = computed(() => {
  if (props.message.role === "human") return "你";
  if (props.message.role === "tool") return props.message.name ?? "工具";
  if (props.message.role === "callback") return `子 agent「${props.message.name}」返回`;
  return "assistant";
});

const html = computed(() => (props.message.role === "ai" ? renderMarkdown(props.message.content) : ""));
</script>

<template>
  <div :class="['msg', message.role]">
    <div class="who">{{ who }}</div>
    <div v-if="message.role === 'ai'" class="body" v-html="html" />
    <div v-else class="body">{{ message.content }}</div>
  </div>
</template>
