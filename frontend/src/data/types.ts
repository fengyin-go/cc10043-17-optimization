/** 纯前端数据层的公共类型：与全栈版后端返回的结构保持一致，换回后端时页面不用改。 */

export type EntryRow = {
  id: number
  status: string
  pending: boolean
  abnormal: boolean
  版本号?: number | null
  凭据状态?: string
  冲突提示?: ConflictHint[]
  引用受理单数?: number
  [field: string]: string | number | boolean | ConflictHint[] | null | undefined
}

export type ModuleMeta = {
  key: string
  name: string
  entity: string
  desc: string
  fields: string[]
  statuses: string[]
  actions: string[]
  actionTargets: Record<string, string>
  metrics: string[]
}

export type PageResult = {
  items: EntryRow[]
  total: number
  page: number
  size: number
}

export type ActionResult = {
  ok: boolean
  message: string
}

export type OverviewResult = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
}

// ---- 检查点确认流转 / 续采受理 ----

export type ConflictHint = {
  受理单号: string
  受理单id: number
  受理起点: number
  重叠区间: [number, number]
  处置: string
}

export type CheckpointRow = EntryRow & {
  版本号: number | null
  确认时间: string | null
  凭据状态: string
  冲突提示: ConflictHint[]
  引用受理单数: number
}

export type ReadSegment = {
  id: number
  受理单id: number
  受理单号: string
  接入任务: string
  分区: string
  起始Offset: number
  结束Offset: number
  读取条数: number
  采用检查点id: number
  采用检查点值: string
  采用版本号: number
  凭据当前状态: string
  凭据告警?: string
  轮次序号: number
  读取时间: string
}

export type Handover = {
  id: number
  受理单号: string
  原检查点值: string
  新检查点值: string
  本轮实读区间: [number, number]
  兜底区间: [number, number] | null
  下轮起点: number
  责任交接: string
  交接时间: string
}

export type Acceptance = {
  id: number
  受理单号: string
  接入任务: string
  分区: string
  续采规则名称: string
  状态: string
  接续检查点id: number
  接续检查点值: string
  接续版本号: number
  起始Offset: number
  已覆盖至: number
  前置受理单: number | null
  后继受理单: number | null
  受理时间: string
  公布时间: string | null
  公布区间: [number, number] | null
  备注: string
  segments: ReadSegment[]
  handovers: Handover[]
  referring_segments?: ReadSegment[]
}
