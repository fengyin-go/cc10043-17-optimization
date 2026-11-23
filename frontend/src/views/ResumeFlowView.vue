<template>
  <section class="page" data-module="resume-flow">
    <header class="page-head">
      <div>
        <h2>检查点续采流转工作台</h2>
        <p class="page-desc">
          受理瞬间冻结接续凭据；受理后新批准的断点不回改本轮，每段实读区间与采用检查点逐段留痕，换点走责任交接。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn ghost" type="button" @click="reloadAll">刷新工作台</button>
      </div>
    </header>

    <p v-if="message" class="flow-message" :class="{ error: messageError }">{{ message }}</p>

    <div class="flow-grid">
      <!-- 左栏：任务候选与受理 -->
      <div class="flow-panel">
        <h3>可用候选检查点</h3>
        <label class="create-field">
          <span>接入任务（精确归属，近名任务互不可见）</span>
          <select v-model="selectedTask" data-testid="task-select" @change="onTaskChange">
            <option v-for="task in taskOptions" :key="task" :value="task">{{ task }}</option>
          </select>
        </label>
        <p class="hint">候选规则：{{ eligible.rule }}</p>
        <table class="data-table compact">
          <thead>
            <tr><th>ID</th><th>检查点值</th><th>版本</th><th>凭据状态</th><th>操作</th></tr>
          </thead>
          <tbody>
            <tr v-for="cp in eligible.items" :key="cp.id" :data-testid="`candidate-${cp.id}`">
              <td>{{ cp.id }}</td>
              <td>{{ cp.检查点值 }}</td>
              <td>V{{ cp.版本号 }}</td>
              <td>
                <span class="badge" :class="badgeClass(cp.凭据状态)">{{ cp.凭据状态 }}</span>
                <div v-if="cp.冲突提示.length" class="conflict-box">
                  <div v-for="hint in cp.冲突提示" :key="hint.受理单id">
                    与{{ hint.受理单号 }}区间{{ hint.重叠区间[0] }}-{{ hint.重叠区间[1] }}冲突：{{ hint.处置 }}
                  </div>
                </div>
              </td>
              <td>
                <button class="link" type="button" @click="openReferences(Number(cp.id))">
                  引用反查({{ cp.引用受理单数 }})
                </button>
              </td>
            </tr>
            <tr v-if="!eligible.items.length">
              <td colspan="5" class="empty-state">该任务没有已确认检查点，草稿与归档点不可作为续采凭据</td>
            </tr>
          </tbody>
        </table>

        <h3>受理新一轮续采</h3>
        <form class="accept-form" @submit.prevent="submitAccept">
          <label class="create-field">
            <span>续采规则（仅已启用）</span>
            <select v-model="acceptForm.policy_id" data-testid="policy-select" required>
              <option v-for="policy in enabledPolicies" :key="policy.id" :value="policy.id">
                {{ policy.规则名称 }}（步长 {{ policy.续采步长 }}）
              </option>
            </select>
          </label>
          <label class="create-field">
            <span>分区/分片</span>
            <input v-model="acceptForm.partition" data-testid="partition-input" placeholder="如 0" required />
          </label>
          <button class="btn primary" data-testid="submit-accept" type="submit" :disabled="busy">
            {{ busy ? '受理中…' : '受理并冻结凭据' }}
          </button>
        </form>
      </div>

      <!-- 右栏：受理单与实读区间明细 -->
      <div class="flow-panel wide">
        <h3>受理单与本次实际续采区间（任务：{{ selectedTask }}）</h3>
        <article v-for="acc in acceptances" :key="acc.id" class="accept-card" :data-testid="`acceptance-${acc.id}`">
          <header class="accept-head">
            <div>
              <strong>{{ acc.受理单号 }}</strong>
              <span class="badge" :class="badgeClass(acc.状态)">{{ acc.状态 }}</span>
              <span class="hint">分区 {{ acc.分区 }} · {{ acc.续采规则名称 }}</span>
            </div>
            <div class="row-actions">
              <button
                v-if="acc.状态 === '进行中'"
                class="btn"
                type="button"
                :data-testid="`read-${acc.id}`"
                @click="submitRead(acc.id)"
              >读取下一段</button>
              <button
                v-if="acc.状态 === '进行中'"
                class="btn"
                type="button"
                @click="openHandover(acc)"
              >交接换点</button>
              <button
                v-if="acc.状态 !== '已公布'"
                class="btn primary"
                type="button"
                @click="submitPublish(acc.id)"
              >公布本轮区间</button>
            </div>
          </header>

          <dl class="credential-grid">
            <div><dt>冻结接续凭据</dt><dd>检查点 {{ acc.接续检查点值 }} / V{{ acc.接续版本号 }}（ID {{ acc.接续检查点id }}）</dd></div>
            <div><dt>本轮起始 Offset</dt><dd>{{ acc.起始Offset }}</dd></div>
            <div><dt>已覆盖至</dt><dd>{{ acc.已覆盖至 }}</dd></div>
            <div><dt>公布覆盖区间</dt><dd>{{ acc.公布区间 ? `${acc.公布区间[0]}-${acc.公布区间[1]}` : '未公布' }}</dd></div>
            <div><dt>前置/后继受理单</dt><dd>{{ acc.前置受理单 ?? '—' }} / {{ acc.后继受理单 ?? '—' }}</dd></div>
            <div><dt>备注</dt><dd>{{ acc.备注 || '—' }}</dd></div>
          </dl>

          <table class="data-table compact">
            <thead>
              <tr>
                <th>段序</th><th>分区</th><th>实读区间</th><th>条数</th>
                <th>采用检查点</th><th>凭据当前状态</th><th>归属受理单</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="seg in acc.segments" :key="seg.id" :data-testid="`segment-${seg.id}`">
                <td>第{{ seg.轮次序号 }}段</td>
                <td>{{ seg.分区 }}</td>
                <td>{{ seg.起始Offset }}-{{ seg.结束Offset }}</td>
                <td>{{ seg.读取条数 }}</td>
                <td>{{ seg.采用检查点值 }} / V{{ seg.采用版本号 }}</td>
                <td>
                  {{ seg.凭据当前状态 }}
                  <div v-if="seg.凭据告警" class="conflict-box">{{ seg.凭据告警 }}</div>
                </td>
                <td>{{ seg.受理单号 }}</td>
              </tr>
              <tr v-if="!acc.segments.length">
                <td colspan="7" class="empty-state">尚未读取，受理区间 {{ acc.起始Offset }} 起的责任已挂在本单</td>
              </tr>
            </tbody>
          </table>

          <div v-for="ho in acc.handovers" :key="ho.id" class="handover-box" :data-testid="`handover-${ho.id}`">
            <strong>换点交接：</strong>
            检查点 {{ ho.原检查点值 }} → {{ ho.新检查点值 }}；
            本轮实读 {{ ho.本轮实读区间[0] }}-{{ ho.本轮实读区间[1] }}；
            兜底区间 {{ ho.兜底区间 ? `${ho.兜底区间[0]}-${ho.兜底区间[1]}` : '无空缺' }}；
            下轮起点 {{ ho.下轮起点 }}
            <p>{{ ho.责任交接 }}</p>
          </div>
        </article>
        <p v-if="!acceptances.length" class="empty-state">该任务暂无受理单，可在左侧受理新一轮续采。</p>
      </div>
    </div>

    <!-- 引用反查弹窗 -->
    <div v-if="referenceView" class="modal-backdrop" data-testid="reference-modal" @click.self="referenceView = null">
      <div class="modal">
        <h3>检查点 {{ referenceView.checkpoint.检查点值 }} 的引用留痕</h3>
        <p class="hint">当前状态：{{ referenceView.checkpoint.凭据状态 }}，共 {{ referenceView.reference_count }} 张受理单引用（含已归档场景）。</p>
        <div v-for="acc in referenceView.acceptances" :key="acc.id" class="handover-box">
          <strong>{{ acc.受理单号 }}（{{ acc.状态 }}）</strong>
          <div v-for="seg in acc.referring_segments" :key="seg.id">
            分区{{ seg.分区 }} 实读 {{ seg.起始Offset }}-{{ seg.结束Offset }}，采用 {{ seg.采用检查点值 }}/V{{ seg.采用版本号 }}
          </div>
        </div>
        <p v-if="!referenceView.acceptances.length" class="empty-state">暂无受理单引用该检查点。</p>
        <div class="page-actions"><button class="btn primary" type="button" @click="referenceView = null">关闭</button></div>
      </div>
    </div>

    <!-- 交接换点弹窗 -->
    <div v-if="handoverTarget" class="modal-backdrop" data-testid="handover-modal" @click.self="handoverTarget = null">
      <div class="modal">
        <h3>{{ handoverTarget.受理单号 }} 交接换点</h3>
        <p class="hint">仅可选择同任务、已确认且断点更高的检查点；旧单实读责任不移交。</p>
        <label class="create-field">
          <span>新检查点</span>
          <select v-model.number="handoverCpId">
            <option v-for="cp in handoverOptions" :key="cp.id" :value="Number(cp.id)">
              ID {{ cp.id }} · 值 {{ cp.检查点值 }} · V{{ cp.版本号 }} · {{ cp.凭据状态 }}
            </option>
          </select>
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="handoverTarget = null">取消</button>
          <button class="btn primary" type="button" :disabled="busy || !handoverCpId" @click="submitHandover">确认交接</button>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  acceptResume,
  fetchCheckpointReferences,
  fetchEligibleCheckpoints,
  fetchTaskAcceptances,
  handoverCheckpoint,
  publishRound,
  readNextSegment,
} from '@/api/resume-flow'
import { listEntries } from '@/api/backend-service'
import type { Acceptance, CheckpointRow, EntryRow } from '@/data/types'

