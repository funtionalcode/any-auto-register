import { useEffect, useRef, useState } from 'react'
import { Card, Table, Button, Input, Tag, Space, Popconfirm, message, Modal, Typography } from 'antd'
import {
  PlusOutlined,
  DeleteOutlined,
  ReloadOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  SwapRightOutlined,
  SwapLeftOutlined,
  EditOutlined,
  ApiOutlined,
} from '@ant-design/icons'
import { apiFetch } from '@/lib/utils'

interface ProxyItem {
  id: number
  url: string
  region: string
  success_count: number
  fail_count: number
  is_active: boolean
  last_checked?: string | null
}

interface ProxyTestResult {
  ip?: string
  latency_ms?: number
  country_code?: string
  country?: string
  region_name?: string
  city?: string
  region_label?: string
  normalized_url?: string
  proxy?: ProxyItem
}

interface ProxyTestTaskSnapshot {
  id: string
  status: 'pending' | 'running' | 'done' | 'failed' | string
  message?: string
  error_message?: string
  proxy_id?: number | null
  current_region?: string
  result?: ProxyTestResult
  created_at: number
  updated_at: number
  finished_at?: number | null
}

interface ActiveProxyTestTask extends ProxyTestTaskSnapshot {
  scope: number | 'draft'
}

function formatDateTime(value?: string | null) {
  if (!value) return '-'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN')
}

