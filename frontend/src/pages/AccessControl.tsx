import { useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Select,
  Space,
  Switch,
  Table,
  Tabs,
  Tag,
  Typography,
  message,
} from 'antd'
import {
  ApiOutlined,
  PlusOutlined,
  ReloadOutlined,
  RotateLeftOutlined,
  UserAddOutlined,
} from '@ant-design/icons'
import { apiFetch } from '@/lib/utils'
import { useAuth } from '@/components/AuthProvider'

const { Title, Paragraph, Text } = Typography

interface ProxyItem {
  id: number
  url: string
  region: string
  is_active: boolean
}

interface UserItem {
  id: number
  username: string
  role: 'admin' | 'user'
  is_active: boolean
  created_at: string
  updated_at: string
}

interface SKKeyItem {
  id: number
  user_id: number
  owner_username: string
  name: string
  description: string
  key_prefix: string
  masked_key: string
  target_url: string
  has_upstream_api_key: boolean
  proxy_id?: number | null
  proxy_url?: string
  resolved_proxy_url?: string
  token_limit: number
  prompt_tokens_used: number
  completion_tokens_used: number
  total_tokens_used: number
  remaining_tokens?: number | null
  request_count: number
  is_active: boolean
  last_used_at?: string | null
  created_at: string
  updated_at: string
}

interface UsageItem {
  id: number
  model: string
  target_url: string
  proxy_url: string
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  success: boolean
  error: string
  created_at: string
}

interface UsageResponse {
  summary: SKKeyItem
  items: UsageItem[]
}

