import { useCallback, useEffect, useState } from 'react'
import {
  Button,
  Card,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import type { TableColumnsType } from 'antd'
import {
  CopyOutlined,
  DeleteOutlined,
  ReloadOutlined,
  SearchOutlined,
} from '@ant-design/icons'
import { apiFetch } from '@/lib/utils'

const { Text, Paragraph } = Typography

interface TaskLogDetailSummary {
  task_id?: string
  attempt_no?: number | null
  total_count?: number | null
  source?: string
  proxy?: string
  has_logs?: boolean
  log_count?: number
  latest_log?: string
  started_at?: number
  finished_at?: number
  duration_ms?: number
}

interface TaskLogDetail extends TaskLogDetailSummary {
  logs?: string[]
  meta?: Record<string, unknown>
  request?: Record<string, unknown>
}

interface TaskLogItem {
  id: number
  created_at: string
  platform: string
  email: string
  status: 'success' | 'failed'
  error: string
  detail_json?: string
  detail_summary?: TaskLogDetailSummary
}

interface TaskLogListResponse {
  page: number
  page_size: number
  total: number
  items: TaskLogItem[]
}

interface TaskLogDetailResponse extends TaskLogItem {
  detail?: TaskLogDetail
}

interface TaskLogBatchDeleteResponse {
  deleted: number
  not_found: number[]
  total_requested: number
}

const PLATFORM_OPTIONS = [
  { value: '', label: '全部平台' },
  { value: 'chatgpt', label: 'ChatGPT' },
  { value: 'cursor', label: 'Cursor' },
  { value: 'grok', label: 'Grok' },
  { value: 'kiro', label: 'Kiro' },
  { value: 'openblocklabs', label: 'OpenBlockLabs' },
  { value: 'tavily', label: 'Tavily' },
  { value: 'trae', label: 'Trae' },
]

const STATUS_OPTIONS = [
  { value: '', label: '全部状态' },
  { value: 'success', label: '成功' },
  { value: 'failed', label: '失败' },
]

const SOURCE_OPTIONS = [
  { value: '', label: '全部来源' },
  { value: 'manual', label: '手动' },
  { value: 'cpa_replenish', label: 'CPA 补注册' },
]

const PAGE_SIZE_OPTIONS = [20, 50, 100]

function formatDateTime(value?: string | number | null) {
  if (!value) return '-'
  const date = typeof value === 'number' ? new Date(value * 1000) : new Date(value)
  return Number.isNaN(date.getTime()) ? '-' : date.toLocaleString('zh-CN')
}

function formatDuration(durationMs?: number) {
  const value = Number(durationMs || 0)
  if (!value) return '-'
  if (value < 1000) return `${value} ms`
  if (value < 60_000) return `${(value / 1000).toFixed(1).replace(/\.0$/, '')} s`
  return `${(value / 60_000).toFixed(1).replace(/\.0$/, '')} min`
}

function sourceLabel(source?: string) {
  if (source === 'manual') return '手动'
  if (source === 'cpa_replenish') return 'CPA 补注册'
  return source || '-'
}

export default function TaskHistory() {
  const [logs, setLogs] = useState<TaskLogItem[]>([])
  const [total, setTotal] = useState(0)
  const [platform, setPlatform] = useState('')
  const [status, setStatus] = useState('')
  const [source, setSource] = useState('')
  const [keywordInput, setKeywordInput] = useState('')
  const [keyword, setKeyword] = useState('')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [loading, setLoading] = useState(false)
  const [detailLoading, setDetailLoading] = useState(false)
  const [selectedRowKeys, setSelectedRowKeys] = useState<number[]>([])
  const [activeDetail, setActiveDetail] = useState<TaskLogDetailResponse | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const params = new URLSearchParams({
        page: String(page),
        page_size: String(pageSize),
      })
      if (platform) params.set('platform', platform)
      if (status) params.set('status', status)
      if (source) params.set('source', source)
      if (keyword) params.set('keyword', keyword)

      const data = await apiFetch(`/tasks/logs?${params}`) as TaskLogListResponse
      const nextItems = Array.isArray(data.items) ? data.items : []
      setLogs(nextItems)
      setTotal(data.total || 0)
      setSelectedRowKeys((prev) => prev.filter((key) => nextItems.some((item) => item.id === key)))
    } finally {
      setLoading(false)
    }
  }, [keyword, page, pageSize, platform, source, status])

  useEffect(() => {
    void load()
  }, [load])

  const openDetail = async (record: TaskLogItem) => {
    setDetailLoading(true)
    setActiveDetail({
      ...record,
      detail: {
        ...(record.detail_summary || {}),
        logs: [],
      },
    })
    try {
      const data = await apiFetch(`/tasks/logs/${record.id}`) as TaskLogDetailResponse
      setActiveDetail(data)
    } catch (e: any) {
      message.error(e?.message || '读取任务日志详情失败')
    } finally {
      setDetailLoading(false)
    }
  }

  const copyActiveLogs = async () => {
    const text = (activeDetail?.detail?.logs || []).join('\n')
    if (!text) {
      message.warning('当前没有可复制的日志')
      return
    }
    try {
      await navigator.clipboard.writeText(text)
      message.success('日志已复制')
    } catch {
      message.error('复制日志失败')
    }
  }

  const handleBatchDelete = async () => {
    if (selectedRowKeys.length === 0) return

    const result = await apiFetch('/tasks/logs/batch-delete', {
      method: 'POST',
      body: JSON.stringify({ ids: selectedRowKeys }),
    }) as TaskLogBatchDeleteResponse

    message.success(`已删除 ${result.deleted} 条任务历史`)
    if (result.not_found.length > 0) {
      message.warning(`${result.not_found.length} 条记录不存在或已被删除`)
    }
    setSelectedRowKeys([])
    if (logs.length === selectedRowKeys.length && page > 1) {
      setPage((current) => Math.max(1, current - 1))
      return
    }
    await load()
  }

  const columns: TableColumnsType<TaskLogItem> = [
    {
      title: '时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 180,
      render: (text: string) => formatDateTime(text),
    },
    {
      title: '平台',
      dataIndex: 'platform',
      key: 'platform',
      width: 110,
      render: (text: string) => <Tag>{text || '-'}</Tag>,
    },
    {
      title: '邮箱',
      dataIndex: 'email',
      key: 'email',
      width: 220,
      render: (text: string) => (
        <span style={{ fontFamily: 'monospace', fontSize: 12 }}>
          {text || '-'}
        </span>
      ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 90,
      render: (value: string) => (
        <Tag color={value === 'success' ? 'success' : 'error'}>
          {value === 'success' ? '成功' : '失败'}
        </Tag>
      ),
    },
    {
      title: '任务信息',
      key: 'detail_summary',
      width: 220,
      render: (_, record) => {
        const summary = record.detail_summary || {}
        return (
          <Space direction="vertical" size={4}>
            <Space size={6} wrap>
              <Tag color="blue">{sourceLabel(summary.source)}</Tag>
              {summary.attempt_no ? (
                <Tag>
                  第 {summary.attempt_no}/{summary.total_count || '?'} 次
                </Tag>
              ) : null}
              {summary.log_count ? <Tag>{summary.log_count} 行日志</Tag> : null}
            </Space>
            <Text type="secondary">
              任务 ID：{summary.task_id || '-'}
            </Text>
            <Text type="secondary">
              耗时：{formatDuration(summary.duration_ms)}
            </Text>
          </Space>
        )
      },
    },
    {
      title: '最近日志',
      key: 'latest_log',
      render: (_, record) => {
        const latest = record.detail_summary?.latest_log || record.error || '-'
        return (
          <Paragraph
            ellipsis={{ rows: 2, expandable: false }}
            style={{ marginBottom: 0 }}
          >
            {latest}
          </Paragraph>
        )
      },
    },
    {
      title: '操作',
      key: 'actions',
      width: 120,
      render: (_, record) => (
        <Button
          type="link"
          style={{ padding: 0 }}
          onClick={() => void openDetail(record)}
        >
          查看详情
        </Button>
      ),
    },
  ]

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <Modal
        open={Boolean(activeDetail)}
        title="注册日志详情"
        width={960}
        onCancel={() => setActiveDetail(null)}
        onOk={() => setActiveDetail(null)}
        okText="关闭"
        cancelButtonProps={{ style: { display: 'none' } }}
        confirmLoading={detailLoading}
        footer={[
          <Button
            key="copy"
            icon={<CopyOutlined />}
            onClick={copyActiveLogs}
            disabled={(activeDetail?.detail?.logs || []).length === 0}
          >
            复制日志
          </Button>,
          <Button key="ok" type="primary" onClick={() => setActiveDetail(null)}>
            关闭
          </Button>,
        ]}
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10, marginBottom: 12 }}>
          <Space wrap>
            <Tag>{activeDetail?.platform || '-'}</Tag>
            <Tag color={activeDetail?.status === 'success' ? 'success' : 'error'}>
              {activeDetail?.status === 'success' ? '成功' : '失败'}
            </Tag>
            <Tag color="blue">{sourceLabel(activeDetail?.detail?.source)}</Tag>
          </Space>
          <div>邮箱：{activeDetail?.email || '-'}</div>
          <div>任务 ID：{activeDetail?.detail?.task_id || '-'}</div>
          <div>
            尝试序号：
            {activeDetail?.detail?.attempt_no
              ? `${activeDetail.detail.attempt_no}/${activeDetail.detail.total_count || '?'}`
              : '-'}
          </div>
          <div>代理：{activeDetail?.detail?.proxy || '-'}</div>
          <div>开始时间：{formatDateTime(activeDetail?.detail?.started_at)}</div>
          <div>结束时间：{formatDateTime(activeDetail?.detail?.finished_at)}</div>
          <div>执行耗时：{formatDuration(activeDetail?.detail?.duration_ms)}</div>
          {activeDetail?.error ? (
            <div style={{ color: '#ef4444' }}>错误信息：{activeDetail.error}</div>
          ) : null}
        </div>

        <div style={{ marginBottom: 8, fontWeight: 500 }}>日志内容</div>
        <pre
          style={{
            margin: 0,
            maxHeight: 420,
            overflow: 'auto',
            padding: 12,
            borderRadius: 8,
            background: 'rgba(127,127,127,0.08)',
            fontSize: 12,
            lineHeight: 1.6,
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
          }}
        >
          {(activeDetail?.detail?.logs || []).join('\n') || '暂无日志'}
        </pre>
      </Modal>

      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
        <div>
          <h1 style={{ fontSize: 24, fontWeight: 'bold', margin: 0 }}>任务历史</h1>
          <p style={{ color: '#7a8ba3', marginTop: 4 }}>支持按平台、状态、来源和关键词筛选，并查看完整注册日志</p>
        </div>
        <Space wrap>
          <Text type="secondary">{total} 条记录</Text>
          {selectedRowKeys.length > 0 ? <Text type="success">已选 {selectedRowKeys.length} 条</Text> : null}
          {selectedRowKeys.length > 0 ? (
            <Popconfirm
              title={`确认删除选中的 ${selectedRowKeys.length} 条任务历史？`}
              onConfirm={() => void handleBatchDelete()}
            >
              <Button danger icon={<DeleteOutlined />}>
                删除 {selectedRowKeys.length} 条
              </Button>
            </Popconfirm>
          ) : null}
          <Button icon={<ReloadOutlined spin={loading} />} onClick={() => void load()} loading={loading} />
        </Space>
      </div>

      <Card>
        <Space wrap style={{ marginBottom: 16 }}>
          <Select
            value={platform}
            onChange={(value) => {
              setPlatform(value)
              setPage(1)
              setSelectedRowKeys([])
            }}
            style={{ width: 140 }}
            options={PLATFORM_OPTIONS}
          />
          <Select
            value={status}
            onChange={(value) => {
              setStatus(value)
              setPage(1)
              setSelectedRowKeys([])
            }}
            style={{ width: 120 }}
            options={STATUS_OPTIONS}
          />
          <Select
            value={source}
            onChange={(value) => {
              setSource(value)
              setPage(1)
              setSelectedRowKeys([])
            }}
            style={{ width: 160 }}
            options={SOURCE_OPTIONS}
          />
          <Input
            value={keywordInput}
            onChange={(event) => setKeywordInput(event.target.value)}
            onPressEnter={() => {
              setKeyword(keywordInput.trim())
              setPage(1)
            }}
            style={{ width: 260 }}
            placeholder="搜索邮箱 / 任务 ID / 错误 / 日志关键字"
            prefix={<SearchOutlined />}
            allowClear
          />
          <Button
            type="primary"
            onClick={() => {
              setKeyword(keywordInput.trim())
              setPage(1)
            }}
          >
            搜索
          </Button>
          <Button
            onClick={() => {
              setPlatform('')
              setStatus('')
              setSource('')
              setKeyword('')
              setKeywordInput('')
              setPage(1)
              setPageSize(20)
              setSelectedRowKeys([])
            }}
          >
            重置
          </Button>
        </Space>

        <Table
          rowKey="id"
          columns={columns}
          dataSource={logs}
          loading={loading}
          rowSelection={{
            selectedRowKeys,
            onChange: (keys) => setSelectedRowKeys(keys as number[]),
          }}
          pagination={{
            current: page,
            pageSize,
            total,
            showSizeChanger: true,
            pageSizeOptions: PAGE_SIZE_OPTIONS.map(String),
            showTotal: (count) => `共 ${count} 条任务历史`,
            onChange: (nextPage, nextPageSize) => {
              if ((nextPageSize || pageSize) !== pageSize) {
                setPageSize(nextPageSize || pageSize)
                setPage(1)
                return
              }
              setPage(nextPage)
            },
          }}
        />
      </Card>
    </div>
  )
}
