import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import ResumeFlowView from '@/views/ResumeFlowView.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    { path: '/resume-flow', name: 'resume-flow', component: ResumeFlowView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