const busy = ref(false)
const message = ref('')
const messageError = ref(false)
const allCheckpoints = ref<EntryRow[]>([])
const allPolicies = ref<EntryRow[]>([])
const selectedTask = ref('')
const eligible = ref<{ task: string; total: number; items: CheckpointRow[]; rule: string }>({
  task: '', total: 0, items: [], rule: '',
})
const acceptances = ref<Acceptance[]>([])
const referenceView = ref<Awaited<ReturnType<typeof fetchCheckpointReferences>> | null>(null)
const handoverTarget = ref<Acceptance | null>(null)
const handoverCpId = ref<number | null>(null)
const acceptForm = ref({ policy_id: 0, partition: '0' })

const taskOptions = computed(() => {
  const names = new Set<string>()
  for (const row of allCheckpoints.value) names.add(String(row.关联接入任务))
  return [...names]
})
const enabledPolicies = computed(() => allPolicies.value.filter((row) => row.status === '已启用'))
const handoverOptions = computed(() =>
  eligible.value.items.filter((cp) => Number(cp.检查点值) > Number(handoverTarget.value?.接续检查点值 ?? -1)),
)

function notify(text: string, isError = false) {
  message.value = text
  messageError.value = isError
}

function badgeClass(status: string) {
  if (status.includes('在用') || status === '进行中') return 'badge-blue'
  if (status.includes('冲突')) return 'badge-amber'
  if (status.includes('归档') || status.includes('不可用')) return 'badge-gray'
  if (status.includes('已公布') || status.includes('可用')) return 'badge-green'
  return 'badge-gray'
}