export default function AccessControl() {
  const { user, refreshMe } = useAuth()
  const [loading, setLoading] = useState(true)
  const [proxies, setProxies] = useState<ProxyItem[]>([])
  const [users, setUsers] = useState<UserItem[]>([])
  const [keys, setKeys] = useState<SKKeyItem[]>([])
  const [issuedSecret, setIssuedSecret] = useState('')
  const [usageModalOpen, setUsageModalOpen] = useState(false)
  const [usageLoading, setUsageLoading] = useState(false)
  const [usageData, setUsageData] = useState<UsageResponse | null>(null)
  const [keyModalOpen, setKeyModalOpen] = useState(false)
  const [userModalOpen, setUserModalOpen] = useState(false)
  const [savingKey, setSavingKey] = useState(false)
  const [savingUser, setSavingUser] = useState(false)
  const [editingKey, setEditingKey] = useState<SKKeyItem | null>(null)
  const [editingUser, setEditingUser] = useState<UserItem | null>(null)
  const [keyForm] = Form.useForm()
  const [userForm] = Form.useForm()

  const isAdmin = user?.role === 'admin'
  const apiBase = typeof window === 'undefined' ? '' : window.location.origin

  const loadData = async () => {
    setLoading(true)
    try {
      const requests: Promise<any>[] = [apiFetch('/sk-keys'), apiFetch('/proxies')]
      if (isAdmin) {
        requests.push(apiFetch('/auth/users'))
      }

      const [skData, proxyData, userData] = await Promise.all(requests)
      setKeys((skData?.items || []) as SKKeyItem[])
      setProxies((proxyData || []) as ProxyItem[])
      setUsers(isAdmin ? ((userData?.items || []) as UserItem[]) : [])
      await refreshMe()
    } catch (error: any) {
      message.error(error?.message || '访问控制数据加载失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    loadData()
  }, [isAdmin])

  const activeProxyOptions = useMemo(
    () =>
      proxies
        .filter((item) => item.is_active)
        .map((item) => ({
          label: item.region ? `${item.region} | ${item.url}` : item.url,
          value: item.id,
        })),
    [proxies],
  )

  const ownerOptions = useMemo(
    () =>
      users.map((item) => ({
        label: `${item.username} (${item.role})`,
        value: item.id,
      })),
    [users],
  )

  const openCreateKeyModal = () => {
    setEditingKey(null)
    keyForm.setFieldsValue({
      name: '',
      description: '',
      owner_user_id: user?.id,
      target_url: '',
      upstream_api_key: '',
      proxy_id: undefined,
      proxy_url: '',
      token_limit: 0,
      is_active: true,
    })
    setKeyModalOpen(true)
  }

  const openEditKeyModal = (item: SKKeyItem) => {
    setEditingKey(item)
    keyForm.setFieldsValue({
      name: item.name,
      description: item.description,
      owner_user_id: item.user_id,
      target_url: item.target_url,
      upstream_api_key: '',
      proxy_id: item.proxy_id || undefined,
      proxy_url: item.proxy_id ? '' : item.proxy_url || '',
      token_limit: item.token_limit,
      is_active: item.is_active,
    })
    setKeyModalOpen(true)
  }

  const submitKeyForm = async () => {
    const values = await keyForm.validateFields()
    if (values.proxy_id && String(values.proxy_url || '').trim()) {
      message.error('代理池绑定和自定义代理地址只能选一个')
      return
    }

    setSavingKey(true)
    try {
      const body = {
        ...values,
        proxy_url: String(values.proxy_url || '').trim() || null,
      }
      const data = editingKey
        ? await apiFetch(`/sk-keys/${editingKey.id}`, {
            method: 'PATCH',
            body: JSON.stringify(body),
          })
        : await apiFetch('/sk-keys', {
            method: 'POST',
            body: JSON.stringify(body),
          })

      if (data?.secret_key) {
        setIssuedSecret(String(data.secret_key))
      }
      message.success(editingKey ? 'SK Key 已更新' : 'SK Key 已创建')
      setKeyModalOpen(false)
      await loadData()
    } catch (error: any) {
      message.error(error?.message || 'SK Key 保存失败')
    } finally {
      setSavingKey(false)
    }
  }

  const rotateKey = async (item: SKKeyItem) => {
    try {
      const data = await apiFetch(`/sk-keys/${item.id}/rotate`, {
        method: 'POST',
      })
      setIssuedSecret(String(data?.secret_key || ''))
      message.success('SK Key 已轮换')
      await loadData()
    } catch (error: any) {
      message.error(error?.message || 'SK Key 轮换失败')
    }
  }

  const deleteKey = async (item: SKKeyItem) => {
    try {
      await apiFetch(`/sk-keys/${item.id}`, { method: 'DELETE' })
      message.success('SK Key 已删除')
      await loadData()
    } catch (error: any) {
      message.error(error?.message || 'SK Key 删除失败')
    }
  }

  const showUsage = async (item: SKKeyItem) => {
    setUsageModalOpen(true)
    setUsageLoading(true)
    try {
      const data = await apiFetch(`/sk-keys/${item.id}/usage?limit=50`)
      setUsageData((data || null) as UsageResponse | null)
    } catch (error: any) {
      message.error(error?.message || '读取用量失败')
    } finally {
      setUsageLoading(false)
    }
  }

  const openCreateUserModal = () => {
    setEditingUser(null)
    userForm.setFieldsValue({
      username: '',
      password: '',
      role: 'user',
      is_active: true,
    })
    setUserModalOpen(true)
  }

  const openEditUserModal = (item: UserItem) => {
    setEditingUser(item)
    userForm.setFieldsValue({
      username: item.username,
      password: '',
      role: item.role,
      is_active: item.is_active,
    })
    setUserModalOpen(true)
  }

  const submitUserForm = async () => {
    const values = await userForm.validateFields()
    setSavingUser(true)
    try {
      if (editingUser) {
        const body: Record<string, unknown> = {
          role: values.role,
          is_active: Boolean(values.is_active),
        }
        if (String(values.password || '').trim()) {
          body.password = String(values.password)
        }
        await apiFetch(`/auth/users/${editingUser.id}`, {
          method: 'PATCH',
          body: JSON.stringify(body),
        })
      } else {
        await apiFetch('/auth/users', {
          method: 'POST',
          body: JSON.stringify({
            username: values.username,
            password: values.password,
            role: values.role,
            is_active: Boolean(values.is_active),
          }),
        })
      }
      message.success(editingUser ? '用户已更新' : '用户已创建')
      setUserModalOpen(false)
      await loadData()
    } catch (error: any) {
      message.error(error?.message || '用户保存失败')
    } finally {
      setSavingUser(false)
    }
  }

  const deleteUser = async (item: UserItem) => {
    try {
      await apiFetch(`/auth/users/${item.id}`, { method: 'DELETE' })
      message.success('用户已删除')
      await loadData()
    } catch (error: any) {
      message.error(error?.message || '用户删除失败')
    }
  }

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Card bordered={false}>
        <Space direction="vertical" size={10} style={{ width: '100%' }}>
          <Space align="center" style={{ justifyContent: 'space-between', width: '100%' }}>
            <div>
              <Title level={3} style={{ margin: 0 }}>
                访问控制
              </Title>
              <Paragraph style={{ margin: '8px 0 0', color: '#7a8ba3' }}>
                管理用户/角色、SK Key、绑定代理、Token 配额，以及对外 OpenAI 协议接口。
              </Paragraph>
            </div>
            <Button icon={<ReloadOutlined />} onClick={loadData} loading={loading}>
              刷新
            </Button>
          </Space>

          <Space size={8} wrap>
            <Tag color="blue">当前用户: {user?.username}</Tag>
            <Tag color={isAdmin ? 'gold' : 'default'}>{user?.role}</Tag>
            <Tag>对外地址: {apiBase || '-'}</Tag>
            <Tag color="purple">OpenAI: `/v1/models` / `/v1/chat/completions`</Tag>
          </Space>

          {issuedSecret ? (
            <Alert
              type="success"
              showIcon
              message="新的 SK Key 只会在当前操作后显示一次"
              description={
                <Text copyable style={{ wordBreak: 'break-all' }}>
                  {issuedSecret}
                </Text>
              }
            />
          ) : null}
        </Space>
      </Card>

      <Tabs
        items={[
          {
            key: 'keys',
            label: 'SK Keys',
            children: (
              <Space direction="vertical" size={16} style={{ width: '100%' }}>
                <Card
                  title="OpenAI 协议接入"
                  extra={<ApiOutlined />}
                >
                  <Paragraph style={{ marginBottom: 12 }}>
                    外部客户端直接调用 <Text code>{`${apiBase}/v1/chat/completions`}</Text>，
                    Header 使用 <Text code>Authorization: Bearer sk-...</Text>。
                  </Paragraph>
                  <Paragraph copyable={{ text: `curl ${apiBase}/v1/models -H "Authorization: Bearer sk-xxxx"` }}>
                    <Text code>{`curl ${apiBase}/v1/models -H "Authorization: Bearer sk-xxxx"`}</Text>
                  </Paragraph>
                  <Paragraph
                    copyable={{
                      text: `curl ${apiBase}/v1/chat/completions -H "Authorization: Bearer sk-xxxx" -H "Content-Type: application/json" -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hello"}]}'`,
                    }}
                    style={{ marginBottom: 0 }}
                  >
                    <Text code style={{ whiteSpace: 'pre-wrap' }}>
                      {`curl ${apiBase}/v1/chat/completions -H "Authorization: Bearer sk-xxxx" -H "Content-Type: application/json" -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hello"}]}'`}
                    </Text>
                  </Paragraph>
                </Card>

                <Card
                  title="SK Key 列表"
                  extra={
                    <Button type="primary" icon={<PlusOutlined />} onClick={openCreateKeyModal}>
                      新建 SK Key
                    </Button>
                  }
                >
                  <Table
                    rowKey="id"
                    loading={loading}
                    dataSource={keys}
                    scroll={{ x: 1200 }}
                    pagination={{ pageSize: 10, showSizeChanger: false }}
                    columns={[
                      {
                        title: '名称',
                        dataIndex: 'name',
                        render: (_, item: SKKeyItem) => (
                          <Space direction="vertical" size={4}>
                            <Text strong>{item.name}</Text>
                            <Text type="secondary">{item.masked_key}</Text>
                          </Space>
                        ),
                      },
                      {
                        title: '所有者',
                        dataIndex: 'owner_username',
                        render: (_, item: SKKeyItem) => (
                          <Space size={8}>
                            <Text>{item.owner_username || `#${item.user_id}`}</Text>
                            <Tag>{item.user_id}</Tag>
                          </Space>
                        ),
                      },
                      {
                        title: '目标接口',
                        dataIndex: 'target_url',
                        render: (value: string) => (
                          <Text style={{ maxWidth: 320 }} ellipsis={{ tooltip: value }}>
                            {value || '-'}
                          </Text>
                        ),
                      },
                      {
                        title: '代理',
                        dataIndex: 'resolved_proxy_url',
                        render: (value: string) =>
                          value ? (
                            <Text style={{ maxWidth: 240 }} ellipsis={{ tooltip: value }}>
                              {value}
                            </Text>
                          ) : (
                            <Tag color="default">直连</Tag>
                          ),
                      },
                      {
                        title: '用量',
                        render: (_, item: SKKeyItem) => (
                          <Space direction="vertical" size={4}>
                            <Text>{item.total_tokens_used} tokens</Text>
                            <Text type="secondary">
                              limit: {item.token_limit > 0 ? item.token_limit : '∞'} / remain:{' '}
                              {item.remaining_tokens == null ? '∞' : item.remaining_tokens}
                            </Text>
                            <Text type="secondary">req: {item.request_count}</Text>
                          </Space>
                        ),
                      },
                      {
                        title: '状态',
                        render: (_, item: SKKeyItem) => (
                          <Space direction="vertical" size={4}>
                            <Tag color={item.is_active ? 'success' : 'default'}>
                              {item.is_active ? '启用' : '停用'}
                            </Tag>
                            {item.has_upstream_api_key ? <Tag color="blue">Upstream Key</Tag> : null}
                          </Space>
                        ),
                      },
                      {
                        title: '操作',
                        fixed: 'right',
                        render: (_, item: SKKeyItem) => (
                          <Space wrap>
                            <Button size="small" onClick={() => showUsage(item)}>
                              用量
                            </Button>
                            <Button size="small" onClick={() => openEditKeyModal(item)}>
                              编辑
                            </Button>
                            <Button
                              size="small"
                              icon={<RotateLeftOutlined />}
                              onClick={() => rotateKey(item)}
                            >
                              轮换
                            </Button>
                            <Popconfirm
                              title="确认删除该 SK Key？"
                              onConfirm={() => deleteKey(item)}
                            >
                              <Button size="small" danger>
                                删除
                              </Button>
                            </Popconfirm>
                          </Space>
                        ),
                      },
                    ]}
                  />
                </Card>
              </Space>
            ),
          },
          ...(isAdmin
            ? [
                {
                  key: 'users',
                  label: '用户 / 角色',
                  children: (
                    <Card
                      title="用户管理"
                      extra={
                        <Button type="primary" icon={<UserAddOutlined />} onClick={openCreateUserModal}>
                          新建用户
                        </Button>
                      }
                    >
                      <Table
                        rowKey="id"
                        loading={loading}
                        dataSource={users}
                        pagination={{ pageSize: 10, showSizeChanger: false }}
                        columns={[
                          {
                            title: '用户名',
                            dataIndex: 'username',
                          },
                          {
                            title: '角色',
                            dataIndex: 'role',
                            render: (value: string) => <Tag color={value === 'admin' ? 'gold' : 'default'}>{value}</Tag>,
                          },
                          {
                            title: '状态',
                            dataIndex: 'is_active',
                            render: (value: boolean) => (
                              <Tag color={value ? 'success' : 'default'}>{value ? '启用' : '停用'}</Tag>
                            ),
                          },
                          {
                            title: '更新时间',
                            dataIndex: 'updated_at',
                          },
                          {
                            title: '操作',
                            render: (_, item: UserItem) => (
                              <Space wrap>
                                <Button size="small" onClick={() => openEditUserModal(item)}>
                                  编辑
                                </Button>
                                <Popconfirm title="确认删除该用户？" onConfirm={() => deleteUser(item)}>
                                  <Button size="small" danger disabled={item.id === user?.id}>
                                    删除
                                  </Button>
                                </Popconfirm>
                              </Space>
                            ),
                          },
                        ]}
                      />
                    </Card>
                  ),
                },
              ]
            : []),
        ]}
      />

      <Modal
        open={keyModalOpen}
        title={editingKey ? '编辑 SK Key' : '新建 SK Key'}
        onCancel={() => setKeyModalOpen(false)}
        onOk={submitKeyForm}
        confirmLoading={savingKey}
        width={720}
      >
        <Form form={keyForm} layout="vertical">
          <Form.Item name="name" label="名称" rules={[{ required: true, message: '请输入名称' }]}>
            <Input placeholder="production-key" />
          </Form.Item>
          <Form.Item name="description" label="描述">
            <Input.TextArea rows={3} placeholder="可选备注" />
          </Form.Item>
          {isAdmin ? (
            <Form.Item name="owner_user_id" label="所属用户" rules={[{ required: true, message: '请选择所属用户' }]}>
              <Select options={ownerOptions} showSearch optionFilterProp="label" />
            </Form.Item>
          ) : null}
          <Form.Item
            name="target_url"
            label="Chat Completions URL"
            extra="留空默认走 https://chatgpt.com/backend-api/conversation；如果要转发到 OpenAI 兼容上游，再填写完整 /chat/completions 地址。"
          >
            <Input placeholder="留空使用 ChatGPT 官方 conversation；或填写 https://your-upstream.example.com/v1/chat/completions" />
          </Form.Item>
          <Form.Item
            name="upstream_api_key"
            label="Upstream API Key / Access Token"
            extra="官方 ChatGPT 模式下，这里填 access_token；普通 OpenAI 兼容模式下，这里填上游 API Key。"
          >
            <Input.Password placeholder={editingKey ? '留空则保持原值；如需清空请显式输入空格后再删除' : '官方模式填 access_token，兼容模式填 API Key'} />
          </Form.Item>
          <Space size={12} style={{ width: '100%' }} align="start">
            <Form.Item name="proxy_id" label="绑定代理池" style={{ flex: 1 }}>
              <Select
                allowClear
                options={activeProxyOptions}
                placeholder="从代理池选择一个活跃代理"
                showSearch
                optionFilterProp="label"
              />
            </Form.Item>
            <Form.Item name="proxy_url" label="自定义代理地址" style={{ flex: 1 }}>
              <Input placeholder="http://user:pass@host:port" />
            </Form.Item>
          </Space>
          <Space size={12} style={{ width: '100%' }} align="start">
            <Form.Item name="token_limit" label="Token 限额" style={{ flex: 1 }}>
              <InputNumber min={0} style={{ width: '100%' }} placeholder="0 表示不限制" />
            </Form.Item>
            <Form.Item name="is_active" label="启用" valuePropName="checked" style={{ minWidth: 120 }}>
              <Switch checkedChildren="启用" unCheckedChildren="停用" />
            </Form.Item>
          </Space>
        </Form>
      </Modal>

      <Modal
        open={userModalOpen}
        title={editingUser ? '编辑用户' : '新建用户'}
        onCancel={() => setUserModalOpen(false)}
        onOk={submitUserForm}
        confirmLoading={savingUser}
        width={560}
      >
        <Form form={userForm} layout="vertical">
          <Form.Item name="username" label="用户名" rules={[{ required: true, message: '请输入用户名' }]}>
            <Input disabled={Boolean(editingUser)} />
          </Form.Item>
          <Form.Item
            name="password"
            label={editingUser ? '新密码（可选）' : '密码'}
            rules={editingUser ? [] : [{ required: true, message: '请输入密码' }]}
          >
            <Input.Password placeholder={editingUser ? '留空表示不修改' : '请输入密码'} />
          </Form.Item>
          <Form.Item name="role" label="角色" rules={[{ required: true, message: '请选择角色' }]}>
            <Select
              options={[
                { label: 'admin', value: 'admin' },
                { label: 'user', value: 'user' },
              ]}
            />
          </Form.Item>
          <Form.Item name="is_active" label="启用" valuePropName="checked">
            <Switch checkedChildren="启用" unCheckedChildren="停用" />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        open={usageModalOpen}
        title="SK Key 用量明细"
        onCancel={() => setUsageModalOpen(false)}
        footer={null}
        width={960}
      >
        {usageLoading ? (
          <Text>加载中...</Text>
        ) : usageData ? (
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
            <Descriptions bordered size="small" column={2}>
              <Descriptions.Item label="名称">{usageData.summary.name}</Descriptions.Item>
              <Descriptions.Item label="所有者">{usageData.summary.owner_username}</Descriptions.Item>
              <Descriptions.Item label="目标地址">{usageData.summary.target_url || '-'}</Descriptions.Item>
              <Descriptions.Item label="代理">{usageData.summary.resolved_proxy_url || '直连'}</Descriptions.Item>
              <Descriptions.Item label="请求数">{usageData.summary.request_count}</Descriptions.Item>
              <Descriptions.Item label="Token 总量">{usageData.summary.total_tokens_used}</Descriptions.Item>
              <Descriptions.Item label="Prompt">{usageData.summary.prompt_tokens_used}</Descriptions.Item>
              <Descriptions.Item label="Completion">{usageData.summary.completion_tokens_used}</Descriptions.Item>
            </Descriptions>
            <Table
              rowKey="id"
              dataSource={usageData.items}
              scroll={{ x: 900 }}
              pagination={{ pageSize: 10, showSizeChanger: false }}
              columns={[
                { title: '时间', dataIndex: 'created_at', width: 180 },
                { title: '模型', dataIndex: 'model', width: 140 },
                {
                  title: '结果',
                  dataIndex: 'success',
                  width: 100,
                  render: (value: boolean) => (
                    <Tag color={value ? 'success' : 'error'}>{value ? 'success' : 'failed'}</Tag>
                  ),
                },
                { title: 'Prompt', dataIndex: 'prompt_tokens', width: 100 },
                { title: 'Completion', dataIndex: 'completion_tokens', width: 120 },
                { title: 'Total', dataIndex: 'total_tokens', width: 100 },
                {
                  title: '代理',
                  dataIndex: 'proxy_url',
                  render: (value: string) => (
                    <Text style={{ maxWidth: 200 }} ellipsis={{ tooltip: value }}>
                      {value || '直连'}
                    </Text>
                  ),
                },
                {
                  title: '错误',
                  dataIndex: 'error',
                  render: (value: string) => (
                    <Text style={{ maxWidth: 220 }} ellipsis={{ tooltip: value }}>
                      {value || '-'}
                    </Text>
                  ),
                },
              ]}
            />
          </Space>
        ) : (
          <Alert type="info" showIcon message="暂无用量数据" />
        )}
      </Modal>
    </Space>
  )
}
