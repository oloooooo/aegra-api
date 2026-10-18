<script setup lang="ts">
import { onMounted, ref } from "vue";
import { getToken } from "./api/client";
import { useChat } from "./stores/chat";
import LoginView from "./components/LoginView.vue";
import ThreadList from "./components/ThreadList.vue";
import ChatView from "./components/ChatView.vue";
import SidePanel from "./components/SidePanel.vue";

const chat = useChat();
const ready = ref(false);
const showSide = ref(true);

onMounted(async () => {
  if (!getToken()) {
    ready.value = true;
    return;
  }
  try {
    await chat.bootstrap();
  } catch {
    chat.logout(); // token 过期或账号被删：回登录页
  } finally {
    ready.value = true;
  }
});
</script>

<template>
  <div v-if="!ready" class="login"><span class="muted">加载中…</span></div>
  <LoginView v-else-if="!chat.identity" />
  <div v-else :class="['layout', { 'no-side': !showSide }]">
    <ThreadList />
    <div class="center">
      <div class="center-head">
        <strong style="color: var(--text)">{{ chat.identity }}</strong>
        <span class="badge" v-if="chat.isSuper">超级用户</span>
        <span style="flex: 1" />
        <button class="tiny ghost" @click="showSide = !showSide">{{ showSide ? "隐藏面板" : "显示面板" }}</button>
        <button class="tiny ghost" @click="chat.logout()">退出</button>
      </div>
      <div v-if="chat.error" class="error">{{ chat.error }}</div>
      <ChatView />
    </div>
    <SidePanel v-if="showSide" />
  </div>
</template>
