<script setup lang="ts">
import { ref } from "vue";
import { useChat } from "../stores/chat";

const chat = useChat();
const identity = ref("root");
const password = ref("1234");
const busy = ref(false);
const err = ref("");

async function submit() {
  busy.value = true;
  err.value = "";
  try {
    await chat.login(identity.value.trim(), password.value);
  } catch (e) {
    err.value = (e as Error).message;
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div class="login">
    <form @submit.prevent="submit">
      <h2 style="margin: 0 0 4px">Aegra 登录</h2>
      <p class="muted" style="margin: 0 0 8px">用知识空间账号登录后开始对话</p>
      <input v-model="identity" placeholder="账号" autocomplete="username" />
      <input v-model="password" type="password" placeholder="密码" autocomplete="current-password" />
      <button class="primary" :disabled="busy">{{ busy ? "登录中…" : "登录" }}</button>
      <p v-if="err" class="error" style="padding: 0">{{ err }}</p>
    </form>
  </div>
</template>
