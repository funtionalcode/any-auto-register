import { useEffect, useRef, useState } from 'react'
import { Button, Input, message, Space, Tag, theme, Typography } from 'antd'
import { CopyOutlined, FastForwardOutlined, SendOutlined, StopOutlined } from '@ant-design/icons'

const { Text } = Typography

import { API_BASE, apiFetch, getToken } from '@/lib/utils'

interface TaskLogPanelProps {
  taskId: string
  onDone?: () => void
  onManualOtp?: (taskId: string, code: string) => Promise<void>
}

type TaskTerminalStatus = 'idle' | 'done' | 'failed' | 'stopped'

interface RegisterSummary {
  success: number
  registered: number
  total: number
}

function parseCounter(value: unknown): number {
  const n = Number(value || 0)
  if (!Number.isFinite(n) || n < 0) return 0
  return Math.floor(n)
}

function normalizeSummary(next: RegisterSummary): RegisterSummary {
  const success = parseCounter(next.success)
  const registered = Math.max(parseCounter(next.registered), success)
  const total = Math.max(parseCounter(next.total), registered)
  return { success, registered, total }
}

function mergeSummary(previous: RegisterSummary, incoming: Partial<RegisterSummary>): RegisterSummary {
  return normalizeSummary({
    success: incoming.success ?? previous.success,
    registered: incoming.registered ?? previous.registered,
    total: incoming.total ?? previous.total,
  })
}

