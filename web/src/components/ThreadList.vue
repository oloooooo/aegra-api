<script setup lang="ts">
import { useChat } from "../stores/chat";

const chat = useChat();

function title(t: { metadata?: { thread_name?: string }; thread_id: string }) {
  const name = t.metadata?.thread_name?.trim();
  return name || t.thread_id.slice(0, 8);
}

async function remove(id: string, ev: Event) {
  ev.stopPropagation();
  if (!confirm("删除这个会话？删除后不可恢复（关联的定时任务也会一起删掉）。")) return;
  await chat.removeThread(id);
}
</script>

<template>
  <aside class="panel">
    <div class="panel-head">
      <button class="primary" style="flex: 1" @click="chat.threadId = null; chat.messages = []; chat.interrupts = []">
        + 新会话
      </button>
      <button class="ghost tiny" title="刷新" @click="chat.loadThreads()">⟳</button>
    </div>
    <div class="panel-body">
      <div
        v-for="t in chat.threads"
        :key="t.thread_id"
        :class="['thread-item', { active: t.thread_id === chat.threadId }]"
        @click="chat.openThread(t.thread_id)"
      >
        <span class="name">{{ title(t) }}</span>
        <button class="tiny ghost del" @click="remove(t.thread_id, $event)">删</button>
      </div>
      <p v-if="!chat.threads.length" class="muted" style="padding: 8px">还没有会话</p>
    </div>
  </aside>
</template>
