<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'

const rows = ref([])
const err = ref('')

onMounted(async () => {
  try {
    rows.value = await api.quarantine()
  } catch (e) {
    err.value = String(e.message || e)
  }
})
</script>

<template>
  <section class="panel">
    <h1>搁置区</h1>
    <p class="muted">脏击点 / 冲突组搁置落库后在此可见（功能题填充）。</p>
    <p v-if="err" class="err">{{ err }}</p>
    <p v-if="!rows.length" class="muted">暂无搁置记录。</p>
    <table v-else class="data">
      <thead>
        <tr><th>ID</th><th>访客</th><th>原因</th><th>成员</th><th>时刻</th></tr>
      </thead>
      <tbody>
        <tr v-for="r in rows" :key="r.id">
          <td>{{ r.id }}</td>
          <td>{{ r.visitor_id }}</td>
          <td>{{ r.reason }}</td>
          <td class="mono">{{ r.member_json }}</td>
          <td class="mono">{{ r.created_at }}</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>
