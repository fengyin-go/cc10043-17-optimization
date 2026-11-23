<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th v-if="moduleKey === 'record_checkpoint'">版本</th>
          <th v-if="moduleKey === 'record_checkpoint'">凭据状态与冲突</th>
          <th v-if="moduleKey === 'record_checkpoint'">引用受理单</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td v-if="moduleKey === 'record_checkpoint'">
            {{ row.版本号 ? `V${row.版本号}` : '未占版' }}
          </td>
          <td v-if="moduleKey === 'record_checkpoint'">
            <span class="badge" :class="credentialBadge(String(row.凭据状态))">{{ row.凭据状态 ?? '—' }}</span>
            <div v-if="conflictHints(row).length" class="conflict-box">
              <div v-for="hint in conflictHints(row)" :key="hint.受理单id">
                与{{ hint.受理单号 }}区间{{ hint.重叠区间[0] }}-{{ hint.重叠区间[1] }}冲突：{{ hint.处置 }}
              </div>
            </div>
          </td>
          <td v-if="moduleKey === 'record_checkpoint'">
            <button class="link" type="button" :data-testid="`refs-${row.id}`" @click="showReferences(row)">
              {{ Number(row.引用受理单数 ?? 0) }} 张
            </button>
          </td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <button
              v-for="action in meta.actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2 + (moduleKey === 'record_checkpoint' ? 3 : 0)" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="referenceView" class="modal-backdrop" data-testid="reference-modal" @click.self="referenceView = null">
      <div class="modal">
        <h3>检查点 {{ referenceView.checkpoint.检查点值 }} 的引用留痕</h3>
        <p class="hint">
          当前状态：{{ referenceView.checkpoint.凭据状态 }}，
          共 {{ referenceView.reference_count }} 张受理单引用（停用/归档后仍可反查）。
        </p>
        <div v-for="acc in referenceView.acceptances" :key="acc.id" class="handover-box">
          <strong>{{ acc.受理单号 }}（{{ acc.状态 }}）</strong>
          <div v-for="seg in acc.referring_segments" :key="seg.id">
            分区{{ seg.分区 }} 实读 {{ seg.起始Offset }}-{{ seg.结束Offset }}，
            采用 {{ seg.采用检查点值 }}/V{{ seg.采用版本号 }}
          </div>
        </div>
        <p v-if="!referenceView.acceptances.length" class="empty-state">暂无受理单引用该检查点。</p>
        <div class="page-actions"><button class="btn primary" type="button" @click="referenceView = null">关闭</button></div>
      </div>
    </div>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="field in meta.fields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  createEntry,
  downloadEntries,
  listEntries,
  moduleMeta,
  runAction as applyAction,
} from '@/api/backend-service'
import { fetchCheckpointReferences } from '@/api/resume-flow'
import type { ConflictHint, EntryRow } from '@/data/types'

const props = defineProps<{ moduleKey: string }>()
const meta = moduleMeta(props.moduleKey)
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = meta.fields.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const referenceView = ref<Awaited<ReturnType<typeof fetchCheckpointReferences>> | null>(null)
const stats = computed(() => [
  { label: meta.metrics[0], value: total.value },
  { label: meta.metrics[1], value: rows.value.filter((row) => row.status === meta.statuses[1]).length },
  {
    label: meta.metrics[2],
    value: rows.value.filter((row) => row.status === meta.statuses[meta.statuses.length - 1]).length,
  },
])
const statusSummary = computed(() =>
  meta.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)

function resetFilters() {
  filters.value = {}
  void reload()
}

function conflictHints(row: EntryRow): ConflictHint[] {
  return Array.isArray(row.冲突提示) ? (row.冲突提示 as ConflictHint[]) : []
}

function credentialBadge(status: string) {
  if (status.includes('在用')) return 'badge-blue'
  if (status.includes('冲突')) return 'badge-amber'
  if (status.includes('归档') || status.includes('不可用')) return 'badge-gray'
  return 'badge-green'
}

async function showReferences(row: EntryRow) {
  try {
    referenceView.value = await fetchCheckpointReferences(Number(row.id))
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '引用反查失败'
  }
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.fields.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = await createEntry(meta.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  try {
    const result = await applyAction(meta.key, Number(row.id), action)
    if (!result.ok) throw new Error(result.message)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
}

onMounted(reload)
</script>
