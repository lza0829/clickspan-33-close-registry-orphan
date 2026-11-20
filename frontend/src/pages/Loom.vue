<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'

const overview = ref(null)
const sessions = ref([])
const details = ref({})
const err = ref('')
const busyId = ref('')
const attestations = ref({})
const closingAttestations = ref({})

// 织机顶栏「当前会话」：只认访客 visitor_open 物化指针且状态仍 open 的会话，
// 已关 id 永远不会被点亮（指针在关闭事务内同成败卸掉/改指）。
function isCurrentOpen(s) {
  return s.status === 'open' && s.visitor_open === s.id
}

async function load() {
  err.value = ''
  try {
    overview.value = await api.overview()
    sessions.value = await api.sessions()
    const map = {}
    for (const s of sessions.value) {
      map[s.id] = await api.session(s.id)
    }
    details.value = map
  } catch (e) {
    err.value = String(e.message || e)
  }
}

async function close(s) {
  if (!window.confirm(`确认显式关闭 ${s.id}？关闭后成员固化、收流拒收。`)) return
  busyId.value = s.id
  err.value = ''
  try {
    await api.closeSession(s.id)
    await load()
    await verify(s.id)
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busyId.value = ''
  }
}

async function verify(id) {
  try {
    attestations.value = { ...attestations.value, [id]: await api.attestation(id) }
  } catch (e) {
    err.value = String(e.message || e)
  }
}

async function verifyClosing(id) {
  try {
    closingAttestations.value = {
      ...closingAttestations.value,
      [id]: await api.closingAttestation(id),
    }
  } catch (e) {
    err.value = String(e.message || e)
  }
}

onMounted(load)
</script>

<template>
  <!-- plant-ui -->

  <div>
    <div class="stats" v-if="overview">
      <div class="stat"><b>{{ overview.visitors }}</b><span>访客</span></div>
      <div class="stat"><b>{{ overview.sessions }}</b><span>会话</span></div>
      <div class="stat"><b>{{ overview.hits_primary }}</b><span>正式击点</span></div>
      <div class="stat"><b>{{ overview.quarantine }}</b><span>隔离</span></div>
      <div class="stat"><b>{{ overview.idle_timeout_sec }}s</b><span>空闲超时</span></div>
    </div>
    <p v-if="err" class="err">{{ err }}</p>

    <section class="panel">
      <h1>会话织机</h1>
      <p class="muted">显式关闭后成员 id 列表与摘要码固化登记，织机按登记只读渲染，收流闸拒收新击点。</p>
      <div class="lane-list">
        <article
          v-for="s in sessions"
          :key="s.id"
          class="lane"
          :class="{ 'lane-closed': s.status === 'closed' }"
        >
          <div class="lane-head">
            <div>
              <strong>{{ s.id }}</strong>
              <span class="muted"> · {{ s.visitor_id }} · </span>
              <span :class="s.status === 'closed' ? 'badge badge-closed' : 'badge badge-open'">
                {{ s.status === 'closed' ? 'closed 已固化' : 'open' }}
              </span>
              <span v-if="isCurrentOpen(s)" class="badge badge-current">当前会话</span>
              <span
                v-else-if="s.visitor_open && s.visitor_open !== s.id"
                class="badge badge-ghost-badge"
              >指针 → {{ s.visitor_open }}</span>
              <template v-if="details[s.id]?.closing">
                <div class="mono closing-meta">
                  close_id {{ details[s.id].closing.close_id }} ·
                  关闭于 {{ details[s.id].closing.closed_at }} ·
                  成员 {{ details[s.id].closing.hit_count }} 点 ·
                  哈希 {{ details[s.id].closing.content_hash.slice(0, 16) }}…
                </div>
              </template>
            </div>
            <div class="lane-actions">
              <span class="mono">{{ s.hit_count }} 点 · {{ s.started_at }} → {{ s.ended_at || '…' }}</span>
              <button
                v-if="s.status === 'open'"
                class="btn btn-small"
                :disabled="busyId === s.id"
                @click="close(s)"
              >{{ busyId === s.id ? '关闭中…' : '显式关闭' }}</button>
              <button
                v-else
                class="btn btn-small btn-ghost"
                @click="verify(s.id)"
              >核对</button>
              <button
                v-if="s.status === 'closed'"
                class="btn btn-small btn-ghost"
                @click="verifyClosing(s.id)"
              >关闭登记核证</button>
            </div>
          </div>
          <div class="beads" v-if="details[s.id]">
            <template v-for="(h, i) in details[s.id].hits" :key="h.id">
              <span v-if="i" class="connector" aria-hidden="true"></span>
              <span class="hit-bead" :class="{ 'bead-sealed': s.status === 'closed' }">
                <strong>{{ h.event_name }}</strong>
                <span>{{ h.page_path }}</span>
              </span>
            </template>
          </div>
          <div v-if="attestations[s.id]" class="attestation">
            <template v-if="attestations[s.id].verified">
              <span class="badge badge-ok">核对通过 verified</span>
              <span class="mono">登记成员与当前实况、核对行、摘要码完全一致，关闭后无追加/改写。</span>
            </template>
            <template v-else>
              <span class="badge badge-bad">核对失败</span>
              <span class="mono">{{ attestations[s.id].checks }}</span>
            </template>
          </div>
          <div v-if="closingAttestations[s.id]" class="attestation">
            <template v-if="closingAttestations[s.id].verified">
              <span class="badge badge-ok">关闭登记一致</span>
              <span class="mono">
                status=closed · close_id {{ closingAttestations[s.id].close_id }} ·
                visitor_open {{ closingAttestations[s.id].visitor_open || 'NULL（已卸）' }}
                · 结论 {{ closingAttestations[s.id].conclusion }}
              </span>
            </template>
            <template v-else>
              <span class="badge badge-bad">关闭登记撕裂</span>
              <span class="mono">
                结论 {{ closingAttestations[s.id].conclusion }} ·
                {{ closingAttestations[s.id].checks }}
              </span>
            </template>
          </div>
        </article>
      </div>
    </section>
  </div>
</template>