export default function Proxies() {
  const proxyTestPollTimerRef = useRef<number | null>(null)
  const [proxies, setProxies] = useState<ProxyItem[]>([])
  const [newProxy, setNewProxy] = useState('')
  const [region, setRegion] = useState('')
  const [checking, setChecking] = useState(false)
  const [loading, setLoading] = useState(false)
  const [testing, setTesting] = useState<number | 'draft' | null>(null)
  const [testTask, setTestTask] = useState<ActiveProxyTestTask | null>(null)
  const [editingProxy, setEditingProxy] = useState<ProxyItem | null>(null)
  const [editingRegion, setEditingRegion] = useState('')
  const [testResult, setTestResult] = useState<{
    open: boolean
    title: string
    proxyId?: number
    currentRegion?: string
    detectedRegion?: string
    data?: ProxyTestResult
  }>({
    open: false,
    title: '',
  })

  const load = async () => {
    setLoading(true)
    try {
      const data = await apiFetch('/proxies') as ProxyItem[]
      setProxies(data)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [])

  useEffect(() => {
    return () => {
      if (proxyTestPollTimerRef.current !== null) {
        window.clearInterval(proxyTestPollTimerRef.current)
      }
    }
  }, [])

  const add = async () => {
    if (!newProxy.trim()) return
    const lines = newProxy.trim().split('\n').map((line) => line.trim()).filter(Boolean)
    try {
      if (lines.length > 1) {
        await apiFetch('/proxies/bulk', {
          method: 'POST',
          body: JSON.stringify({ proxies: lines, region }),
        })
      } else {
        await apiFetch('/proxies', {
          method: 'POST',
          body: JSON.stringify({ url: lines[0], region }),
        })
      }
      message.success('添加成功')
      setNewProxy('')
      setRegion('')
      await load()
    } catch (e: any) {
      message.error(`添加失败: ${e.message}`)
    }
  }

  const updateRegion = async (id: number, nextRegion: string, options?: { silent?: boolean }) => {
    await apiFetch(`/proxies/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({ region: nextRegion }),
    })
    if (!options?.silent) {
      message.success('地区已更新')
    }
    await load()
  }

  const del = async (id: number) => {
    await apiFetch(`/proxies/${id}`, { method: 'DELETE' })
    message.success('删除成功')
    await load()
  }

  const toggle = async (id: number) => {
    await apiFetch(`/proxies/${id}/toggle`, { method: 'PATCH' })
    await load()
  }

  const check = async () => {
    setChecking(true)
    try {
      await apiFetch('/proxies/check', { method: 'POST' })
      setTimeout(() => {
        void load()
        setChecking(false)
      }, 3000)
    } catch (e: any) {
      setChecking(false)
      message.error(`检测失败: ${e.message}`)
    }
  }

  const stopProxyTestPolling = () => {
    if (proxyTestPollTimerRef.current !== null) {
      window.clearInterval(proxyTestPollTimerRef.current)
      proxyTestPollTimerRef.current = null
    }
  }

  const finalizeProxyTestTask = async (snapshot: ActiveProxyTestTask) => {
    stopProxyTestPolling()
    setTesting(null)
    setTestTask(null)

    if (snapshot.status === 'failed') {
      message.error(snapshot.error_message || '代理测试失败')
      return
    }

    const data = snapshot.result || {}
    if (snapshot.scope === 'draft' && !region && data.region_label) {
      setRegion(String(data.region_label))
    }
    setTestResult({
      open: true,
      title: '代理测试结果',
      proxyId: snapshot.proxy_id ? Number(snapshot.proxy_id) : undefined,
      currentRegion: snapshot.current_region || undefined,
      detectedRegion: String(data.region_label || ''),
      data,
    })
    message.success('代理测试成功')
    await load()
  }

  const pollProxyTestTask = async (taskId: string, scope: number | 'draft') => {
    try {
      const snapshot = await apiFetch(`/proxies/test/tasks/${taskId}`) as ProxyTestTaskSnapshot
      const nextTask: ActiveProxyTestTask = { ...snapshot, scope }
      setTestTask(nextTask)

      if (snapshot.status === 'done' || snapshot.status === 'failed') {
        await finalizeProxyTestTask(nextTask)
      }
      return snapshot
    } catch (e: any) {
      stopProxyTestPolling()
      setTesting(null)
      setTestTask(null)
      message.error(`获取代理测试任务状态失败: ${e?.message || e || '未知错误'}`)
      return null
    }
  }

  const runDraftTest = async () => {
    const lines = newProxy.trim().split('\n').map((line) => line.trim()).filter(Boolean)
    if (lines.length !== 1) {
      message.error('测试输入代理时请只保留一条代理地址')
      return
    }
    if (testing !== null) {
      message.info('已有代理测试任务正在执行')
      return
    }
    setTesting('draft')
    try {
      const task = await apiFetch('/proxies/test/async', {
        method: 'POST',
        body: JSON.stringify({ url: lines[0] }),
      }) as ProxyTestTaskSnapshot
      setTestTask({ ...task, scope: 'draft' })
      message.success('代理测试已转入后台执行')

      stopProxyTestPolling()
      const snapshot = await pollProxyTestTask(task.id, 'draft')
      if (snapshot && snapshot.status !== 'done' && snapshot.status !== 'failed') {
        proxyTestPollTimerRef.current = window.setInterval(() => {
          void pollProxyTestTask(task.id, 'draft')
        }, 1500)
      }
    } catch (e: any) {
      message.error(`测试失败: ${e.message}`)
      setTesting(null)
      setTestTask(null)
    } finally {
      if (!proxyTestPollTimerRef.current) {
        setTesting(null)
      }
    }
  }

  const runSavedProxyTest = async (record: ProxyItem) => {
    if (testing !== null) {
      message.info('已有代理测试任务正在执行')
      return
    }
    setTesting(record.id)
    try {
      const task = await apiFetch(`/proxies/${record.id}/test/async`, {
        method: 'POST',
        body: JSON.stringify({ save_region: false }),
      }) as ProxyTestTaskSnapshot
      setTestTask({ ...task, scope: record.id })
      message.success('代理测试已转入后台执行')

      stopProxyTestPolling()
      const snapshot = await pollProxyTestTask(task.id, record.id)
      if (snapshot && snapshot.status !== 'done' && snapshot.status !== 'failed') {
        proxyTestPollTimerRef.current = window.setInterval(() => {
          void pollProxyTestTask(task.id, record.id)
        }, 1500)
      }
    } catch (e: any) {
      message.error(`测试失败: ${e.message}`)
      setTesting(null)
      setTestTask(null)
    } finally {
      if (!proxyTestPollTimerRef.current) {
        setTesting(null)
      }
    }
  }

  const columns = [
    {
      title: '代理地址',
      dataIndex: 'url',
      key: 'url',
      render: (text: string) => <span style={{ fontFamily: 'monospace', fontSize: 12 }}>{text}</span>,
    },
    {
      title: '地区',
      dataIndex: 'region',
      key: 'region',
      width: 180,
      render: (text: string) => text ? <Tag color="blue">{text}</Tag> : <Typography.Text type="secondary">未设置</Typography.Text>,
    },
    {
      title: '成功/失败',
      key: 'stats',
      width: 120,
      render: (_: unknown, record: ProxyItem) => (
        <Space>
          <Tag color="success">{record.success_count}</Tag>
          <span>/</span>
          <Tag color="error">{record.fail_count}</Tag>
        </Space>
      ),
    },
    {
      title: '状态',
      dataIndex: 'is_active',
      key: 'is_active',
      width: 100,
      render: (active: boolean) => (
        <Tag color={active ? 'success' : 'error'} icon={active ? <CheckCircleOutlined /> : <CloseCircleOutlined />}>
          {active ? '活跃' : '禁用'}
        </Tag>
      ),
    },
    {
      title: '最近检测',
      dataIndex: 'last_checked',
      key: 'last_checked',
      width: 180,
      render: (value: string | null) => formatDateTime(value),
    },
    {
      title: '操作',
      key: 'action',
      width: 180,
      render: (_: unknown, record: ProxyItem) => (
        <Space>
          <Button
            type="text"
            size="small"
            icon={<ApiOutlined />}
            loading={testing === record.id}
            disabled={testing !== null && testing !== record.id}
            onClick={() => runSavedProxyTest(record)}
          />
          <Button
            type="text"
            size="small"
            icon={<EditOutlined />}
            onClick={() => {
              setEditingProxy(record)
              setEditingRegion(record.region || '')
            }}
          />
          <Button
            type="text"
            size="small"
            icon={record.is_active ? <SwapLeftOutlined /> : <SwapRightOutlined />}
            onClick={() => toggle(record.id)}
          />
          <Popconfirm title="确认删除？" onConfirm={() => del(record.id)}>
            <Button type="text" size="small" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    },
  ]

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <Modal
        open={Boolean(editingProxy)}
        title="修改地区"
        onCancel={() => setEditingProxy(null)}
        onOk={async () => {
          if (!editingProxy) return
          await updateRegion(editingProxy.id, editingRegion)
          setEditingProxy(null)
        }}
      >
        <Input
          value={editingRegion}
          onChange={(event) => setEditingRegion(event.target.value)}
          placeholder="例如 US / California 或 SG"
        />
      </Modal>

      <Modal
        open={testResult.open}
        title={testResult.title}
        onCancel={() => setTestResult({ open: false, title: '' })}
        onOk={() => setTestResult({ open: false, title: '' })}
        okText="关闭"
        cancelButtonProps={{ style: { display: 'none' } }}
        footer={[
          testResult.proxyId && testResult.detectedRegion ? (
            <Button
              key="apply-region"
              onClick={async () => {
                await updateRegion(testResult.proxyId as number, testResult.detectedRegion as string)
                setTestResult({ open: false, title: '' })
              }}
            >
              应用检测地区
            </Button>
          ) : null,
          <Button key="close" type="primary" onClick={() => setTestResult({ open: false, title: '' })}>
            关闭
          </Button>,
        ].filter(Boolean)}
      >
        <Space direction="vertical" style={{ width: '100%' }}>
          <div>出口 IP：{testResult.data?.ip || '-'}</div>
          <div>检测地区：{testResult.data?.region_label || '-'}</div>
          <div>国家：{testResult.data?.country || '-'}</div>
          <div>省州：{testResult.data?.region_name || '-'}</div>
          <div>城市：{testResult.data?.city || '-'}</div>
          <div>延迟：{typeof testResult.data?.latency_ms === 'number' ? `${testResult.data.latency_ms} ms` : '-'}</div>
          <div>规范化代理：{testResult.data?.normalized_url || '-'}</div>
          {testResult.currentRegion !== undefined ? <div>当前已保存地区：{testResult.currentRegion || '-'}</div> : null}
        </Space>
      </Modal>

      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div>
          <h1 style={{ fontSize: 24, fontWeight: 'bold', margin: 0 }}>代理管理</h1>
          <p style={{ color: '#7a8ba3', marginTop: 4 }}>共 {proxies.length} 个代理</p>
        </div>
        <Button icon={<ReloadOutlined spin={checking} />} onClick={check} loading={checking}>
          检测全部
        </Button>
      </div>

      {testTask ? (
        <Card size="small">
          <Typography.Text strong>
            {testTask.status === 'failed'
              ? '代理测试后台任务失败'
              : testTask.status === 'done'
                ? '代理测试后台任务已完成'
                : '代理测试后台任务运行中'}
          </Typography.Text>
          <div style={{ color: '#7a8ba3', marginTop: 6 }}>
            {[testTask.message || '', `对象 ${testTask.scope === 'draft' ? '草稿代理' : `#${testTask.scope}`}`].filter(Boolean).join(' · ')}
          </div>
        </Card>
      ) : null}

      <Card title="添加代理（每行一个）">
        <Space direction="vertical" style={{ width: '100%' }}>
          <Input.TextArea
            value={newProxy}
            onChange={(e) => setNewProxy(e.target.value)}
            placeholder="http://user:pass@host:port"
            rows={3}
            style={{ fontFamily: 'monospace' }}
          />
          <Space wrap>
            <Input
              value={region}
              onChange={(e) => setRegion(e.target.value)}
              placeholder="地区标签 (如 US, SG)"
              style={{ width: 220 }}
            />
            <Button icon={<ApiOutlined />} onClick={runDraftTest} loading={testing === 'draft'}>
              测试
            </Button>
            <Button type="primary" icon={<PlusOutlined />} onClick={add}>
              添加
            </Button>
          </Space>
        </Space>
      </Card>

      <Card>
        <Table
          rowKey="id"
          columns={columns}
          dataSource={proxies}
          loading={loading}
          pagination={false}
        />
      </Card>
    </div>
  )
}
