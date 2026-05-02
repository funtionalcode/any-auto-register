import {
  createContext,
  memo,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import {
  Badge,
  Button,
  Card,
  Empty,
  Modal,
  Segmented,
  Space,
  Tag,
  Typography,
  message,
  theme,
} from 'antd'
import {
  CheckCircleOutlined,
  CaretDownOutlined,
  CaretRightOutlined,
  CloseCircleOutlined,
  CloseOutlined,
  CopyOutlined,
  FilterOutlined,
  FolderOpenOutlined,
  LinkOutlined,
  LoadingOutlined,
  MinusOutlined,
  ReloadOutlined,
  ShrinkOutlined,
} from '@ant-design/icons'
import { API_BASE, apiFetch } from '@/lib/utils'

const { Text, Paragraph } = Typography

const TERMINAL_STATUSES = new Set(['done', 'failed'])
const TASK_DOCK_FILTER_STORAGE_KEY = 'registerTaskCenter.dockFilter'
const TASK_DOCK_COLLAPSED_STORAGE_KEY = 'registerTaskCenter.collapsedPlatforms'

type RegisterTaskStatus = 'pending' | 'running' | 'done' | 'failed' | string

interface RegisterTaskRequestPayload {
  platform: string
  email?: string | null
  password?: string | null
  count: number
  concurrency?: number
  register_delay_seconds?: number
  proxy?: string | null
  executor_type: string
  captcha_solver: string
  extra: Record<string, unknown>
}

interface ServerRegisterTask {
  id: string
  status: RegisterTaskStatus
  platform: string
  progress?: string
  logs?: string[]
  success?: number
  errors?: string[]
  error?: string
  cashier_urls?: string[]
  created_at?: number
  updated_at?: number
  finished_at?: number | null
  log_count?: number
  latest_log?: string
}

interface ManagedRegisterTask extends ServerRegisterTask {
  createdAt: number
  minimized: boolean
  unseenLogs: number
  summary: string
  platformLabel: string
}

interface RegisterTaskCenterContextValue {
  tasks: ManagedRegisterTask[]
  activeTask: ManagedRegisterTask | null
  completionVersion: number
  launchTask: (payload: RegisterTaskRequestPayload) => Promise<string>
  openTask: (taskId: string) => void
  minimizeTask: (taskId: string) => void
  dismissTask: (taskId: string) => void
  refreshTask: (taskId: string) => Promise<void>
}

interface RegisterTaskActionsContextValue {
  launchTask: (payload: RegisterTaskRequestPayload) => Promise<string>
  openTask: (taskId: string) => void
  minimizeTask: (taskId: string) => void
  dismissTask: (taskId: string) => void
  refreshTask: (taskId: string) => Promise<void>
}

const RegisterTaskCenterContext = createContext<RegisterTaskCenterContextValue | null>(null)

function readStoredDockFilter(): 'all' | 'running' | 'finished' {
  if (typeof window === 'undefined') return 'all'
  const raw = window.localStorage.getItem(TASK_DOCK_FILTER_STORAGE_KEY)
  return raw === 'running' || raw === 'finished' ? raw : 'all'
}

function readStoredCollapsedPlatforms(): string[] {
  if (typeof window === 'undefined') return []
  try {
    const raw = window.localStorage.getItem(TASK_DOCK_COLLAPSED_STORAGE_KEY)
    const parsed = raw ? JSON.parse(raw) : []
    return Array.isArray(parsed) ? parsed.map((item) => String(item || '')).filter(Boolean) : []
  } catch {
    return []
  }
}

function isTerminal(status?: string) {
  return status ? TERMINAL_STATUSES.has(status) : false
}

function getStatusMeta(status: string) {
  if (status === 'done') return { color: 'success' as const, label: '已完成' }
  if (status === 'failed') return { color: 'error' as const, label: '失败' }
  if (status === 'running') return { color: 'processing' as const, label: '运行中' }
  return { color: 'default' as const, label: '排队中' }
}

function formatPlatformLabel(platform: string) {
  const map: Record<string, string> = {
    chatgpt: 'ChatGPT',
    grok: 'Grok',
    trae: 'Trae',
    cursor: 'Cursor',
    kiro: 'Kiro',
    tavily: 'Tavily',
    openblocklabs: 'OpenBlockLabs',
  }
  return map[platform] || platform
}

function extractTaskTimestamp(taskId: string) {
  const match = String(taskId || '').match(/task_(\d+)/)
  return match ? Number(match[1]) : Date.now()
}

function buildTaskSummary(payload: Pick<RegisterTaskRequestPayload, 'count' | 'concurrency' | 'register_delay_seconds'>) {
  const parts = [`批量 ${payload.count || 1}`]
  if ((payload.concurrency || 1) > 1) parts.push(`并发 ${payload.concurrency}`)
  if ((payload.register_delay_seconds || 0) > 0) parts.push(`间隔 ${payload.register_delay_seconds}s`)
  return parts.join(' · ')
}

function createSeedTask(taskId: string, payload: RegisterTaskRequestPayload): ManagedRegisterTask {
  return {
    id: taskId,
    status: 'pending',
    platform: payload.platform,
    progress: `0/${payload.count || 1}`,
    logs: [],
    errors: [],
    cashier_urls: [],
    createdAt: extractTaskTimestamp(taskId),
    minimized: false,
    unseenLogs: 0,
    summary: buildTaskSummary(payload),
    platformLabel: formatPlatformLabel(payload.platform),
  }
}

function createManagedTask(
  snapshot: ServerRegisterTask,
  previous?: ManagedRegisterTask,
  options?: { minimized?: boolean; unseenLogs?: number },
): ManagedRegisterTask {
  return {
    id: snapshot.id,
    status: snapshot.status || previous?.status || 'pending',
    platform: snapshot.platform || previous?.platform || '',
    progress: snapshot.progress || previous?.progress || '',
    logs: Array.isArray(snapshot.logs) ? snapshot.logs : (previous?.logs || []),
    success: snapshot.success ?? previous?.success,
    errors: Array.isArray(snapshot.errors) ? snapshot.errors : (previous?.errors || []),
    error: snapshot.error ?? previous?.error,
    cashier_urls: Array.isArray(snapshot.cashier_urls) ? snapshot.cashier_urls : (previous?.cashier_urls || []),
    createdAt: previous?.createdAt || extractTaskTimestamp(snapshot.id),
    minimized: options?.minimized ?? previous?.minimized ?? true,
    unseenLogs: options?.unseenLogs ?? previous?.unseenLogs ?? 0,
    summary: previous?.summary || buildTaskSummary({
      count: Number(String(snapshot.progress || '').split('/')[1]) || 1,
      concurrency: 1,
      register_delay_seconds: 0,
    }),
    platformLabel: previous?.platformLabel || formatPlatformLabel(snapshot.platform),
  }
}

function sortTasks(tasks: ManagedRegisterTask[]) {
  return [...tasks].sort((a, b) => b.createdAt - a.createdAt)
}

function groupTasksByPlatform(tasks: ManagedRegisterTask[]) {
  const groups = new Map<string, { platform: string; label: string; items: ManagedRegisterTask[] }>()

  tasks.forEach((task) => {
    const key = task.platform || 'unknown'
    if (!groups.has(key)) {
      groups.set(key, {
        platform: key,
        label: task.platformLabel || formatPlatformLabel(key),
        items: [],
      })
    }
    groups.get(key)?.items.push(task)
  })

  return [...groups.values()]
    .map((group) => ({ ...group, items: sortTasks(group.items) }))
    .sort((a, b) => b.items.length - a.items.length || a.label.localeCompare(b.label))
}

const LogPanel = memo(function LogPanel({
  logs,
  taskId,
}: {
  logs: string[]
  taskId: string
}) {
  const { token } = theme.useToken()
  if (logs.length === 0) {
    return (
      <div
        className="log-panel"
        style={{
          overflow: 'auto',
          background: token.colorBgContainer,
          border: `1px solid ${token.colorBorder}`,
          borderRadius: 12,
          padding: 12,
          fontFamily: 'monospace',
          fontSize: 12,
          minHeight: 220,
          maxHeight: 420,
          userSelect: 'text',
          WebkitUserSelect: 'text',
          cursor: 'text',
          whiteSpace: 'pre-wrap',
        }}
      >
        <div style={{ color: token.colorTextTertiary }}>等待任务日志...</div>
      </div>
    )
  }
  return (
    <div
      className="log-panel"
      style={{
        overflow: 'auto',
        background: token.colorBgContainer,
        border: `1px solid ${token.colorBorder}`,
        borderRadius: 12,
        padding: 12,
        fontFamily: 'monospace',
        fontSize: 12,
        minHeight: 220,
        maxHeight: 420,
        userSelect: 'text',
        WebkitUserSelect: 'text',
        cursor: 'text',
        whiteSpace: 'pre-wrap',
      }}
    >
      {logs.map((line, index) => {
        const positive = line.includes('\u2713') || line.includes('成功')
        const negative = line.includes('\u2717') || line.includes('失败') || line.includes('错误')
        return (
          <div
            key={`${taskId}-${index}`}
            style={{
              lineHeight: 1.5,
              color: positive ? token.colorSuccess : negative ? token.colorError : token.colorText,
            }}
          >
            {line}
          </div>
        )
      })}
    </div>
  )
})

const TaskStatusBlock = memo(function TaskStatusBlockInner({
  task,
  onRefresh,
}: {
  task: ManagedRegisterTask
  onRefresh: () => void
}) {
  const { token } = theme.useToken()
  const statusMeta = getStatusMeta(task.status)

  const copyLogs = async () => {
    try {
      await navigator.clipboard.writeText((task.logs || []).join('\n'))
      message.success('任务日志已复制')
    } catch {
      message.error('复制任务日志失败')
    }
  }

  const openCashierUrls = () => {
    ;(task.cashier_urls || []).forEach((url) => window.open(url, '_blank', 'noopener,noreferrer'))
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div
        aria-live="polite"
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(160px, 1fr))',
          gap: 12,
        }}
      >
        <Card size="small" bordered={false} style={{ background: token.colorBgLayout }}>
          <Text type="secondary">任务状态</Text>
          <div style={{ marginTop: 8 }}>
            <Tag color={statusMeta.color}>{statusMeta.label}</Tag>
          </div>
        </Card>
        <Card size="small" bordered={false} style={{ background: token.colorBgLayout }}>
          <Text type="secondary">当前进度</Text>
          <div style={{ marginTop: 8, fontSize: 18, fontWeight: 600 }}>{task.progress || '-'}</div>
        </Card>
        <Card size="small" bordered={false} style={{ background: token.colorBgLayout }}>
          <Text type="secondary">成功 / 失败</Text>
          <div style={{ marginTop: 8, fontSize: 18, fontWeight: 600 }}>
            {task.success ?? 0} / {(task.errors || []).length}
          </div>
        </Card>
      </div>

      {task.error ? (
        <div style={{ color: token.colorError }}>
          <CloseCircleOutlined /> {task.error}
        </div>
      ) : null}

      {(task.errors || []).length > 0 ? (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          {(task.errors || []).map((item, index) => (
            <div key={`${task.id}-error-${index}`} style={{ color: token.colorError }}>
              <CloseCircleOutlined /> {item}
            </div>
          ))}
        </div>
      ) : null}

      {(task.cashier_urls || []).length > 0 ? (
        <Card size="small" bordered={false} style={{ background: token.colorBgLayout }}>
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            <Text strong>升级链接</Text>
            {(task.cashier_urls || []).map((url, index) => (
              <Space key={`${task.id}-cashier-${index}`} wrap>
                <Text copyable={{ text: url }} style={{ wordBreak: 'break-all' }}>
                  {url}
                </Text>
                <Button
                  size="small"
                  icon={<LinkOutlined />}
                  onClick={() => window.open(url, '_blank', 'noopener,noreferrer')}
                >
                  打开
                </Button>
              </Space>
            ))}
            {(task.cashier_urls || []).length > 1 ? (
              <Button size="small" icon={<LinkOutlined />} onClick={openCashierUrls}>
                打开全部链接
              </Button>
            ) : null}
          </Space>
        </Card>
      ) : null}

      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <Text type="secondary">实时日志</Text>
        <Space>
          <Button size="small" icon={<ReloadOutlined />} onClick={onRefresh}>
            刷新
          </Button>
          <Button size="small" icon={<CopyOutlined />} onClick={copyLogs} disabled={(task.logs || []).length === 0}>
            复制日志
          </Button>
        </Space>
      </div>

      <div
        className="log-panel"
        style={{
          overflow: 'auto',
          background: token.colorBgContainer,
          border: `1px solid ${token.colorBorder}`,
          borderRadius: 12,
          padding: 12,
          fontFamily: 'monospace',
          fontSize: 12,
          minHeight: 220,
          maxHeight: 420,
          userSelect: 'text',
          WebkitUserSelect: 'text',
          cursor: 'text',
          whiteSpace: 'pre-wrap',
        }}
      >
        {(task.logs || []).length === 0 ? (
          <div style={{ color: token.colorTextTertiary }}>等待任务日志...</div>
        ) : (
          (task.logs || []).map((line, index) => {
            const positive = line.includes('✓') || line.includes('成功')
            const negative = line.includes('✗') || line.includes('失败') || line.includes('错误')
            return (
              <div
                key={`${task.id}-log-${index}`}
                style={{
                  lineHeight: 1.5,
                  color: positive ? token.colorSuccess : negative ? token.colorError : token.colorText,
                }}
              >
                {line}
              </div>
            )
          })
        )}
      </div>
    </div>
  )
})

