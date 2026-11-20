<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'

const rows = ref([])
const err = ref('')
const busy = ref(false)

async function load() {
  busy.value = true
  err.value = ''
  try {
    rows.value = await api.closingAttestations()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

onMounted(load)
</script>

<template>
  <!-- plant-ui -->

  <section class="panel">
    <h1>关闭登记核证</h1>
    <p class="muted">
      只读核证：会话状态、close_id 关闭登记、visitor_open 指针三方比对。
      结论 consistent 表示同成败；登记悬空 / 指针未卸撕裂会在此亮红。
    </p>
    <button class="btn btn-small" :disabled="busy" @click="load">
      {{ busy ? '核证中…' : '刷新核证' }}
    </button>
    <p v-if="err" class="err">{{ err }}</p>
    <p v-if="!busy && rows.length === 0" class="muted">暂无已关闭会话。</p>
    <table v-if="rows.length" class="data">
      <thead>
        <tr>
          <th>会话</th><th>访客</th><th>status</th><th>close_id</th>
          <th>visitor_open</th><th>结论</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="r in rows" :key="r.session_id">
          <td class="mono">{{ r.session_id }}</td>
          <td>{{ r.visitor_id }}</td>
          <td>
            <span :class="r.status === 'closed' ? 'badge badge-closed' : 'badge badge-open'">
              {{ r.status }}
            </span>
          </td>
          <td class="mono">{{ r.close_id || '— 缺登记 —' }}</td>
          <td class="mono">{{ r.visitor_open || 'NULL（已卸）' }}</td>
          <td>
            <span :class="r.verified ? 'badge badge-ok' : 'badge badge-bad'">
              {{ r.verified ? 'consistent' : r.conclusion }}
            </span>
          </td>
        </tr>
      </tbody>
    </table>
  </section>
</template>
