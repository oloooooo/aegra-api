<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue";
import { useChat } from "../stores/chat";
import MessageItem from "./MessageItem.vue";
import InterruptCard from "./InterruptCard.vue";

const chat = useChat();
const draft = ref("");
const scroller = ref<HTMLElement | null>(null);

const placeholder = computed(() =>
  chat.threadId ? "问点什么…（Ctrl+Enter 发送）" : "输入问题，会自动新建会话",
);

async function scrollDown() {
  await nextTick();
  if (scroller.value) scroller.value.scrollTop = scroller.value.scrollHeight;
}

watch(() => [chat.messages.length, chat.streaming, chat.interrupts.length], scrollDown);

async function submit() {
  const text = draft.value.trim();
  if (!text || chat.running) return;
  draft.value = "";
  await chat.send(text);
  await scrollDown();
}
</script>

<template>
  <div class="messages" ref="scroller">
    <p v-if="!chat.messages.length && !chat.running" class="muted">
      试试：「混合键合 DOE 里良率最高的是哪个？」「CBAM 铝的默认因子是多少？」「SBOL3 中 gene 和 CDS 的区别」
    </p>

    <MessageItem v-for="m in chat.messages" :key="m.key" :message="m" />

    <div v-if="chat.streaming" class="msg ai">
      <div class="who">assistant</div>
      <div class="body">{{ chat.streaming }}</div>
    </div>

    <InterruptCard v-if="chat.interrupts.length" :interrupt="chat.interrupts[0]!" />

    <p v-if="chat.running && !chat.streaming" class="muted">思考中…</p>
  </div>

  <div class="composer">
    <textarea
      v-model="draft"
      :placeholder="placeholder"
      @keydown.ctrl.enter.prevent="submit"
      @keydown.meta.enter.prevent="submit"
    />
    <div style="display: flex; flex-direction: column; gap: 6px">
      <button class="primary" :disabled="chat.running || !draft.trim()" @click="submit">发送</button>
      <button v-if="chat.running" class="ghost" @click="chat.cancel()">停止</button>
    </div>
  </div>
</template>
