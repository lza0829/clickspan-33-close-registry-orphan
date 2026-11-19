<script setup>
import { onMounted, ref } from 'vue'
import { api } from './api.js'

const health = ref(null)
onMounted(async () => {
  try {
    health.value = await api.health()
  } catch {
    health.value = { 正常: false }
  }
})
</script>

<template>
  <div class="loom-shell">
    <header class="mast">
      <div class="mast-top">
        <div class="mark">
          <span class="bead" aria-hidden="true"></span>
          <div>
            <strong>点跨</strong>
            <em>Clickspan</em>
          </div>
        </div>
        <p class="tagline">点击流按空闲间隙织成会话带</p>
        <div class="link-state" :class="{ ok: health?.正常 }">
          {{ health?.正常 ? '链路通' : '待连接' }}
        </div>
      </div>
      <nav class="mast-nav">
        <router-link to="/">织机</router-link>
        <router-link to="/ingest">收流</router-link>
        <router-link to="/quarantine">搁置</router-link>
        <router-link to="/proofs">核证</router-link>
        <router-link to="/settings">切窗</router-link>
      </nav>
    </header>

    <main class="canvas">
      <router-view />
    </main>
  </div>
</template>
