import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  Alert,
  Button,
  Card,
  Input,
  Select,
  Space,
  Spin,
  Switch,
  Tag,
  Typography,
  message,
} from 'antd'
import {
  ArrowLeftOutlined,
  ClearOutlined,
  ReloadOutlined,
  SendOutlined,
} from '@ant-design/icons'
import { apiFetch } from '@/lib/utils'

const { Text, Paragraph, Title } = Typography
const OFFICIAL_DEFAULT_TARGET_URL = 'https://chatgpt.com/backend-api/conversation'

type ConversationMode = 'official' | 'custom_api'

interface ProxyItem {
  id: number
  url: string
  region: string
  is_active: boolean
}

interface ChatMessageItem {
  id: string
  role: 'user' | 'assistant'
  content: string
  status?: 'streaming' | 'error'
}

function parseExtra(raw: string | undefined) {
  if (!raw) return {}
  try {
    const parsed = JSON.parse(raw)
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    return {}
  }
}

function buildMessageHistory(messages: ChatMessageItem[]) {
  return messages
    .filter((item) => item.role !== 'assistant' || item.status !== 'error')
    .filter((item) => item.content.trim())
    .map((item) => ({ role: item.role, content: item.content }))
}

function parseSseBlock(block: string): { event: string; data: any } | null {
  const normalized = String(block || '').replace(/\r/g, '').trim()
  if (!normalized) return null

  let event = 'message'
  const dataLines: string[] = []
  for (const line of normalized.split('\n')) {
    if (line.startsWith('event:')) {
      event = line.slice(6).trim() || 'message'
      continue
    }
    if (line.startsWith('data:')) {
      dataLines.push(line.slice(5).trim())
    }
  }

  if (dataLines.length === 0) return null
  try {
    return { event, data: JSON.parse(dataLines.join('\n')) }
  } catch {
    return null
  }
}

function buildChatErrorDetail(data: any, fallback: string) {
  const sections: string[] = []
  const detailText = String(data?.response_text || '').trim()
  const chain = String(data?.chain || '').trim()
  const targetUrl = String(data?.target_url || '').trim()
  const statusCode = data?.response_status_code
  const headers =
    data?.response_headers && typeof data.response_headers === 'object'
      ? JSON.stringify(data.response_headers, null, 2)
      : ''
  const payloads = Array.isArray(data?.debug_payloads)
    ? data.debug_payloads.map((item: any) => String(item || '').trim()).filter(Boolean)
    : []
  const rawLines = Array.isArray(data?.debug_raw_lines)
    ? data.debug_raw_lines.map((item: any) => String(item || '').trim()).filter(Boolean)
    : []

  sections.push(detailText || fallback)
  if (chain) {
    sections.push(
      `chain: ${chain}${data?.shared_test_flow ? ' (shares preflight with test flow)' : ''}`,
    )
  }
  if (targetUrl) {
    sections.push(`target: ${targetUrl}`)
  }
  if (statusCode) {
    sections.push(`status: ${statusCode}`)
  }
  if (headers) {
    sections.push(`headers:\n${headers}`)
  }
  if (payloads.length > 0) {
    sections.push(`payloads:\n${payloads.join('\n')}`)
  }
  if (rawLines.length > 0) {
    sections.push(`raw lines:\n${rawLines.join('\n')}`)
  }

  return sections.join('\n\n') || fallback
}

function shouldResetOfficialConversation(data: any) {
  const combined = [
    String(data?.message || ''),
    String(data?.response_text || ''),
  ]
    .join('\n')
    .toLowerCase()
  return (
    combined.includes('history_disabled_conversation_not_found') ||
    combined.includes('conversation not found')
  )
}

function proxyLabel(item: ProxyItem) {
  return item.region ? `${item.region} | ${item.url}` : item.url
}

