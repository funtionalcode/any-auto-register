import { useEffect, useState, useCallback } from 'react'
import { useParams } from 'react-router-dom'
import {
  Table,
  Button,
  Input,
  InputNumber,
  Select,
  Tag,
  Space,
  Modal,
  Form,
  message,
  Popconfirm,
  Dropdown,
  Typography,
  Alert,
} from 'antd'
import type { MenuProps } from 'antd'
import {
  ReloadOutlined,
  CopyOutlined,
  LinkOutlined,
  PlusOutlined,
  DownloadOutlined,
  UploadOutlined,
  MoreOutlined,
  DeleteOutlined,
} from '@ant-design/icons'
import { apiFetch } from '@/lib/utils'
import { normalizeExecutorForPlatform } from '@/lib/registerOptions'
import { useRegisterTaskCenter } from '@/components/RegisterTaskCenter'

const { Text } = Typography

const STATUS_COLORS: Record<string, string> = {
  registered: 'default',
  trial: 'success',
  subscribed: 'success',
  expired: 'warning',
  invalid: 'error',
}

const DEFAULT_ACCOUNT_PAGE_SIZE = 20
const ACCOUNT_PAGE_SIZE_OPTIONS = [20, 50, 100, 200]
const ACCOUNT_PAGE_SIZE_STORAGE_PREFIX = 'accounts.pageSize.'

function readPersistedAccountPageSize(platform: string) {
  if (typeof window === 'undefined') return DEFAULT_ACCOUNT_PAGE_SIZE
  const raw = window.localStorage.getItem(`${ACCOUNT_PAGE_SIZE_STORAGE_PREFIX}${platform}`)
  const value = Number(raw || DEFAULT_ACCOUNT_PAGE_SIZE)
  return ACCOUNT_PAGE_SIZE_OPTIONS.includes(value) ? value : DEFAULT_ACCOUNT_PAGE_SIZE
}

function persistAccountPageSize(platform: string, pageSize: number) {
  if (typeof window === 'undefined') return
  window.localStorage.setItem(`${ACCOUNT_PAGE_SIZE_STORAGE_PREFIX}${platform}`, String(pageSize))
}

function parseExtraJson(raw: string | undefined) {
  if (!raw) return {}
  try {
    const parsed = JSON.parse(raw)
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    return {}
  }
}

function normalizeAccount(account: any) {
  const extra = parseExtraJson(account.extra_json)
  const syncStatuses = extra.sync_statuses && typeof extra.sync_statuses === 'object' ? extra.sync_statuses : {}
  const cpaSync = syncStatuses.cpa && typeof syncStatuses.cpa === 'object' ? syncStatuses.cpa : {}
  return { ...account, extra, cpaSync }
}