export function RegisterTaskCenterProvider({ children }: { children: ReactNode }) {
  const [tasks, setTasks] = useState<ManagedRegisterTask[]>([])
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null)
  const [completionVersion, setCompletionVersion] = useState(0)
  const [dockOpen, setDockOpen] = useState(false)
  const [dockFilter, setDockFilter] = useState<'all' | 'running' | 'finished'>(() => readStoredDockFilter())
  const [collapsedPlatforms, setCollapsedPlatforms] = useState<string[]>(() => readStoredCollapsedPlatforms())
  const tasksRef = useRef<ManagedRegisterTask[]>([])
  const activeTaskIdRef = useRef<string | null>(null)
  const streamRefs = useRef<Map<string, EventSource>>(new Map())
  const pendingRefreshRef = useRef<Set<string>>(new Set())

  useEffect(() => {
    tasksRef.current = tasks
  }, [tasks])

  useEffect(() => {
    activeTaskIdRef.current = activeTaskId
  }, [activeTaskId])

  useEffect(() => {
    if (typeof window === 'undefined') return
    window.localStorage.setItem(TASK_DOCK_FILTER_STORAGE_KEY, dockFilter)
  }, [dockFilter])

  useEffect(() => {
    if (typeof window === 'undefined') return
    window.localStorage.setItem(
      TASK_DOCK_COLLAPSED_STORAGE_KEY,
      JSON.stringify(collapsedPlatforms),
    )
  }, [collapsedPlatforms])

  const refreshTask = useCallback(async (taskId: string) => {
    if (pendingRefreshRef.current.has(taskId)) return
    pendingRefreshRef.current.add(taskId)
    try {
      const snapshot = (await apiFetch(`/tasks/${taskId}`)) as ServerRegisterTask
      let becameTerminal = false
      let completionTitle = ''

      setTasks((current) => {
        const previous = current.find((item) => item.id === taskId)
        const logsLength = Array.isArray(snapshot.logs) ? snapshot.logs.length : (previous?.logs || []).length
        const newLogCount = previous ? Math.max(0, logsLength - (previous.logs || []).length) : 0
        const hidden = previous ? (previous.minimized || activeTaskIdRef.current !== taskId) : true
        const next = createManagedTask(snapshot, previous, {
          minimized: previous?.minimized ?? (activeTaskIdRef.current === taskId ? false : true),
          unseenLogs: hidden ? (previous?.unseenLogs || 0) + newLogCount : 0,
        })

        if (previous && !isTerminal(previous.status) && isTerminal(next.status)) {
          becameTerminal = true
          completionTitle = `${next.platformLabel} 注册${next.status === 'done' ? '完成' : '失败'}`
        }

        if (!previous) return sortTasks([next, ...current])
        return sortTasks(current.map((item) => (item.id === taskId ? next : item)))
      })

      if (becameTerminal) {
        setCompletionVersion((value) => value + 1)
        if (snapshot.status === 'done') {
          message.success(completionTitle)
        } else if (snapshot.status === 'failed') {
          message.error(completionTitle)
        }
      }
    } finally {
      pendingRefreshRef.current.delete(taskId)
    }
  }, [])

  const launchTask = useCallback(async (payload: RegisterTaskRequestPayload) => {
    const normalizedPayload: RegisterTaskRequestPayload = {
      ...payload,
      count: Math.max(1, Number(payload.count) || 1),
      concurrency: Math.max(1, Number(payload.concurrency) || 1),
      register_delay_seconds: Math.max(0, Number(payload.register_delay_seconds) || 0),
    }
    const result = await apiFetch('/tasks/register', {
      method: 'POST',
      body: JSON.stringify(normalizedPayload),
    }) as { task_id: string }

    setTasks((current) => {
      const next = createSeedTask(result.task_id, normalizedPayload)
      return sortTasks([
        next,
        ...current.map((item) => (
          item.id === activeTaskIdRef.current
            ? { ...item, minimized: true, unseenLogs: 0 }
            : item
        )),
      ])
    })
    setDockOpen(false)
    activeTaskIdRef.current = result.task_id
    setActiveTaskId(result.task_id)
    await refreshTask(result.task_id)
    return result.task_id
  }, [refreshTask])

  const openTask = useCallback((taskId: string) => {
    setTasks((current) => current.map((item) => {
      if (item.id === taskId) return { ...item, minimized: false, unseenLogs: 0 }
      if (item.id === activeTaskIdRef.current) return { ...item, minimized: true }
      return item
    }))
    setDockOpen(false)
    activeTaskIdRef.current = taskId
    setActiveTaskId(taskId)
  }, [])

  const minimizeTask = useCallback((taskId: string) => {
    setTasks((current) => current.map((item) => (
      item.id === taskId ? { ...item, minimized: true, unseenLogs: 0 } : item
    )))
    if (activeTaskIdRef.current === taskId) activeTaskIdRef.current = null
    setActiveTaskId((current) => (current === taskId ? null : current))
  }, [])

  const dismissTask = useCallback((taskId: string) => {
    setTasks((current) => current.filter((item) => item.id !== taskId))
    if (activeTaskIdRef.current === taskId) activeTaskIdRef.current = null
    setActiveTaskId((current) => (current === taskId ? null : current))
  }, [])

  useEffect(() => {
    let cancelled = false

    const loadActiveTasks = async () => {
      try {
        const data = (await apiFetch('/tasks')) as ServerRegisterTask[]
        if (cancelled || !Array.isArray(data)) return
        const restored = data
          .filter((item) => item?.id && !isTerminal(item.status))
          .map((item) => createManagedTask(item, undefined, { minimized: true, unseenLogs: 0 }))
        if (restored.length === 0) return
        setTasks((current) => {
          const known = new Set(current.map((item) => item.id))
          const merged = [...current, ...restored.filter((item) => !known.has(item.id))]
          return sortTasks(merged)
        })
      } catch {
        // ignore hydration failures, the center can still track newly launched tasks
      }
    }

    loadActiveTasks()
    return () => {
      cancelled = true
    }
  }, [])

  // Polling fallback: only refresh tasks that don\'t have an active SSE stream.
  // Tasks with SSE connections receive real-time updates, so they don\'t need polling.
  useEffect(() => {
    if (tasks.length === 0) return

    const timer = window.setInterval(() => {
      tasksRef.current
        .filter((item) => !isTerminal(item.status))
        .forEach((item) => {
          if (!streamRefs.current.has(item.id)) {
            refreshTask(item.id).catch(() => {})
          }
        })
    }, 10000)

    return () => {
      window.clearInterval(timer)
    }
  }, [refreshTask, tasks.length])

  useEffect(() => {
    const streams = streamRefs.current
    const activeTaskIds = new Set(
      tasks
        .filter((item) => !isTerminal(item.status))
        .map((item) => item.id),
    )

    tasks
      .filter((item) => !isTerminal(item.status))
      .forEach((item) => {
        if (streams.has(item.id)) return

        const stream = new EventSource(
          `${API_BASE}/tasks/${encodeURIComponent(item.id)}/logs/stream?since=${Math.max(0, (item.logs || []).length)}`,
        )

        stream.onmessage = (event) => {
          let payload: Record<string, unknown> = {}
          try {
            payload = JSON.parse(event.data || '{}')
          } catch {
            return
          }

          // Batch all updates from a single SSE message into one setTasks call
          // to avoid cascading re-renders that freeze the UI.
          setTasks((current) => {
            const taskIndex = current.findIndex((t) => t.id === item.id)
            if (taskIndex === -1) return current
            let updated = { ...current[taskIndex] }

            if (typeof payload.line === 'string') {
              const line = payload.line
              const index = typeof payload.index === 'number' ? payload.index : null
              const previousLogs = Array.isArray(updated.logs) ? updated.logs : []
              const nextLogs = [...previousLogs]
              if (index !== null && index >= 0) {
                nextLogs[index] = line
              } else {
                nextLogs.push(line)
              }

              const compactLogs = nextLogs.filter((value): value is string => typeof value === 'string')
              const newLogCount = Math.max(0, compactLogs.length - previousLogs.length)
              const hidden = updated.minimized || activeTaskIdRef.current !== item.id

              updated = {
                ...updated,
                logs: compactLogs,
                unseenLogs: hidden ? (updated.unseenLogs || 0) + newLogCount : 0,
              }
            }

            if (payload.snapshot && typeof payload.snapshot === 'object') {
              const snapshot = payload.snapshot as ServerRegisterTask
              const previousLogsLength = Array.isArray(updated.logs) ? updated.logs.length : 0
              const snapshotLogsLength = Array.isArray(snapshot.logs) ? snapshot.logs.length : previousLogsLength
              const newLogCount = Math.max(0, snapshotLogsLength - previousLogsLength)
              const hidden = updated.minimized || activeTaskIdRef.current !== item.id
              updated = createManagedTask(snapshot, updated, {
                minimized: updated.minimized,
                unseenLogs: hidden ? (updated.unseenLogs || 0) + newLogCount : 0,
              })
            }

            const next = current.slice()
            next[taskIndex] = updated
            return next
          })

          if (payload.done) {
            stream.close()
            streams.delete(item.id)
            refreshTask(item.id).catch(() => {})
          }
        }

        stream.onerror = () => {
          if (isTerminal(tasksRef.current.find((task) => task.id === item.id)?.status)) {
            stream.close()
            streams.delete(item.id)
          }
        }

        streams.set(item.id, stream)
      })

    streams.forEach((stream, taskId) => {
      if (activeTaskIds.has(taskId)) return
      stream.close()
      streams.delete(taskId)
    })

    return () => {
      if (tasks.length > 0) return
      streams.forEach((stream) => stream.close())
      streams.clear()
    }
  }, [refreshTask, tasks.length])

  useEffect(() => (
    () => {
      streamRefs.current.forEach((stream) => stream.close())
      streamRefs.current.clear()
    }
  ), [])

  const activeTask = tasks.find((item) => item.id === activeTaskId) || null
  const minimizedTasks = tasks.filter((item) => item.minimized)
  const filteredMinimizedTasks = minimizedTasks.filter((item) => {
    if (dockFilter === 'running') return !isTerminal(item.status)
    if (dockFilter === 'finished') return isTerminal(item.status)
    return true
  })
  const groupedMinimizedTasks = groupTasksByPlatform(filteredMinimizedTasks)
  const runningCount = minimizedTasks.filter((item) => !isTerminal(item.status)).length
  const finishedCount = minimizedTasks.length - runningCount
  const totalUnseenLogs = minimizedTasks.reduce((count, item) => count + (item.unseenLogs || 0), 0)
  const dockBadgeCount = totalUnseenLogs > 0 ? totalUnseenLogs : minimizedTasks.length

  useEffect(() => {
    if (minimizedTasks.length === 0 && dockOpen) {
      setDockOpen(false)
    }
  }, [dockOpen, minimizedTasks.length])

  const minimizeAllTasks = useCallback(() => {
    setTasks((current) => current.map((item) => ({ ...item, minimized: true, unseenLogs: 0 })))
    activeTaskIdRef.current = null
    setActiveTaskId(null)
  }, [])

  const dismissCompletedTasks = useCallback(() => {
    setTasks((current) => current.filter((item) => !isTerminal(item.status)))
    if (activeTaskIdRef.current) {
      const currentActive = tasksRef.current.find((item) => item.id === activeTaskIdRef.current)
      if (currentActive && isTerminal(currentActive.status)) {
        activeTaskIdRef.current = null
        setActiveTaskId(null)
      }
    }
  }, [])

  const togglePlatformCollapsed = useCallback((platform: string) => {
    setCollapsedPlatforms((current) => (
      current.includes(platform)
        ? current.filter((item) => item !== platform)
        : [...current, platform]
    ))
  }, [])

  const collapseAllGroups = useCallback(() => {
    setCollapsedPlatforms(groupedMinimizedTasks.map((group) => group.platform))
  }, [groupedMinimizedTasks])

  const expandAllGroups = useCallback(() => {
    setCollapsedPlatforms([])
  }, [])

  const contextValue = useMemo<RegisterTaskCenterContextValue>(() => ({
    tasks,
    activeTask,
    completionVersion,
    launchTask,
    openTask,
    minimizeTask,
    dismissTask,
    refreshTask,
  }), [tasks, activeTask, completionVersion, launchTask, openTask, minimizeTask, dismissTask, refreshTask])

  return (
    <RegisterTaskCenterContext.Provider value={contextValue}>
      {children}

      <Modal
        open={Boolean(activeTask)}
        title={activeTask ? (
          <Space direction="vertical" size={2}>
            <Space wrap>
              <Text strong>{activeTask.platformLabel} 注册任务</Text>
              <Tag color={getStatusMeta(activeTask.status).color}>{getStatusMeta(activeTask.status).label}</Tag>
            </Space>
            <Text type="secondary">{activeTask.summary}</Text>
          </Space>
        ) : null}
        onCancel={() => activeTask && minimizeTask(activeTask.id)}
        footer={activeTask ? [
          <Button key="minimize" icon={<MinusOutlined />} onClick={() => minimizeTask(activeTask.id)}>
            最小化为右下角图标
          </Button>,
          <Button
            key="remove"
            danger={isTerminal(activeTask.status)}
            icon={<CloseOutlined />}
            onClick={() => dismissTask(activeTask.id)}
          >
            {isTerminal(activeTask.status) ? '关闭面板' : '停止跟踪'}
          </Button>,
        ] : null}
        width={820}
        maskClosable
        destroyOnHidden={false}
      >
        {activeTask ? (
          <TaskStatusBlock task={activeTask} onRefresh={() => refreshTask(activeTask.id)} />
        ) : (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无任务" />
        )}
      </Modal>

      {minimizedTasks.length > 0 ? (
        <>
          {dockOpen ? (
            <div className="register-task-dock">
              <Card size="small" className="register-task-dock-toolbar">
                <Space direction="vertical" size={10} style={{ width: '100%' }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12 }}>
                    <Space size={8}>
                      <FolderOpenOutlined />
                      <Text strong>后台任务中心</Text>
                    </Space>
                    <Space size={8}>
                      <Tag color="processing">已收起 {minimizedTasks.length}</Tag>
                      <Button
                        type="text"
                        size="small"
                        icon={<CloseOutlined />}
                        onClick={() => setDockOpen(false)}
                        aria-label="收起后台任务中心"
                      />
                    </Space>
                  </div>
                  <Space size={8} wrap>
                    <Text type="secondary">运行中 {runningCount}</Text>
                    <Text type="secondary">已完成 {finishedCount}</Text>
                    <Text type="secondary">平台 {groupedMinimizedTasks.length}</Text>
                  </Space>
                  <Space direction="vertical" size={8} style={{ width: '100%' }}>
                    <Space size={8} wrap>
                      <FilterOutlined />
                      <Text type="secondary">筛选</Text>
                    </Space>
                    <Segmented
                      block
                      value={dockFilter}
                      onChange={(value) => setDockFilter(value as 'all' | 'running' | 'finished')}
                      options={[
                        { label: '全部', value: 'all' },
                        { label: '运行中', value: 'running' },
                        { label: '已完成', value: 'finished' },
                      ]}
                    />
                  </Space>
                  <Space size={8} wrap>
                    <Button
                      size="small"
                      icon={<ShrinkOutlined />}
                      onClick={minimizeAllTasks}
                      disabled={tasks.every((item) => item.minimized)}
                    >
                      全部最小化
                    </Button>
                    <Button
                      size="small"
                      danger
                      icon={<CloseOutlined />}
                      onClick={dismissCompletedTasks}
                      disabled={finishedCount === 0}
                    >
                      清理已完成
                    </Button>
                    <Button
                      size="small"
                      icon={<CaretDownOutlined />}
                      onClick={expandAllGroups}
                      disabled={collapsedPlatforms.length === 0}
                    >
                      展开分组
                    </Button>
                    <Button
                      size="small"
                      icon={<CaretRightOutlined />}
                      onClick={collapseAllGroups}
                      disabled={groupedMinimizedTasks.length === 0 || groupedMinimizedTasks.every((group) => collapsedPlatforms.includes(group.platform))}
                    >
                      折叠分组
                    </Button>
                  </Space>
                </Space>
              </Card>

              {groupedMinimizedTasks.length === 0 ? (
                <Card size="small" className="register-task-dock-card">
                  <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前筛选下没有任务" />
                </Card>
              ) : groupedMinimizedTasks.map((group) => (
                <div key={group.platform} className="register-task-group">
                  <button
                    type="button"
                    className="register-task-group-toggle"
                    onClick={() => togglePlatformCollapsed(group.platform)}
                    aria-expanded={!collapsedPlatforms.includes(group.platform)}
                  >
                    <Space size={8} wrap>
                      {collapsedPlatforms.includes(group.platform) ? <CaretRightOutlined /> : <CaretDownOutlined />}
                      <Text strong>{group.label}</Text>
                      <Tag>{group.items.length} 个</Tag>
                      <Tag color="processing">
                        {group.items.filter((item) => !isTerminal(item.status)).length} 运行中
                      </Tag>
                    </Space>
                  </button>

                  {!collapsedPlatforms.includes(group.platform) && group.items.map((task) => {
                    const statusMeta = getStatusMeta(task.status)
                    const latestLog = (task.logs || []).slice(-1)[0] || '任务后台运行中'
                    return (
                      <Badge count={task.unseenLogs} size="small" key={task.id} offset={[-8, 8]}>
                        <Card
                          size="small"
                          className="register-task-dock-card"
                          title={
                            <Space size={8}>
                              {statusMeta.label === '运行中' ? <LoadingOutlined /> : null}
                              <span>{task.platformLabel}</span>
                            </Space>
                          }
                          extra={<Tag color={statusMeta.color}>{task.progress || statusMeta.label}</Tag>}
                          actions={[
                            <Button key="open" type="link" size="small" onClick={() => openTask(task.id)}>
                              查看
                            </Button>,
                            <Button
                              key="close"
                              type="link"
                              size="small"
                              danger={isTerminal(task.status)}
                              disabled={!isTerminal(task.status)}
                              onClick={() => dismissTask(task.id)}
                            >
                              关闭
                            </Button>,
                          ]}
                        >
                          <Space direction="vertical" size={6} style={{ width: '100%' }}>
                            <Text strong>{task.summary}</Text>
                            <Paragraph
                              ellipsis={{ rows: 2 }}
                              style={{ marginBottom: 0, minHeight: 40, color: 'inherit' }}
                            >
                              {latestLog}
                            </Paragraph>
                            <Space size={8} wrap>
                              {(task.success || 0) > 0 ? (
                                <Text style={{ color: '#10b981' }}>
                                  <CheckCircleOutlined /> 成功 {task.success}
                                </Text>
                              ) : null}
                              {(task.errors || []).length > 0 ? (
                                <Text type="danger">
                                  <CloseCircleOutlined /> 失败 {(task.errors || []).length}
                                </Text>
                              ) : null}
                            </Space>
                          </Space>
                        </Card>
                      </Badge>
                    )
                  })}
                </div>
              ))}
            </div>
          ) : null}

          <div className="register-task-fab-anchor">
            <Badge count={dockBadgeCount} overflowCount={99} size="small">
              <Button
                type={runningCount > 0 ? 'primary' : 'default'}
                shape="circle"
                className={`register-task-fab${dockOpen ? ' is-open' : ''}${runningCount > 0 ? ' is-running' : ''}`}
                icon={<FolderOpenOutlined />}
                onClick={() => setDockOpen((current) => !current)}
                aria-label={dockOpen ? '收起后台任务中心' : '展开后台任务中心'}
                title={dockOpen ? '收起后台任务中心' : `后台任务中心：${minimizedTasks.length} 个任务`}
              />
            </Badge>
          </div>
        </>
      ) : null}
    </RegisterTaskCenterContext.Provider>
  )
}

export function useRegisterTaskCenter() {
  const context = useContext(RegisterTaskCenterContext)
  if (!context) {
    throw new Error('useRegisterTaskCenter must be used within RegisterTaskCenterProvider')
  }
  return context
}