export default function ChatGPTConversation() {
  const { accountId } = useParams<{ accountId: string }>()
  const navigate = useNavigate()
  const scrollRef = useRef<HTMLDivElement | null>(null)

  const [loading, setLoading] = useState(true)
  const [sending, setSending] = useState(false)
  const [account, setAccount] = useState<any>(null)
  const [proxies, setProxies] = useState<ProxyItem[]>([])
  const [messages, setMessages] = useState<ChatMessageItem[]>([])
  const [prompt, setPrompt] = useState('')
  const [conversationId, setConversationId] = useState('')
  const [parentMessageId, setParentMessageId] = useState('')
  const [selectedProxy, setSelectedProxy] = useState('')
  const [customProxy, setCustomProxy] = useState('')
  const [mode, setMode] = useState<ConversationMode>('official')
  const [streamEnabled, setStreamEnabled] = useState(true)
  const [officialTargetUrl, setOfficialTargetUrl] = useState('')
  const [officialModel, setOfficialModel] = useState('auto')
  const [customTargetUrl, setCustomTargetUrl] = useState('')
  const [customApiKey, setCustomApiKey] = useState('')
  const [customModel, setCustomModel] = useState('gpt-4o-mini')
  const [lastUsedProxy, setLastUsedProxy] = useState('')
  const [lastTargetUrl, setLastTargetUrl] = useState('')
  const [lastModel, setLastModel] = useState('')

  const effectiveProxy = customProxy.trim() || selectedProxy
  const currentModel = mode === 'official' ? officialModel : customModel

  const proxyOptions = useMemo(
    () => [
      { label: '自动选择', value: '' },
      ...proxies
        .filter((item) => item.is_active)
        .map((item) => ({
          label: proxyLabel(item),
          value: item.url,
        })),
    ],
    [proxies],
  )

  const load = async () => {
    if (!accountId) return
    setLoading(true)
    try {
      const [accountData, proxyData] = await Promise.all([
        apiFetch(`/accounts/${accountId}`),
        apiFetch('/proxies'),
      ])
      if (accountData.platform !== 'chatgpt') {
        throw new Error('该账号不是 ChatGPT 账号')
      }
      setAccount(accountData)
      setProxies(proxyData || [])

      const extra = parseExtra(accountData.extra_json)
      const preferredProxy = String(extra.test_proxy || '').trim()
      if (preferredProxy) {
        setCustomProxy(preferredProxy)
      }
    } catch (e: any) {
      message.error(e?.message || '加载失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [accountId])

  useEffect(() => {
    const container = scrollRef.current
    if (!container) return
    container.scrollTop = container.scrollHeight
  }, [messages])

  const handleResetConversation = () => {
    setMessages([])
    setConversationId('')
    setParentMessageId('')
  }

  const updateAssistantMessage = (messageId: string, updater: (current: ChatMessageItem) => ChatMessageItem) => {
    setMessages((current) =>
      current.map((item) => (item.id === messageId ? updater(item) : item)),
    )
  }

  const handleSend = async () => {
    const normalizedPrompt = prompt.trim()
    if (!accountId || !normalizedPrompt || sending) return
    if (mode === 'custom_api' && !customTargetUrl.trim()) {
      message.warning('自定义模式需要填写 URL')
      return
    }

    const userMessage: ChatMessageItem = {
      id: `user-${Date.now()}`,
      role: 'user',
      content: normalizedPrompt,
    }
    const assistantId = `assistant-${Date.now()}`
    const assistantMessage: ChatMessageItem = {
      id: assistantId,
      role: 'assistant',
      content: '',
      status: 'streaming',
    }

    const historyBeforeSend = buildMessageHistory(messages)
    setMessages((current) => [...current, userMessage, assistantMessage])
    setPrompt('')
    setSending(true)

    try {
      const response = await fetch(`/api/accounts/${accountId}/chatgpt/chat-stream`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Accept: 'text/event-stream',
        },
        body: JSON.stringify({
          prompt: normalizedPrompt,
          mode,
          stream: streamEnabled,
          proxy: effectiveProxy,
          conversation_id: mode === 'official' ? conversationId : '',
          parent_message_id: mode === 'official' ? parentMessageId : '',
          target_url: mode === 'official' ? officialTargetUrl.trim() : customTargetUrl.trim(),
          api_key: mode === 'custom_api' ? customApiKey.trim() : '',
          model: currentModel,
          messages: [...historyBeforeSend, { role: 'user', content: normalizedPrompt }],
        }),
      })

      if (!response.ok || !response.body) {
        const detail = await response.text()
        throw new Error(detail || '发起对话失败')
      }

      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      let finished = false

      while (!finished) {
        const { value, done } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })
        let splitIndex = buffer.indexOf('\n\n')
        while (splitIndex >= 0) {
          const rawBlock = buffer.slice(0, splitIndex)
          buffer = buffer.slice(splitIndex + 2)
          splitIndex = buffer.indexOf('\n\n')

          const parsed = parseSseBlock(rawBlock)
          if (!parsed) continue

          const { event, data } = parsed
          if (data?.used_proxy) {
            setLastUsedProxy(String(data.used_proxy))
          }
          if (data?.target_url) {
            setLastTargetUrl(String(data.target_url))
          } else if (mode === 'official') {
            setLastTargetUrl(officialTargetUrl.trim() || OFFICIAL_DEFAULT_TARGET_URL)
          }
          if (data?.model) {
            setLastModel(String(data.model))
          }

          if (event === 'delta') {
            const delta = String(data?.delta || '')
            if (!delta) continue
            updateAssistantMessage(assistantId, (current) => ({
              ...current,
              content: `${current.content}${delta}`,
              status: 'streaming',
            }))
            continue
          }

          if (event === 'done') {
            const fullText = String(data?.response_text || '')
            updateAssistantMessage(assistantId, (current) => ({
              ...current,
              content: fullText || current.content,
              status: undefined,
            }))
            if (mode === 'official') {
              setConversationId(String(data?.conversation_id || ''))
              setParentMessageId(String(data?.response_message_id || ''))
            }
            finished = true
            continue
          }

          if (event === 'error') {
            const errorText = String(data?.message || '发送失败')
            const shouldResetConversation = mode === 'official' && shouldResetOfficialConversation(data)
            const detailText = buildChatErrorDetail(
              data,
              shouldResetConversation
                ? `${errorText}\n\n已自动清空当前会话上下文，请重新发送。`
                : errorText,
            )
            if (shouldResetConversation) {
              setConversationId('')
              setParentMessageId('')
            }
            updateAssistantMessage(assistantId, (current) => ({
              ...current,
              content: detailText,
              status: 'error',
            }))
            message.error(errorText)
            finished = true
          }
        }
      }

      const trailingEvent = parseSseBlock(buffer)
      if (trailingEvent) {
        const data = trailingEvent.data || {}
        if (data?.used_proxy) {
          setLastUsedProxy(String(data.used_proxy))
        }
        if (data?.target_url) {
          setLastTargetUrl(String(data.target_url))
        }
        if (data?.model) {
          setLastModel(String(data.model))
        }
        if (trailingEvent.event === 'done') {
          updateAssistantMessage(assistantId, (current) => ({
            ...current,
            content: String(data?.response_text || current.content),
            status: undefined,
          }))
          if (mode === 'official') {
            setConversationId(String(data?.conversation_id || ''))
            setParentMessageId(String(data?.response_message_id || ''))
          }
        }
        if (trailingEvent.event === 'error') {
          const errorText = String(data?.message || '发送失败')
          const shouldResetConversation = mode === 'official' && shouldResetOfficialConversation(data)
          const detailText = buildChatErrorDetail(
            data,
            shouldResetConversation
              ? `${errorText}\n\n已自动清空当前会话上下文，请重新发送。`
              : errorText,
          )
          if (shouldResetConversation) {
            setConversationId('')
            setParentMessageId('')
          }
          updateAssistantMessage(assistantId, (current) => ({
            ...current,
            content: detailText,
            status: 'error',
          }))
          message.error(errorText)
        }
      }
    } catch (e: any) {
      updateAssistantMessage(assistantId, (current) => ({
        ...current,
        content: e?.message || '发送失败',
        status: 'error',
      }))
      message.error(e?.message || '发送失败')
    } finally {
      setSending(false)
    }
  }

  if (loading) {
    return (
      <div style={{ minHeight: 320, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <Spin />
      </div>
    )
  }

  if (!account) {
    return (
      <Alert
        type="error"
        showIcon
        message="账号加载失败"
        description="请返回账号列表后重试。"
      />
    )
  }

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Space align="center" style={{ justifyContent: 'space-between', width: '100%' }}>
        <Space align="center">
          <Button icon={<ArrowLeftOutlined />} onClick={() => navigate('/accounts/chatgpt')}>
            返回账号列表
          </Button>
          <Title level={4} style={{ margin: 0 }}>
            ChatGPT 对话测试
          </Title>
        </Space>
        <Space>
          <Button icon={<ClearOutlined />} onClick={handleResetConversation} disabled={sending}>
            新会话
          </Button>
          <Button icon={<ReloadOutlined />} onClick={load} disabled={sending}>
            刷新
          </Button>
        </Space>
      </Space>

      <Card>
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          <Space size={8} wrap>
            <Tag color="blue">#{account.id}</Tag>
            <Text copyable>{account.email}</Text>
            <Tag>{account.status || 'unknown'}</Tag>
            {account.region ? <Tag>{account.region}</Tag> : null}
          </Space>
          <Alert
            type="info"
            showIcon
            message="默认使用官方 ChatGPT + 当前账号 token"
            description="你可以在当前对话切换为自定义 URL/API Key/模型，也可以切换代理。官方模式支持连续上下文，自定义模式按当前消息历史重放。"
          />
        </Space>
      </Card>

      <Card title="对话配置">
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Space size={12} wrap style={{ width: '100%' }}>
            <div style={{ minWidth: 180 }}>
              <div style={{ marginBottom: 6 }}>发送方式</div>
              <Select
                value={mode}
                style={{ width: 180 }}
                onChange={(value) => setMode(value as ConversationMode)}
                options={[
                  { label: '官方 Token', value: 'official' },
                  { label: '自定义 API', value: 'custom_api' },
                ]}
              />
            </div>

            <div style={{ minWidth: 220, flex: 1 }}>
              <div style={{ marginBottom: 6 }}>代理选择</div>
              <Select
                value={selectedProxy}
                style={{ width: '100%' }}
                onChange={setSelectedProxy}
                options={proxyOptions}
                showSearch
                optionFilterProp="label"
              />
            </div>

            <div style={{ minWidth: 300, flex: 1 }}>
              <div style={{ marginBottom: 6 }}>自定义代理</div>
              <Input
                value={customProxy}
                onChange={(e) => setCustomProxy(e.target.value)}
                placeholder="留空表示使用上方选择或自动代理"
              />
            </div>

            <div style={{ minWidth: 180 }}>
              <div style={{ marginBottom: 6 }}>流式输出</div>
              <Switch
                checked={streamEnabled}
                checkedChildren="开启"
                unCheckedChildren="关闭"
                onChange={setStreamEnabled}
                disabled={sending}
              />
            </div>
          </Space>

          {mode === 'official' ? (
            <Space direction="vertical" size={12} style={{ width: '100%' }}>
              <div style={{ minWidth: 280, maxWidth: 520 }}>
                <div style={{ marginBottom: 6 }}>官方 URL</div>
                <Input
                  value={officialTargetUrl}
                  onChange={(e) => setOfficialTargetUrl(e.target.value)}
                  placeholder={OFFICIAL_DEFAULT_TARGET_URL}
                />
              </div>
              <div style={{ maxWidth: 220 }}>
                <div style={{ marginBottom: 6 }}>模型</div>
                <Input
                  value={officialModel}
                  onChange={(e) => setOfficialModel(e.target.value)}
                  placeholder="auto"
                />
              </div>
            </Space>
          ) : (
            <Space direction="vertical" size={12} style={{ width: '100%' }}>
              <div>
                <div style={{ marginBottom: 6 }}>URL</div>
                <Input
                  value={customTargetUrl}
                  onChange={(e) => setCustomTargetUrl(e.target.value)}
                  placeholder="例如 https://api.openai.com/v1/chat/completions"
                />
              </div>
              <Space size={12} wrap style={{ width: '100%' }}>
                <div style={{ minWidth: 280, flex: 1 }}>
                  <div style={{ marginBottom: 6 }}>API Key</div>
                  <Input.Password
                    value={customApiKey}
                    onChange={(e) => setCustomApiKey(e.target.value)}
                    placeholder="可留空，取决于目标接口"
                  />
                </div>
                <div style={{ minWidth: 220, flex: 1 }}>
                  <div style={{ marginBottom: 6 }}>模型</div>
                  <Input
                    value={customModel}
                    onChange={(e) => setCustomModel(e.target.value)}
                    placeholder="gpt-4o-mini"
                  />
                </div>
              </Space>
            </Space>
          )}

          <Space size={8} wrap>
            <Tag color="processing">当前代理: {effectiveProxy || '自动选择'}</Tag>
            <Tag color="purple">当前模型: {currentModel || (mode === 'official' ? 'auto' : 'gpt-4o-mini')}</Tag>
            <Tag color={streamEnabled ? 'geekblue' : 'default'}>
              返回方式: {streamEnabled ? '流式' : '非流式'}
            </Tag>
            {lastUsedProxy ? <Tag color="success">最近实际代理: {lastUsedProxy}</Tag> : null}
            {lastTargetUrl ? <Tag>最近目标: {lastTargetUrl}</Tag> : null}
            {lastModel ? <Tag>最近使用模型: {lastModel}</Tag> : null}
          </Space>
        </Space>
      </Card>

      <Card title="对话记录">
        <div
          ref={scrollRef}
          style={{
            minHeight: 320,
            maxHeight: 520,
            overflowY: 'auto',
            display: 'flex',
            flexDirection: 'column',
            gap: 12,
            paddingRight: 4,
          }}
        >
          {messages.length === 0 ? (
            <Alert
              type="info"
              showIcon
              message="还没有消息"
              description="输入内容后发送。官方模式会沿用本页会话上下文；切到自定义 API 时会按当前对话记录重放。"
            />
          ) : (
            messages.map((item) => {
              const isUser = item.role === 'user'
              return (
                <div
                  key={item.id}
                  style={{
                    alignSelf: isUser ? 'flex-end' : 'flex-start',
                    maxWidth: '78%',
                    padding: '12px 14px',
                    borderRadius: 14,
                    background: isUser ? 'rgba(79,70,229,0.14)' : 'rgba(127,127,127,0.10)',
                    border: item.status === 'error' ? '1px solid rgba(239,68,68,0.35)' : '1px solid rgba(127,127,127,0.16)',
                  }}
                >
                  <Space direction="vertical" size={6} style={{ width: '100%' }}>
                    <Space size={8}>
                      <Tag color={isUser ? 'blue' : item.status === 'error' ? 'error' : 'green'}>
                        {isUser ? '用户' : item.status === 'error' ? '错误' : '助手'}
                      </Tag>
                      {item.status === 'streaming' ? <Tag>输出中</Tag> : null}
                    </Space>
                    <Paragraph style={{ margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                      {item.content || (item.status === 'streaming' ? '...' : '')}
                    </Paragraph>
                  </Space>
                </div>
              )
            })
          )}
        </div>
      </Card>

      <Card title="发送消息">
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Input.TextArea
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            rows={5}
            placeholder="输入消息，Enter 发送，Shift+Enter 换行"
            onPressEnter={(e) => {
              if (!e.shiftKey && !sending) {
                e.preventDefault()
                handleSend()
              }
            }}
          />
          <Space style={{ justifyContent: 'space-between', width: '100%' }}>
            <Text type="secondary">
              官方模式默认目标为 <code>{OFFICIAL_DEFAULT_TARGET_URL}</code>，也支持自定义 URL，并可切换流式或非流式返回
            </Text>
            <Button
              type="primary"
              icon={<SendOutlined />}
              loading={sending}
              onClick={handleSend}
              disabled={!prompt.trim()}
            >
              发送
            </Button>
          </Space>
        </Space>
      </Card>
    </Space>
  )
}