function formatSyncTime(value?: string) {
  if (!value) return ''
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

function ActionMenu({ acc, onRefresh }: { acc: any; onRefresh: () => void }) {
  const [actions, setActions] = useState<any[]>([])
  const [resultOpen, setResultOpen] = useState(false)
  const [resultTitle, setResultTitle] = useState('')
  const [resultStatus, setResultStatus] = useState<'success' | 'error'>('success')
  const [resultText, setResultText] = useState('')
  const [resultUrl, setResultUrl] = useState('')

  useEffect(() => {
    apiFetch(`/actions/${acc.platform}`)
      .then((d) => setActions(d.actions || []))
      .catch(() => {})
  }, [acc.platform])

  const showResult = (title: string, status: 'success' | 'error', text: string, url = '') => {
    setResultTitle(title)
    setResultStatus(status)
    setResultText(text)
    setResultUrl(url)
    setResultOpen(true)
  }

  const copyResultUrl = async () => {
    if (!resultUrl) return
    try {
      await navigator.clipboard.writeText(resultUrl)
      message.success('链接已复制')
    } catch {
      message.error('复制失败')
    }
  }

  const handleAction = async (actionId: string) => {
    const actionLabel = actions.find((item) => item.id === actionId)?.label || actionId

    try {
      const r = await apiFetch(`/actions/${acc.platform}/${acc.id}/${actionId}`, {
        method: 'POST',
        body: JSON.stringify({ params: {} }),
      })
      if (!r.ok) {
        showResult(actionLabel, 'error', r.error || '操作失败')
        return
      }
      const data = r.data || {}
      if (data.url || data.checkout_url || data.cashier_url) {
        const targetUrl = data.url || data.checkout_url || data.cashier_url
        message.success('链接已生成')
        showResult(actionLabel, 'success', '操作成功，请在弹窗中打开或复制链接。', targetUrl)
      } else {
        message.success(data.message || '操作成功')
        const text =
          typeof data === 'string'
            ? data
            : Object.keys(data).length > 0
              ? JSON.stringify(data, null, 2)
              : '操作成功'
        showResult(actionLabel, 'success', text)
      }
      onRefresh()
    } catch (e: any) {
      const detail = e?.message ? String(e.message) : '请求失败'
      message.error(detail)
      showResult(actionLabel, 'error', detail)
    }
  }

  const menuItems: MenuProps['items'] = actions.map((a) => ({
    key: a.id,
    label: a.label,
  }))

  if (actions.length === 0) return null

  return (
    <>
      <Dropdown
        menu={{
          items: menuItems,
          onClick: ({ key }) => handleAction(String(key)),
        }}
      >
        <Button type="link" size="small" icon={<MoreOutlined />} />
      </Dropdown>
      <Modal
        title={resultTitle}
        open={resultOpen}
        onCancel={() => setResultOpen(false)}
        footer={[
          resultUrl ? (
            <Button key="copy" onClick={copyResultUrl}>
              复制链接
            </Button>
          ) : null,
          resultUrl ? (
            <Button
              key="open"
              type="primary"
              onClick={() => window.open(resultUrl, '_blank', 'noopener,noreferrer')}
            >
              打开链接
            </Button>
          ) : null,
          <Button key="ok" type={resultUrl ? 'default' : 'primary'} onClick={() => setResultOpen(false)}>
            确定
          </Button>,
        ].filter(Boolean)}
        maskClosable={false}
      >
        <Alert
          type={resultStatus}
          showIcon
          message={resultStatus === 'success' ? '操作完成' : '操作失败'}
          style={{ marginBottom: 12 }}
        />
        {resultUrl ? (
          <Space direction="vertical" style={{ width: '100%' }}>
            <Text copyable={{ text: resultUrl }} style={{ wordBreak: 'break-all' }}>
              {resultUrl}
            </Text>
          </Space>
        ) : null}
        {resultText ? (
          <pre
            style={{
              margin: 0,
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
              fontFamily: 'monospace',
              fontSize: 12,
            }}
          >
            {resultText}
          </pre>
        ) : null}
      </Modal>
    </>
  )
}

export default function Accounts() {
  const { launchTask, completionVersion } = useRegisterTaskCenter()
  const { platform } = useParams<{ platform: string }>()
  const [currentPlatform, setCurrentPlatform] = useState(platform || 'trae')
  const [accounts, setAccounts] = useState<any[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [search, setSearch] = useState('')
  const [filterStatus, setFilterStatus] = useState('')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(() => readPersistedAccountPageSize(platform || 'trae'))
  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([])

  const [registerModalOpen, setRegisterModalOpen] = useState(false)
  const [addModalOpen, setAddModalOpen] = useState(false)
  const [importModalOpen, setImportModalOpen] = useState(false)
  const [detailModalOpen, setDetailModalOpen] = useState(false)
  const [currentAccount, setCurrentAccount] = useState<any>(null)

  const [registerForm] = Form.useForm()
  const [addForm] = Form.useForm()
  const [detailForm] = Form.useForm()
  const [importText, setImportText] = useState('')
  const [importLoading, setImportLoading] = useState(false)
  const [registerLoading, setRegisterLoading] = useState(false)
  const [cpaSyncLoading, setCpaSyncLoading] = useState<'pending' | 'selected' | ''>('')

  useEffect(() => {
    if (platform) setCurrentPlatform(platform)
  }, [platform])

  useEffect(() => {
    setPage(1)
    setPageSize(readPersistedAccountPageSize(currentPlatform))
    setSelectedRowKeys([])
  }, [currentPlatform])

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const params = new URLSearchParams({
        platform: currentPlatform,
        page: String(page),
        page_size: String(pageSize),
      })
      if (search) params.set('email', search)
      if (filterStatus) params.set('status', filterStatus)
      const data = await apiFetch(`/accounts?${params}`)
      const nextTotal = Number(data.total || 0)
      const maxPage = Math.max(1, Math.ceil(nextTotal / pageSize))
      if (page > maxPage) {
        setPage(maxPage)
        return
      }
      setAccounts((data.items || []).map(normalizeAccount))
      setTotal(nextTotal)
    } finally {
      setLoading(false)
    }
  }, [currentPlatform, filterStatus, page, pageSize, search])

  useEffect(() => {
    load()
  }, [load])

  useEffect(() => {
    if (completionVersion === 0) return
    load()
  }, [completionVersion, load])

  const copyText = (text: string) => {
    navigator.clipboard.writeText(text)
    message.success('已复制')
  }

  const getRefreshToken = (record: any): string => {
    try {
      const extra = JSON.parse(record.extra_json || '{}')
      return extra.refresh_token || ''
    } catch {
      return ''
    }
  }

  const exportCsv = () => {
    const params = new URLSearchParams()
    params.set('platform', currentPlatform)
    if (filterStatus) params.set('status', filterStatus)
    window.open(`/api/accounts/export?${params.toString()}`, '_blank', 'noopener,noreferrer')
  }

  const handleDelete = async (id: number) => {
    await apiFetch(`/accounts/${id}`, { method: 'DELETE' })
    message.success('删除成功')
    load()
  }

  const handleBatchDelete = async () => {
    if (selectedRowKeys.length === 0) return
    await apiFetch('/accounts/batch-delete', {
      method: 'POST',
      body: JSON.stringify({ ids: Array.from(selectedRowKeys) }),
    })
    message.success('批量删除成功')
    setSelectedRowKeys([])
    load()
  }

  const handleAdd = async () => {
    const values = await addForm.validateFields()
    await apiFetch('/accounts', {
      method: 'POST',
      body: JSON.stringify({ ...values, platform: currentPlatform }),
    })
    message.success('添加成功')
    setAddModalOpen(false)
    addForm.resetFields()
    load()
  }

  const handleImport = async () => {
    if (!importText.trim()) return
    setImportLoading(true)
    try {
      const lines = importText.trim().split('\n').filter(Boolean)
      const res = await apiFetch('/accounts/import', {
        method: 'POST',
        body: JSON.stringify({ platform: currentPlatform, lines }),
      })
      message.success(`导入成功 ${res.created} 个`)
      setImportModalOpen(false)
      setImportText('')
      load()
    } catch (e: any) {
      message.error(`导入失败: ${e.message}`)
    } finally {
      setImportLoading(false)
    }
  }

  const handleRegister = async () => {
    const values = await registerForm.validateFields()
    setRegisterLoading(true)
    try {
      const cfg = await apiFetch('/config')
      const executorType = normalizeExecutorForPlatform(currentPlatform, cfg.default_executor)
      await launchTask({
        platform: currentPlatform,
        count: values.count,
        concurrency: values.concurrency,
        register_delay_seconds: values.register_delay_seconds || 0,
        executor_type: executorType,
        captcha_solver: cfg.default_captcha_solver || 'yescaptcha',
        proxy: null,
        extra: {
          mail_provider: cfg.mail_provider || 'laoudo',
          laoudo_auth: cfg.laoudo_auth,
          laoudo_email: cfg.laoudo_email,
          laoudo_account_id: cfg.laoudo_account_id,
          maliapi_base_url: cfg.maliapi_base_url,
          maliapi_api_key: cfg.maliapi_api_key,
          maliapi_domain: cfg.maliapi_domain,
          maliapi_auto_domain_strategy: cfg.maliapi_auto_domain_strategy,
          yescaptcha_key: cfg.yescaptcha_key,
          moemail_api_url: cfg.moemail_api_url,
          skymail_api_base: cfg.skymail_api_base,
          skymail_token: cfg.skymail_token,
          skymail_domain: cfg.skymail_domain,
          duckmail_address: cfg.duckmail_address,
          duckmail_password: cfg.duckmail_password,
          duckmail_api_url: cfg.duckmail_api_url,
          duckmail_provider_url: cfg.duckmail_provider_url,
          duckmail_bearer: cfg.duckmail_bearer,
          freemail_api_url: cfg.freemail_api_url,
          freemail_admin_token: cfg.freemail_admin_token,
          freemail_username: cfg.freemail_username,
          freemail_password: cfg.freemail_password,
          cfworker_api_url: cfg.cfworker_api_url,
          cfworker_admin_token: cfg.cfworker_admin_token,
          cfworker_custom_auth: cfg.cfworker_custom_auth,
          cfworker_domain: cfg.cfworker_domain,
          cfworker_fingerprint: cfg.cfworker_fingerprint,
          smstome_cookie: cfg.smstome_cookie,
          smstome_country_slugs: cfg.smstome_country_slugs,
          smstome_phone_attempts: cfg.smstome_phone_attempts,
          smstome_otp_timeout_seconds: cfg.smstome_otp_timeout_seconds,
          smstome_poll_interval_seconds: cfg.smstome_poll_interval_seconds,
          smstome_sync_max_pages_per_country: cfg.smstome_sync_max_pages_per_country,
        },
      })
      message.success('注册任务已启动，可最小化到右下角后台执行')
      setRegisterModalOpen(false)
    } catch (e: any) {
      message.error(`启动注册任务失败: ${e?.message || e || '未知错误'}`)
    } finally {
      setRegisterLoading(false)
    }
  }

  const handleDetailSave = async () => {
    const values = await detailForm.validateFields()
    await apiFetch(`/accounts/${currentAccount.id}`, {
      method: 'PATCH',
      body: JSON.stringify(values),
    })
    message.success('保存成功')
    setDetailModalOpen(false)
    load()
  }

  const showCpaSyncResult = (title: string, result: any) => {
    const lines = (result.items || [])
      .flatMap((item: any) =>
        (item.results || []).map((syncResult: any) => ({
          email: item.email,
          platform: item.platform,
          ok: Boolean(syncResult.ok),
          name: syncResult.name || 'CPA',
          msg: syncResult.msg || '',
        })),
      )
      .filter((item: any) => !item.ok)
      .map((item: any) => `[${item.platform}] ${item.email || '-'} / ${item.name}: ${item.msg || '失败'}`)

    if (lines.length === 0) return

    Modal.info({
      title,
      width: 760,
      content: (
        <pre
          style={{
            margin: 0,
            maxHeight: 360,
            overflow: 'auto',
            padding: 12,
            borderRadius: 8,
            background: 'rgba(127,127,127,0.08)',
            fontSize: 12,
            lineHeight: 1.5,
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
          }}
        >
          {lines.join('\n')}
        </pre>
      ),
    })
  }

  const handleCpaBackfill = async (mode: 'pending' | 'selected') => {
    if (currentPlatform !== 'chatgpt') return

    const body: Record<string, unknown> = {
      platforms: ['chatgpt'],
    }

    if (mode === 'selected') {
      const accountIds = Array.from(selectedRowKeys)
        .map((value) => Number(value))
        .filter((value) => Number.isInteger(value) && value > 0)

      if (accountIds.length === 0) {
        message.warning('请先选择要上传的账号')
        return
      }
      body.account_ids = accountIds
    } else {
      body.pending_only = true
      if (filterStatus) body.status = filterStatus
      if (search) body.email = search
    }

    setCpaSyncLoading(mode)
    try {
      const result = await apiFetch('/integrations/backfill', {
        method: 'POST',
        body: JSON.stringify(body),
      })

      const actionLabel = mode === 'selected' ? '所选账号 CPA 上传' : '未上传账号 CPA 补传'
      if (!result.total) {
        message.info('没有可处理的账号')
      } else if (!result.failed) {
        message.success(`${actionLabel}完成：成功 ${result.success} / ${result.total}`)
      } else if (!result.success) {
        message.error(`${actionLabel}失败：成功 ${result.success} / ${result.total}`)
      } else {
        message.warning(`${actionLabel}部分完成：成功 ${result.success} / ${result.total}`)
      }

      showCpaSyncResult(`${actionLabel}结果`, result)
      await load()
    } catch (e: any) {
      message.error(`CPA 上传失败: ${e.message}`)
    } finally {
      setCpaSyncLoading('')
    }
  }

  const columns: any[] = [
    {
      title: '邮箱',
      dataIndex: 'email',
      key: 'email',
      render: (text: string) => (
        <Text copyable={{ text }} style={{ fontFamily: 'monospace', fontSize: 12 }}>
          {text}
        </Text>
      ),
    },
    {
      title: '密码',
      dataIndex: 'password',
      key: 'password',
      render: (text: string) => (
        <Space>
          <Text style={{ fontFamily: 'monospace', fontSize: 12, filter: 'blur(4px)' }}>{text}</Text>
          <Button type="text" size="small" icon={<CopyOutlined />} onClick={() => copyText(text)} />
        </Space>
      ),
    },
    {
      title: 'RT',
      key: 'refresh_token',
      render: (_: any, record: any) => {
        const rt = getRefreshToken(record)
        if (!rt) return <span style={{ color: '#ccc' }}>-</span>
        return (
          <Space>
            <Text style={{ fontFamily: 'monospace', fontSize: 11, filter: 'blur(4px)', maxWidth: 80, overflow: 'hidden', display: 'inline-block', verticalAlign: 'middle' }}>
              {rt.slice(0, 16)}
            </Text>
            <Button type="text" size="small" icon={<CopyOutlined />} onClick={() => copyText(rt)} />
          </Space>
        )
      },
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      render: (status: string) => <Tag color={STATUS_COLORS[status] || 'default'}>{status}</Tag>,
    },
    {
      title: '地区',
      dataIndex: 'region',
      key: 'region',
      render: (text: string) => text || '-',
    },
    {
      title: '试用链接',
      dataIndex: 'cashier_url',
      key: 'cashier_url',
      render: (url: string) =>
        url ? (
          <Space>
            <Button type="text" size="small" icon={<CopyOutlined />} onClick={() => copyText(url)} />
            <Button type="text" size="small" icon={<LinkOutlined />} onClick={() => window.open(url, '_blank')} />
          </Space>
        ) : (
          '-'
        ),
    },
    {
      title: '注册时间',
      dataIndex: 'created_at',
      key: 'created_at',
      render: (text: string) => (text ? new Date(text).toLocaleDateString() : '-'),
    },
    {
      title: '操作',
      key: 'action',
      render: (_: any, record: any) => (
        <Space>
          <Button type="link" size="small" onClick={() => { setCurrentAccount(record); setDetailModalOpen(true); }}>
            详情
          </Button>
          <Popconfirm title="确认删除？" onConfirm={() => handleDelete(record.id)}>
            <Button type="link" size="small" danger>
              删除
            </Button>
          </Popconfirm>
          <ActionMenu acc={record} onRefresh={load} />
        </Space>
      ),
    },
  ]

  if (currentPlatform === 'chatgpt') {
    columns.splice(4, 0, {
      title: 'CPA',
      key: 'cpa_sync',
      render: (_: any, record: any) => {
        const sync = record.cpaSync || {}
        const uploaded = Boolean(sync.uploaded || sync.uploaded_at)
        const attempted = Boolean(sync.last_attempt_at)
        const color = uploaded ? 'success' : attempted ? 'error' : 'default'
        const label = uploaded ? '已上传' : attempted ? '最近失败' : '未上传'
        const time = uploaded ? sync.uploaded_at : sync.last_attempt_at

        return (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 4, minWidth: 140 }}>
            <Tag color={color}>{label}</Tag>
            {time ? (
              <Text type="secondary" style={{ fontSize: 12 }}>
                {formatSyncTime(time)}
              </Text>
            ) : null}
            {sync.last_message ? (
              <Text type="secondary" ellipsis={{ tooltip: sync.last_message }} style={{ maxWidth: 220, fontSize: 12 }}>
                {sync.last_message}
              </Text>
            ) : null}
          </div>
        )
      },
    })
  }

  return (
    <div>
      <div style={{ marginBottom: 16, display: 'flex', justifyContent: 'space-between', flexWrap: 'wrap', gap: 8 }}>
        <Space>
          <Input.Search
            placeholder="搜索邮箱..."
            allowClear
            onSearch={(value) => {
              setSearch(value)
              setPage(1)
            }}
            style={{ width: 200 }}
          />
          <Select
            placeholder="状态筛选"
            allowClear
            style={{ width: 120 }}
            onChange={(value) => {
              setFilterStatus(value || '')
              setPage(1)
            }}
            options={[
              { value: 'registered', label: '已注册' },
              { value: 'trial', label: '试用中' },
              { value: 'subscribed', label: '已订阅' },
              { value: 'expired', label: '已过期' },
              { value: 'invalid', label: '已失效' },
            ]}
          />
          <Text type="secondary">{total} 个账号</Text>
          {selectedRowKeys.length > 0 && (
            <Text type="success">已选 {selectedRowKeys.length} 个</Text>
          )}
        </Space>
        <Space>
          {currentPlatform === 'chatgpt' && selectedRowKeys.length > 0 && (
            <Popconfirm
              title={`确认上传选中的 ${selectedRowKeys.length} 个账号到 CPA？`}
              onConfirm={() => handleCpaBackfill('selected')}
            >
              <Button loading={cpaSyncLoading === 'selected'} icon={<UploadOutlined />}>
                上传所选 CPA
              </Button>
            </Popconfirm>
          )}
          {currentPlatform === 'chatgpt' && (
            <Popconfirm
              title="确认补传当前筛选范围内尚未成功上传 CPA 的账号？"
              onConfirm={() => handleCpaBackfill('pending')}
            >
              <Button loading={cpaSyncLoading === 'pending'} icon={<UploadOutlined />} disabled={total === 0}>
                补传未上传 CPA
              </Button>
            </Popconfirm>
          )}
          {selectedRowKeys.length > 0 && (
            <Popconfirm title={`确认删除选中的 ${selectedRowKeys.length} 个账号？`} onConfirm={handleBatchDelete}>
              <Button danger icon={<DeleteOutlined />}>删除 {selectedRowKeys.length} 个</Button>
            </Popconfirm>
          )}
          <Button icon={<UploadOutlined />} onClick={() => setImportModalOpen(true)}>导入</Button>
          <Button icon={<DownloadOutlined />} onClick={exportCsv} disabled={accounts.length === 0}>导出</Button>
          <Button icon={<PlusOutlined />} onClick={() => setAddModalOpen(true)}>新增</Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setRegisterModalOpen(true)}>注册</Button>
          <Button icon={<ReloadOutlined spin={loading} />} onClick={load} />
        </Space>
      </div>

      <Table
        rowKey="id"
        columns={columns}
        dataSource={accounts}
        loading={loading}
        rowSelection={{
          selectedRowKeys,
          onChange: setSelectedRowKeys,
        }}
        pagination={{
          current: page,
          pageSize,
          total,
          showSizeChanger: true,
          pageSizeOptions: ACCOUNT_PAGE_SIZE_OPTIONS.map(String),
          showTotal: (count) => `共 ${count} 个账号`,
          onChange: (nextPage, nextPageSize) => {
            const normalizedPageSize = nextPageSize || pageSize
            if (normalizedPageSize !== pageSize) {
              persistAccountPageSize(currentPlatform, normalizedPageSize)
              setPageSize(normalizedPageSize)
              setPage(1)
            } else {
              setPage(nextPage)
            }
          },
        }}
        onRow={(record) => ({
          onDoubleClick: () => {
            setCurrentAccount(record)
            setDetailModalOpen(true)
          },
        })}
      />

      <Modal
        title={`注册 ${currentPlatform}`}
        open={registerModalOpen}
        onCancel={() => setRegisterModalOpen(false)}
        footer={null}
        width={500}
        maskClosable={false}
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message="启动后会进入右下角任务托盘"
          description="任务支持最小化、恢复查看日志，并且可以继续发起新的批量注册。"
        />
        <Form form={registerForm} layout="vertical" onFinish={handleRegister}>
          <Form.Item name="count" label="注册数量" initialValue={1} rules={[{ required: true }]}>
            <Input type="number" min={1} max={99} />
          </Form.Item>
          <Form.Item name="concurrency" label="并发数" initialValue={1} rules={[{ required: true }]}>
            <Input type="number" min={1} max={5} />
          </Form.Item>
          <Form.Item name="register_delay_seconds" label="每个注册延迟(秒)" initialValue={0}>
            <InputNumber min={0} precision={1} step={0.5} style={{ width: '100%' }} placeholder="0 = 不延迟" />
          </Form.Item>
          <Form.Item>
            <Button type="primary" htmlType="submit" block loading={registerLoading}>
              启动后台注册
            </Button>
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title="手动新增账号"
        open={addModalOpen}
        onCancel={() => { setAddModalOpen(false); addForm.resetFields(); }}
        onOk={handleAdd}
        maskClosable={false}
      >
        <Form form={addForm} layout="vertical">
          <Form.Item name="email" label="邮箱" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="password" label="密码" rules={[{ required: true }]}>
            <Input.Password />
          </Form.Item>
          <Form.Item name="token" label="Token">
            <Input />
          </Form.Item>
          <Form.Item name="cashier_url" label="试用链接">
            <Input />
          </Form.Item>
          <Form.Item name="status" label="状态" initialValue="registered">
            <Select
              options={[
                { value: 'registered', label: '已注册' },
                { value: 'trial', label: '试用中' },
                { value: 'subscribed', label: '已订阅' },
              ]}
            />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title="批量导入"
        open={importModalOpen}
        onCancel={() => { setImportModalOpen(false); setImportText(''); }}
        onOk={handleImport}
        confirmLoading={importLoading}
        maskClosable={false}
      >
        <p style={{ marginBottom: 8, fontSize: 12, color: '#7a8ba3' }}>
          每行格式: <code style={{ background: 'rgba(255,255,255,0.1)', padding: '2px 4px', borderRadius: 4 }}>email password [cashier_url]</code>
        </p>
        <Input.TextArea
          value={importText}
          onChange={(e) => setImportText(e.target.value)}
          rows={8}
          style={{ fontFamily: 'monospace' }}
        />
      </Modal>

      <Modal
        title="账号详情"
        open={detailModalOpen}
        onCancel={() => setDetailModalOpen(false)}
        onOk={handleDetailSave}
        maskClosable={false}
      >
        {currentAccount && (
          <>
            <Form form={detailForm} layout="vertical" initialValues={currentAccount}>
              <Form.Item name="status" label="状态">
                <Select
                  options={[
                    { value: 'registered', label: '已注册' },
                    { value: 'trial', label: '试用中' },
                    { value: 'subscribed', label: '已订阅' },
                    { value: 'expired', label: '已过期' },
                    { value: 'invalid', label: '已失效' },
                  ]}
                />
              </Form.Item>
              <Form.Item name="token" label="Access Token">
                <Input.TextArea rows={2} style={{ fontFamily: 'monospace' }} />
              </Form.Item>
            </Form>
            {(() => {
              const rt = getRefreshToken(currentAccount)
              if (!rt) return null
              return (
                <div style={{ marginTop: 8 }}>
                  <div style={{ marginBottom: 4, fontWeight: 500, fontSize: 13 }}>Refresh Token</div>
                  <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8, background: 'rgba(0,0,0,0.03)', border: '1px solid #e5e7eb', borderRadius: 6, padding: '6px 10px' }}>
                    <Text
                      style={{ fontFamily: 'monospace', fontSize: 11, wordBreak: 'break-all', flex: 1, userSelect: 'text' }}
                      copyable={{ text: rt, tooltips: ['复制 RT', '已复制'] }}
                    >
                      {rt}
                    </Text>
                  </div>
                </div>
              )
            })()}
          </>
        )}
      </Modal>
    </div>
  )
}