async function loadBaseLists() {
  const [cpPage, policyPage] = await Promise.all([
    listEntries('record_checkpoint', {}),
    listEntries('resume_policy', {}),
  ])
  allCheckpoints.value = cpPage.items
  allPolicies.value = policyPage.items
  if (!selectedTask.value && taskOptions.value.length) {
    selectedTask.value = taskOptions.value.includes('订单接入任务A')
      ? '订单接入任务A'
      : taskOptions.value[0]
  }
  if (!acceptForm.value.policy_id && enabledPolicies.value.length) {
    acceptForm.value.policy_id = Number(enabledPolicies.value[0].id)
  }
}

async function loadEligible() {
  if (!selectedTask.value) return
  eligible.value = await fetchEligibleCheckpoints(selectedTask.value)
}

async function loadAcceptances() {
  if (!selectedTask.value) return
  const view = await fetchTaskAcceptances(selectedTask.value)
  acceptances.value = view.items
}

async function reloadAll() {
  try {
    await loadBaseLists()
    await Promise.all([loadEligible(), loadAcceptances()])
  } catch (error) {
    notify(error instanceof Error ? error.message : '工作台刷新失败', true)
  }
}

async function onTaskChange() {
  await Promise.all([loadEligible(), loadAcceptances()])
}

async function submitAccept() {
  busy.value = true
  try {
    const result = await acceptResume({
      policy_id: acceptForm.value.policy_id,
      task: selectedTask.value,
      partition: acceptForm.value.partition || '0',
    })
    notify(result.message)
    await reloadAll()
  } catch (error) {
    notify(error instanceof Error ? error.message : '受理失败', true)
  } finally {
    busy.value = false
  }
}

async function submitRead(acceptanceId: number) {
  busy.value = true
  try {
    const result = await readNextSegment(acceptanceId)
    notify(result.message)
    await Promise.all([loadEligible(), loadAcceptances()])
  } catch (error) {
    notify(error instanceof Error ? error.message : '读取失败', true)
  } finally {
    busy.value = false
  }
}

async function submitPublish(acceptanceId: number) {
  busy.value = true
  try {
    const result = await publishRound(acceptanceId)
    notify(result.message)
    await loadAcceptances()
  } catch (error) {
    notify(error instanceof Error ? error.message : '公布失败', true)
  } finally {
    busy.value = false
  }
}

function openHandover(acc: Acceptance) {
  handoverTarget.value = acc
  handoverCpId.value = handoverOptions.value[0] ? Number(handoverOptions.value[0].id) : null
}

async function submitHandover() {
  if (!handoverTarget.value || handoverCpId.value == null) return
  busy.value = true
  try {
    const result = await handoverCheckpoint(handoverTarget.value.id, handoverCpId.value)
    notify(result.message)
    handoverTarget.value = null
    await reloadAll()
  } catch (error) {
    notify(error instanceof Error ? error.message : '交接失败', true)
  } finally {
    busy.value = false
  }
}

async function openReferences(checkpointId: number) {
  try {
    referenceView.value = await fetchCheckpointReferences(checkpointId)
  } catch (error) {
    notify(error instanceof Error ? error.message : '引用反查失败', true)
  }
}

onMounted(reloadAll)
</script>
