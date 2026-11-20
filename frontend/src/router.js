import { createRouter, createWebHistory } from 'vue-router'
import Loom from './pages/Loom.vue'
import Ingest from './pages/Ingest.vue'
import Quarantine from './pages/Quarantine.vue'
import Proofs from './pages/Proofs.vue'
import ClosingAttestations from './pages/ClosingAttestations.vue'
import Settings from './pages/Settings.vue'

export default createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', component: Loom },
    { path: '/ingest', component: Ingest },
    { path: '/quarantine', component: Quarantine },
    { path: '/proofs', component: Proofs },
    { path: '/closing-attestations', component: ClosingAttestations },
    { path: '/settings', component: Settings },
  ],
})
