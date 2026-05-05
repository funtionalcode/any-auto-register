import { useEffect, useState } from 'react'
import {
  Card,
  Table,
  Button,
  Tag,
  Space,
  message,
  Popconfirm,
  Modal,
  Descriptions,
  Statistic,
  Row,
  Col,
  Spin,
} from 'antd'
import {
  MailOutlined,
  DeleteOutlined,
  ReloadOutlined,
  EyeOutlined,
  InboxOutlined,
} from '@ant-design/icons'
import { apiFetch } from '@/lib/utils'

interface TempMailbox {
  id: number
  email: string
  provider: string
  account_id: string
  extra_json: string
  created_at: string
}

interface MailMessage {
  message_id: string
  subject: string
  sender: string
  recipient: string
  preview: string
  content: string
  html_content: string
  created_at: string
}

interface MailboxStats {
  total: number
  by_provider: Record<string, number>
}

export default function Mailbox() {
  const [mailboxes, setMailboxes] = useState<TempMailbox[]>([])
  const [stats, setStats] = useState<MailboxStats | null>(null)
  const [loading, setLoading] = useState(false)
  const [selectedRowKeys, setSelectedRowKeys] = useState<number[]>([])
  const [messagesModalOpen, setMessagesModalOpen] = useState(false)
  const [currentMailbox, setCurrentMailbox] = useState<TempMailbox | null>(null)
  const [messages, setMessages] = useState<MailMessage[]>([])
  const [messagesLoading, setMessagesLoading] = useState(false)
  const [detailModalOpen, setDetailModalOpen] = useState(false)
  const [detailContent, setDetailContent] = useState<MailMessage | null>(null)

  const loadData = async () => {
    setLoading(true)
    try {
      const [mailboxData, statsData] = await Promise.all([
        apiFetch('/mailbox/inboxes'),
        apiFetch('/mailbox/stats'),
      ])
      setMailboxes(Array.isArray(mailboxData) ? mailboxData : [])
      setStats(statsData && typeof statsData === 'object' && !Array.isArray(statsData) ? statsData : null)
    } catch (e: any) {
      message.error(`加载失败: ${e.message}`)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    loadData()
  }, [])

  const batchDelete = async () => {
    if (selectedRowKeys.length === 0) return
    try {
      const result = await apiFetch('/mailbox/inboxes/batch-delete', {
        method: 'POST',
        body: JSON.stringify({ ids: selectedRowKeys }),
      })
      message.success(`已删除 ${result.deleted} 条记录`)
      setSelectedRowKeys([])
      loadData()
    } catch (e: any) {
      message.error(`删除失败: ${e.message}`)
    }
  }

  const viewMessages = async (mailbox: TempMailbox) => {
    setCurrentMailbox(mailbox)
    setMessagesModalOpen(true)
    setMessagesLoading(true)
    setMessages([])
    try {
      const data = await apiFetch(`/mailbox/inboxes/${mailbox.id}/messages`, {
        method: 'POST',
        body: JSON.stringify({}),
      })
      setMessages(Array.isArray(data?.items) ? data.items : [])
    } catch (e: any) {
      message.error(`获取邮件失败: ${e.message}`)
    } finally {
      setMessagesLoading(false)
    }
  }

  const providerLabel = (provider: string) => {
    const labels: Record<string, string> = {
      moemail: 'MoeMail',
      tempmail_lol: 'TempMail.lol',
      skymail: 'SkyMail',
      cloudmail: 'CloudMail',
      duckmail: 'DuckMail',
      laoudo: 'Laoudo',
      luckmail: 'LuckMail',
      cfworker: 'CF Worker',
      freemail: 'Freemail',
      maliapi: 'MaliAPI',
      gptmail: 'GPTMail',
      opentrashmail: 'OpenTrashMail',
    }
    return labels[provider] || provider
  }

  const columns = [
    {
      title: '邮箱地址',
      dataIndex: 'email',
      key: 'email',
      render: (text: string) => (
        <span style={{ fontFamily: 'monospace', fontSize: 13 }}>{text}</span>
      ),
    },
    {
      title: '邮箱服务',
      dataIndex: 'provider',
      key: 'provider',
      render: (text: string) => <Tag color="blue">{providerLabel(text)}</Tag>,
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      key: 'created_at',
      render: (text: string) => {
        if (!text) return '-'
        try {
          const d = new Date(text)
          return d.toLocaleString('zh-CN')
        } catch {
          return text
        }
      },
    },
    {
      title: '操作',
      key: 'action',
      render: (_: unknown, record: TempMailbox) => (
        <Space>
          <Button
            type="link"
            size="small"
            icon={<EyeOutlined />}
            onClick={() => viewMessages(record)}
          >
            查看邮件
          </Button>
        </Space>
      ),
    },
  ]

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div>
          <h1 style={{ fontSize: 24, fontWeight: 'bold', margin: 0 }}>邮箱服务</h1>
          <p style={{ color: '#7a8ba3', marginTop: 4 }}>管理临时邮箱、查看邮件</p>
        </div>
        <Button icon={<ReloadOutlined spin={loading} />} onClick={loadData} loading={loading}>
          刷新
        </Button>
      </div>

      {/* 统计卡片 */}
      <Row gutter={16}>
        <Col span={8}>
          <Card>
            <Statistic
              title="临时邮箱总数"
              value={stats?.total || 0}
              prefix={<InboxOutlined />}
            />
          </Card>
        </Col>
        {stats?.by_provider && Object.entries(stats.by_provider).map(([provider, count]) => (
          <Col span={8} key={provider} style={{ marginTop: 16 }}>
            <Card>
              <Statistic
                title={providerLabel(provider)}
                value={count}
                prefix={<MailOutlined />}
              />
            </Card>
          </Col>
        )).slice(0, 2)}
      </Row>

      {/* 邮箱列表 */}
      <Card title={`临时邮箱列表 (${mailboxes.length})`}>
        <div style={{ marginBottom: 12, display: 'flex', justifyContent: 'space-between' }}>
          <div style={{ color: '#7a8ba3' }}>
            已选中 {selectedRowKeys.length} 条
          </div>
          <Popconfirm
            title={`确认删除选中的 ${selectedRowKeys.length} 条记录？`}
            onConfirm={batchDelete}
            okText="删除"
            cancelText="取消"
            okButtonProps={{ danger: true }}
            disabled={selectedRowKeys.length === 0}
          >
            <Button danger icon={<DeleteOutlined />} disabled={selectedRowKeys.length === 0}>
              批量删除
            </Button>
          </Popconfirm>
        </div>
        <Table
          rowKey="id"
          columns={columns}
          dataSource={mailboxes}
          loading={loading}
          rowSelection={{
            selectedRowKeys,
            onChange: (keys) => setSelectedRowKeys(keys as number[]),
          }}
          pagination={{ pageSize: 20, showSizeChanger: true, showTotal: (total) => `共 ${total} 条` }}
        />
      </Card>

      {/* 邮件列表弹窗 */}
      <Modal
        title={
          <Space>
            <MailOutlined />
            <span>{currentMailbox?.email} 的收件箱</span>
          </Space>
        }
        open={messagesModalOpen}
        onCancel={() => setMessagesModalOpen(false)}
        footer={null}
        width={800}
      >
        <Spin spinning={messagesLoading}>
          {messages.length === 0 && !messagesLoading ? (
            <div style={{ textAlign: 'center', padding: 40, color: '#7a8ba3' }}>
              <InboxOutlined style={{ fontSize: 40, marginBottom: 8 }} />
              <div>暂无邮件</div>
            </div>
          ) : (
            <Table
              rowKey="message_id"
              dataSource={messages}
              pagination={false}
              size="small"
              columns={[
                {
                  title: '主题',
                  dataIndex: 'subject',
                  key: 'subject',
                  ellipsis: true,
                  render: (text: string) => text || '(无主题)',
                },
                {
                  title: '发件人',
                  dataIndex: 'sender',
                  key: 'sender',
                  width: 200,
                  ellipsis: true,
                  render: (text: string) => text || '-',
                },
                {
                  title: '时间',
                  dataIndex: 'created_at',
                  key: 'created_at',
                  width: 180,
                  render: (text: string) => {
                    if (!text) return '-'
                    try {
                      return new Date(text).toLocaleString('zh-CN')
                    } catch {
                      return text
                    }
                  },
                },
                {
                  title: '操作',
                  key: 'action',
                  width: 80,
                  render: (_: unknown, record: MailMessage) => (
                    <Button type="link" size="small" onClick={() => {
                      setDetailContent(record)
                      setDetailModalOpen(true)
                    }}>
                      详情
                    </Button>
                  ),
                },
              ]}
            />
          )}
        </Spin>
      </Modal>

      {/* 邮件详情弹窗 */}
      <Modal
        title={detailContent?.subject || '(无主题)'}
        open={detailModalOpen}
        onCancel={() => setDetailModalOpen(false)}
        footer={null}
        width={700}
      >
        {detailContent && (
          <Descriptions column={1} bordered size="small">
            <Descriptions.Item label="发件人">{detailContent.sender || '-'}</Descriptions.Item>
            <Descriptions.Item label="收件人">{detailContent.recipient || '-'}</Descriptions.Item>
            <Descriptions.Item label="时间">
              {detailContent.created_at
                ? (() => {
                    try { return new Date(detailContent.created_at).toLocaleString('zh-CN') }
                    catch { return detailContent.created_at }
                  })()
                : '-'}
            </Descriptions.Item>
            <Descriptions.Item label="内容">
              {detailContent.html_content ? (
                <div
                  dangerouslySetInnerHTML={{ __html: detailContent.html_content }}
                  style={{ maxHeight: 400, overflow: 'auto' }}
                />
              ) : (
                <pre style={{ whiteSpace: 'pre-wrap', maxHeight: 400, overflow: 'auto' }}>
                  {detailContent.content || detailContent.preview || '(无内容)'}
                </pre>
              )}
            </Descriptions.Item>
          </Descriptions>
        )}
      </Modal>
    </div>
  )
}