export function TaskLogPanel({ taskId, onDone, onManualOtp }: TaskLogPanelProps) {
  const { token: themeToken } = theme.useToken()
  const [lines, setLines] = useState<string[]>([])
  const [summary, setSummary] = useState<RegisterSummary>({ success: 0, registered: 0, total: 0 })
  const [error, setError] = useState('')
  const [terminalStatus, setTerminalStatus] = useState<TaskTerminalStatus>('idle')
  const [skipLoading, setSkipLoading] = useState(false)
  const [stopLoading, setStopLoading] = useState(false)
  const [stopRequested, setStopRequested] = useState(false)
  const [otpInput, setOtpInput] = useState('')
  const [otpSubmitting, setOtpSubmitting] = useState(false)
  const [showOtpInput, setShowOtpInput] = useState(false)
  const [mailboxInfo, setMailboxInfo] = useState<{email: string; provider: string | null; account_id: string | null} | null>(null)
  const [mailboxMessages, setMailboxMessages] = useState<any[]>([])
  const [mailboxLoading, setMailboxLoading] = useState(false)
  const panelRef = useRef<HTMLDivElement>(null)
  const onDoneRef = useRef(onDone)
  const nextSinceRef = useRef(0)

  const isFinished = terminalStatus !== 'idle' || stopRequested

  const handleCopyAll = async () => {
    try {
      const text = lines.join('\n')
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text)
      } else {
        const textarea = document.createElement('textarea')
        textarea.value = text
        textarea.style.position = 'fixed'
        textarea.style.opacity = '0'
        document.body.appendChild(textarea)
        textarea.select()
        document.execCommand('copy')
        document.body.removeChild(textarea)
      }
      message.success('日志已复制')
    } catch {
      message.error('复制失败')
    }
  }

  const handleSkipCurrent = async () => {
    if (isFinished) return
    setSkipLoading(true)
    try {
      const response = await apiFetch(`/tasks/${taskId}/skip-current`, { method: 'POST' }) as {
        control?: { targeted_skip_attempts?: number }
      }
      const targeted = Number(response.control?.targeted_skip_attempts || 0)
      message.success(
        targeted > 1
          ? `已发送跳过 ${targeted} 个进行中账号请求`
          : '已发送跳过当前账号请求',
      )
    } catch (error_: unknown) {
      const detail = error_ instanceof Error ? error_.message : '请求失败'
      message.error(detail)
    } finally {
      setSkipLoading(false)
    }
  }

  const handleStopTask = async () => {
    if (isFinished) return
    setStopLoading(true)
    try {
      await apiFetch(`/tasks/${taskId}/stop`, { method: 'POST' })
      setStopRequested(true)
      message.success('已发送停止任务请求，正在停止进行中的线程')
    } catch (error_: unknown) {
      const detail = error_ instanceof Error ? error_.message : '请求失败'
      message.error(detail)
    } finally {
      setStopLoading(false)
    }
  }

  useEffect(() => {
    onDoneRef.current = onDone
  }, [onDone])

  useEffect(() => {
    if (!taskId) return
    const controller = new AbortController()
    let cancelled = false
    const baseRetryMs = 1000
    const maxRetryMs = 8000
    nextSinceRef.current = 0
    setLines([])
    setSummary({ success: 0, registered: 0, total: 0 })
    setError('')
    setTerminalStatus('idle')
    setStopRequested(false)

    const sleep = async (ms: number) =>
      new Promise((resolve) => setTimeout(resolve, ms))

    const initSnapshot = async (): Promise<boolean> => {
      try {
        const snapshot = await apiFetch(`/tasks/${taskId}`) as {
          logs?: string[]
          status?: TaskTerminalStatus | string
          success?: number
          registered?: number
          total?: number
          control?: { stop_requested?: boolean }
        }
        if (cancelled) return true

        const snapshotLines = Array.isArray(snapshot.logs) ? snapshot.logs : []
        setLines(snapshotLines)
        setSummary((previous) =>
          mergeSummary(previous, {
            success: snapshot.success,
            registered: snapshot.registered,
            total: snapshot.total,
          }),
        )
        nextSinceRef.current = snapshotLines.length
        setStopRequested(Boolean(snapshot.control?.stop_requested))

        if (snapshot.status === 'done' || snapshot.status === 'failed' || snapshot.status === 'stopped') {
          setTerminalStatus(snapshot.status)
          onDoneRef.current?.()
          return true
        }
      } catch (error_: unknown) {
        if (!cancelled) {
          const detail = error_ instanceof Error ? error_.message : '获取任务快照失败'
          setError(detail)
        }
      }
      return false
    }

    const connectStreamOnce = async (): Promise<boolean> => {
      try {
        const token = getToken()
        const headers: Record<string, string> = {}
        if (token) headers.Authorization = `Bearer ${token}`

        const since = nextSinceRef.current
        const response = await fetch(`${API_BASE}/tasks/${taskId}/logs/stream?since=${since}`, {
          headers,
          signal: controller.signal,
        })

        if (!response.ok) {
          setError(`日志流连接失败 (${response.status})`)
          return true
        }

        if (!response.body) {
          setError('日志流未返回可读数据')
          return false
        }

        setError('')
        const reader = response.body.getReader()
        const decoder = new TextDecoder()
        let buffer = ''

        while (!cancelled) {
          const { done, value } = await reader.read()
          if (done) break

          buffer += decoder.decode(value, { stream: true })
          const parts = buffer.split('\n\n')
          buffer = parts.pop() || ''

          for (const part of parts) {
            const match = part.match(/^data:\s*(.+)$/m)
            if (!match) continue
            try {
              const payload = JSON.parse(match[1]) as {
                line?: string
                done?: boolean
                status?: TaskTerminalStatus
                success?: number
                registered?: number
                total?: number
              }
              setSummary((previous) =>
                mergeSummary(previous, {
                  success: payload.success,
                  registered: payload.registered,
                  total: payload.total,
                }),
              )
              if (payload.line) {
                nextSinceRef.current += 1
                setLines((previous) => [...previous, payload.line!])
              }
              if (payload.done) {
                setTerminalStatus(payload.status || 'done')
                onDoneRef.current?.()
                return true
              }
            } catch {
              // ignore malformed SSE payload
            }
          }
        }

        return false
      } catch (error_: unknown) {
        if (!cancelled && !(error_ instanceof DOMException && error_.name === 'AbortError')) {
          return false
        }
        return true
      }
    }

    const connectStream = async () => {
      const shouldStopImmediately = await initSnapshot()
      if (shouldStopImmediately || cancelled) return

      let retryCount = 0
      while (!cancelled) {
        const shouldStop = await connectStreamOnce()
        if (shouldStop || cancelled) return

        retryCount += 1
        const retryMs = Math.min(baseRetryMs * (2 ** (retryCount - 1)), maxRetryMs)
        setError(`日志流连接中断，${retryMs / 1000}s 后重试（第 ${retryCount} 次）`)
        await sleep(retryMs)
      }
    }

    void connectStream()

    return () => {
      cancelled = true
      controller.abort()
    }
  }, [taskId])

  // Detect OTP waiting pattern in logs
  useEffect(() => {
    if (lines.length === 0) return
    const lastLines = lines.slice(-5)
    const otpWaiting = lastLines.some(line =>
      line.includes('等待邮箱验证码') ||
      line.includes('等待验证码') ||
      line.includes('正在等待') ||
      line.includes('OTP') ||
      line.includes('验证码超时')
    )
    setShowOtpInput(otpWaiting && !isFinished)
  }, [lines, isFinished])

  // Extract mailbox email from logs - prefer the email used in actual registration
  useEffect(() => {
    if (lines.length === 0 || mailboxInfo) return
    // Priority 1: "邮箱: xxx" from registration platform (actual email used)
    // Priority 2: "临时邮箱: xxx" from our pre-log
    for (const line of lines) {
      // Match "邮箱: odessa7975ca@y8.cloudvxz.com" from the platform's log
      const platformMatch = line.match(/邮箱:\s*(\S+?@\S+?)(?:[,，\s]|$)/)
      if (platformMatch) {
        setMailboxInfo({ email: platformMatch[1].replace(/[,，]+$/, ''), provider: null, account_id: null })
        return
      }
    }
    for (const line of lines) {
      const match = line.match(/临时邮箱:\s*(\S+?@\S+?)(?:[,，\s]|$)/)
      if (match) {
        setMailboxInfo({ email: match[1].replace(/[,，]+$/, ''), provider: null, account_id: null })
        return
      }
    }
  }, [lines, mailboxInfo])

  // Fetch mailbox info from API once we have a taskId
  useEffect(() => {
    if (!taskId || mailboxInfo) return
    let cancelled = false
    apiFetch(`/tasks/${taskId}/mailbox`)
      .then((data: any) => {
        if (!cancelled && data.email) {
          setMailboxInfo({ email: data.email, provider: data.provider, account_id: data.account_id })
        }
      })
      .catch(() => {})
    return () => { cancelled = true }
  }, [taskId, mailboxInfo])

  const handleFetchMessages = async () => {
    if (!taskId) return
    setMailboxLoading(true)
    setMailboxMessages([])
    try {
      const data = await apiFetch(`/tasks/${taskId}/mailbox/messages`, {
        method: 'POST',
        body: JSON.stringify({ limit: 10 }),
      }) as any
      setMailboxMessages(data.messages || [])
    } catch (e: any) {
      message.error(e?.message || '获取邮件失败')
    } finally {
      setMailboxLoading(false)
    }
  }

  const handleSubmitOtp = async () => {
    const code = otpInput.trim()
    if (!code) return
    setOtpSubmitting(true)
    try {
      if (onManualOtp) {
        await onManualOtp(taskId, code)
      } else {
        await apiFetch(`/tasks/${taskId}/otp`, {
          method: 'POST',
          body: JSON.stringify({ code }),
        })
      }
      message.success('验证码已提交')
      setOtpInput('')
      setShowOtpInput(false)
    } catch (e: any) {
      message.error(e?.message || '提交验证码失败')
    } finally {
      setOtpSubmitting(false)
    }
  }

  useEffect(() => {
    if (!panelRef.current) return
    panelRef.current.scrollTop = panelRef.current.scrollHeight
  }, [lines])

  const footerText =
    terminalStatus === 'done'
      ? { text: '注册完成', color: '#10b981' }
      : terminalStatus === 'stopped'
        ? { text: '任务已停止', color: '#d97706' }
        : terminalStatus === 'failed'
          ? { text: '任务失败', color: '#dc2626' }
          : null

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <Space wrap style={{ marginBottom: 8 }}>
        <Tag color="green">注册成功：{summary.success}</Tag>
        <Tag color="blue">已注册：{summary.registered}</Tag>
        <Tag color="default">总共注册：{summary.total}</Tag>
      </Space>

      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
        <Space>
          <Button
            size="small"
            icon={<FastForwardOutlined />}
            onClick={handleSkipCurrent}
            loading={skipLoading}
            disabled={isFinished}
          >
            跳过当前账号
          </Button>
          <Button
            size="small"
            danger
            icon={<StopOutlined />}
            onClick={handleStopTask}
            loading={stopLoading}
            disabled={isFinished}
          >
            停止任务
          </Button>
        </Space>
        <Button size="small" icon={<CopyOutlined />} onClick={handleCopyAll} disabled={lines.length === 0}>
          复制日志
        </Button>
      </div>

      <div
        ref={panelRef}
        className="log-panel"
        style={{
          flex: 1,
          overflowY: 'auto',
          overflowX: 'hidden',
          background: themeToken.colorBgContainer,
          border: `1px solid ${themeToken.colorBorder}`,
          borderRadius: 8,
          padding: 12,
          fontFamily: 'monospace',
          fontSize: 12,
          minHeight: 320,
          maxHeight: '65vh',
          userSelect: 'text',
          WebkitUserSelect: 'text',
          cursor: 'text',
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-word',
        }}
      >
        {lines.length === 0 && !error && <div style={{ color: '#9ca3af' }}>等待日志...</div>}
        {error && <div style={{ color: '#dc2626' }}>{error}</div>}
        {lines.map((line, index) => (
          <div
            key={index}
            style={{
              lineHeight: 1.5,
              color:
                line.includes('✓') || line.includes('成功')
                  ? '#059669'
                  : line.includes('✗') || line.includes('失败') || line.includes('错误')
                    ? '#dc2626'
                    : line.includes('停止') || line.includes('跳过')
                      ? '#d97706'
                      : themeToken.colorText,
            }}
          >
            {line}
          </div>
        ))}
      </div>

      {footerText ? (
        <div style={{ fontSize: 12, color: footerText.color, marginTop: 8 }}>
          {footerText.text}
        </div>
      ) : null}

      {mailboxInfo && (
        <div style={{ marginTop: 8, padding: '8px 12px', background: themeToken.colorBgContainer, border: `1px solid ${themeToken.colorBorderSecondary}`, borderRadius: 8 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <Space>
              <Text strong style={{ fontSize: 13 }}>临时邮箱</Text>
              <Text code style={{ fontSize: 12, userSelect: 'text' }}>{mailboxInfo.email}</Text>
              {mailboxInfo.provider && <Tag color="blue" style={{ fontSize: 11 }}>{mailboxInfo.provider}</Tag>}
            </Space>
            <Button size="small" loading={mailboxLoading} onClick={handleFetchMessages}>
              收取邮件
            </Button>
          </div>
          {mailboxMessages.length > 0 && (
            <div style={{ marginTop: 8, maxHeight: 200, overflowY: 'auto' }}>
              {mailboxMessages.map((msg: any, idx: number) => (
                <div key={idx} style={{
                  padding: '6px 8px',
                  borderBottom: idx < mailboxMessages.length - 1 ? `1px solid ${themeToken.colorBorderSecondary}` : 'none',
                  fontSize: 12,
                }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                    <Text strong style={{ fontSize: 11 }}>{msg.subject || '(无主题)'}</Text>
                    <Text type="secondary" style={{ fontSize: 11 }}>{msg.sender || ''}</Text>
                  </div>
                  {msg.verification_code && (
                    <Tag color="green" style={{ fontSize: 12, fontWeight: 600, marginTop: 4 }}>
                      验证码: {msg.verification_code}
                    </Tag>
                  )}
                  {(msg.preview || msg.content) && !msg.verification_code && (
                    <Text type="secondary" style={{ fontSize: 11, display: 'block', marginTop: 2 }} ellipsis>
                      {msg.preview || msg.content}
                    </Text>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {showOtpInput && (
        <div style={{ marginTop: 8, display: 'flex', gap: 8, alignItems: 'center' }}>
          <Input
            placeholder='输入验证码'
            value={otpInput}
            onChange={(e) => setOtpInput(e.target.value)}
            onPressEnter={handleSubmitOtp}
            style={{ flex: 1 }}
            maxLength={10}
          />
          <Button
            type='primary'
            icon={<SendOutlined />}
            loading={otpSubmitting}
            onClick={handleSubmitOtp}
          >
            提交验证码
          </Button>
        </div>
      )}
    </div>
  )
}

export default TaskLogPanel
