import { useEffect, useRef, useState } from 'react'
import { Card, Form, Input, Select, Button, message, Tabs, Space, Tag, Typography, Modal, Segmented } from 'antd'
import type { FormInstance } from 'antd'
import {
  SaveOutlined,
  EyeOutlined,
  EyeInvisibleOutlined,
  MailOutlined,
  SafetyOutlined,
  ApiOutlined,
  FileTextOutlined,
  PlusOutlined,
} from '@ant-design/icons'
import { apiFetch } from '@/lib/utils'

const SELECT_FIELDS: Record<string, { label: string; value: string }[]> = {
  mail_provider: [
    { label: 'Laoudo（固定邮箱）', value: 'laoudo' },
    { label: 'TempMail.lol（自动生成）', value: 'tempmail_lol' },
    { label: 'SkyMail（CloudMail 接口）', value: 'skymail' },
    { label: 'DuckMail（自动生成）', value: 'duckmail' },
    { label: 'MoeMail (sall.cc)', value: 'moemail' },
    { label: 'YYDS Mail / MaliAPI', value: 'maliapi' },
    { label: 'Freemail（自建 CF Worker）', value: 'freemail' },
    { label: 'CF Worker（自建域名）', value: 'cfworker' },
    { label: 'LuckMail（订单接码 / 已购邮箱）', value: 'luckmail' },
    { label: 'API Mail（Mail.tm 临时邮箱）', value: 'api_mail' },
  ],
  maliapi_auto_domain_strategy: [
    { label: 'balanced', value: 'balanced' },
    { label: 'prefer_owned', value: 'prefer_owned' },
    { label: 'prefer_public', value: 'prefer_public' },
  ],
  default_executor: [
    { label: 'API 协议（无浏览器）', value: 'protocol' },
    { label: '无头浏览器', value: 'headless' },
    { label: '有头浏览器', value: 'headed' },
  ],
  default_captcha_solver: [
    { label: 'YesCaptcha', value: 'yescaptcha' },
    { label: '本地 Solver (Camoufox)', value: 'local_solver' },
    { label: '手动', value: 'manual' },
  ],
  cpa_cleanup_enabled: [
    { label: '关闭', value: '0' },
    { label: '开启', value: '1' },
  ],
  codex_proxy_upload_type: [
    { label: 'AT（Access Token，推荐）', value: 'at' },
    { label: 'RT（Refresh Token）', value: 'rt' },
  ],
  request_logging_enabled: [
    { label: '关闭', value: '0' },
    { label: '开启', value: '1' },
  ],
}

const CHATGPT_MODULE_OPTIONS = [
  { label: 'CPA / CLIProxyAPI', value: 'cpa' },
  { label: 'Sub2API', value: 'sub2api' },
  { label: 'CPA 自动维护', value: 'cpa_cleanup' },
  { label: 'Team Manager', value: 'team_manager' },
  { label: 'CodexProxy', value: 'codex_proxy' },
  { label: 'SMSToMe 手机验证', value: 'smstome' },
]
const CHATGPT_MODULE_KEYS = CHATGPT_MODULE_OPTIONS.map((option) => option.value)
const SETTINGS_ACTIVE_TAB_STORAGE_KEY = 'settings.activeTab'

const TAB_ITEMS = [
  {
    key: 'register',
    label: '注册设置',
    icon: <ApiOutlined />,
    sections: [
      {
        title: '默认注册方式',
        desc: '控制注册任务如何执行',
        fields: [{ key: 'default_executor', label: '执行器类型', type: 'select' }],
      },
    ],
  },
  {
    key: 'mailbox',
    label: '邮箱服务',
    icon: <MailOutlined />,
    sections: [],
  },
  {
    key: 'captcha',
    label: '验证码',
    icon: <SafetyOutlined />,
    sections: [
      {
        title: '验证码服务',
        desc: '用于绕过注册页面的人机验证',
        fields: [
          { key: 'default_captcha_solver', label: '默认服务', type: 'select' },
          { key: 'yescaptcha_key', label: 'YesCaptcha Key', secret: true },
        ],
      },
    ],
  },
  {
    key: 'logs',
    label: '日志管理',
    icon: <FileTextOutlined />,
    sections: [],
  },
  {
    key: 'chatgpt',
    label: 'ChatGPT',
    icon: <ApiOutlined />,
    sections: [
      {
        key: 'cpa',
        title: 'CPA / CLIProxyAPI 面板',
        desc: '注册完成后自动上传到兼容 CPA Management API 的平台；API Key 留空时会自动复用 CLIProxyAPI 管理口令',
        fields: [
          { key: 'cpa_api_url', label: 'API URL', placeholder: 'https://your-cpa.example.com 或 http://127.0.0.1:8317' },
          { key: 'cpa_api_key', label: 'API Key', secret: true, placeholder: '留空则自动复用 CLIProxyAPI 管理口令' },
        ],
      },
      {
        key: 'sub2api',
        title: 'Sub2API 面板',
        desc: '注册完成后自动上传到 Sub2API 管理后台',
        fields: [
          { key: 'sub2api_api_url', label: 'API URL', placeholder: 'https://your-sub2api.example.com' },
          { key: 'sub2api_api_key', label: 'API Key', secret: true },
        ],
      },
      {
        key: 'cpa_cleanup',
        title: 'CPA 自动维护',
        desc: '定时删除 status=error 的凭证，剩余数量低于阈值时自动按现有配置补注册 ChatGPT',
        fields: [
          { key: 'cpa_cleanup_enabled', label: '自动维护', type: 'select' },
          { key: 'cpa_cleanup_interval_minutes', label: '检查间隔（分钟）', placeholder: '60' },
          { key: 'cpa_cleanup_threshold', label: '最低凭证阈值', placeholder: '5' },
          { key: 'cpa_cleanup_concurrency', label: '补注册并发数', placeholder: '1' },
          { key: 'cpa_cleanup_register_delay_seconds', label: '每个注册延迟（秒）', placeholder: '0' },
        ],
      },
      {
        key: 'team_manager',
        title: 'Team Manager',
        desc: '上传到自建 Team Manager 系统',
        fields: [
          { key: 'team_manager_url', label: 'API URL', placeholder: 'https://your-tm.example.com' },
          { key: 'team_manager_key', label: 'API Key', secret: true },
        ],
      },
      {
        key: 'codex_proxy',
        title: 'CodexProxy',
        desc: '注册完成后自动上传到 CodexProxy 管理平台',
        fields: [
          { key: 'codex_proxy_url', label: 'API URL', placeholder: 'https://your-codex-proxy.example.com' },
          { key: 'codex_proxy_key', label: 'Admin Key', secret: true },
          { key: 'codex_proxy_upload_type', label: '上传类型' },
        ],
      },
      {
        key: 'smstome',
        title: 'SMSToMe 手机验证',
        desc: 'ChatGPT add_phone 阶段自动取号并轮询短信验证码',
        fields: [
          { key: 'smstome_cookie', label: 'SMSToMe Cookie', secret: true },
          { key: 'smstome_country_slugs', label: '国家列表', placeholder: 'united-kingdom,poland' },
          { key: 'smstome_phone_attempts', label: '手机号尝试次数', placeholder: '3' },
          { key: 'smstome_otp_timeout_seconds', label: '短信等待秒数', placeholder: '45' },
          { key: 'smstome_poll_interval_seconds', label: '轮询间隔秒数', placeholder: '5' },
          { key: 'smstome_sync_max_pages_per_country', label: '每国同步页数', placeholder: '5' },
        ],
      },
    ],
  },
  {
    key: 'cliproxyapi',
    label: 'CLIProxyAPI',
    icon: <ApiOutlined />,
    sections: [
      {
        title: '管理面板',
        desc: '用于 CLIProxyAPI 管理页登录',
        fields: [
          { key: 'cliproxyapi_management_key', label: '管理口令', secret: true, placeholder: '默认 cliproxyapi' },
        ],
      },
    ],
  },
  {
    key: 'grok',
    label: 'Grok',
    icon: <ApiOutlined />,
    sections: [
      {
        title: 'grok2api',
        desc: '注册成功后自动导入到 grok2api 管理后台',
        fields: [
          { key: 'grok2api_url', label: 'API URL', placeholder: 'http://127.0.0.1:7860' },
          { key: 'grok2api_app_key', label: 'App Key', secret: true },
          { key: 'grok2api_pool', label: 'Token Pool', placeholder: 'ssoBasic 或 ssoSuper' },
          { key: 'grok2api_quota', label: 'Quota（可选）', placeholder: '留空按池默认值' },
        ],
      },
    ],
  },
  {
    key: 'kiro',
    label: 'Kiro',
    icon: <ApiOutlined />,
    sections: [
      {
        title: 'Kiro Account Manager',
        desc: '注册成功后自动写入 kiro-account-manager 的 accounts.json',
        fields: [
          {
            key: 'kiro_manager_path',
            label: 'accounts.json 路径（可选）',
            placeholder: '留空则自动使用系统默认路径',
          },
          {
            key: 'kiro_manager_exe',
            label: 'Kiro Manager 可执行文件（可选）',
            placeholder: '未安装 Rust 时可填写已安装的 KiroAccountManager.exe',
          },
        ],
      },
    ],
  },
  {
    key: 'integrations',
    label: '插件',
    icon: <ApiOutlined />,
    sections: [],
  },
]

interface FieldConfig {
  key: string
  label: string
  placeholder?: string
  type?: 'select' | 'input'
  secret?: boolean
}

interface SectionConfig {
  key?: string
  title: string
  desc?: string
  fields: FieldConfig[]
}

interface MailboxServiceConfig {
  key: string
  label: string
  title: string
  desc: string
  fields: FieldConfig[]
}

interface MailboxInboxItem {
  id: string
  subject: string
  from: string
  to: string
  created_at: string
  preview: string
  content: string
  html: string
  verification_code: string
}

interface TabConfig {
  key: string
  label: string
  icon: React.ReactNode
  sections: SectionConfig[]
}

function formatResultText(data: unknown) {
  if (typeof data === 'string') return data
  try {
    return JSON.stringify(data, null, 2)
  } catch {
    return String(data)
  }
}

function buildMailboxHtmlDocument(rawHtml: string) {
  const html = String(rawHtml || '').trim()
  if (!html) return ''
  if (/<html[\s>]|<!doctype/i.test(html)) return html
  return `<!doctype html><html><head><meta charset="utf-8" /><base target="_blank" /></head><body>${html}</body></html>`
}

function normalizeDomainList(input: unknown): string[] {
  const items = Array.isArray(input) ? input : []
  const seen = new Set<string>()
  const domains: string[] = []
  for (const item of items) {
    const domain = String(item || '').trim().toLowerCase().replace(/^@/, '')
    if (!domain || seen.has(domain)) continue
    seen.add(domain)
    domains.push(domain)
  }
  return domains
}

function parseStoredDomainList(value: unknown): string[] {
  if (Array.isArray(value)) return normalizeDomainList(value)
  if (typeof value !== 'string') return []

  const text = value.trim()
  if (!text) return []

  try {
    const parsed = JSON.parse(text)
    if (Array.isArray(parsed)) {
      return normalizeDomainList(parsed)
    }
  } catch {}

  return normalizeDomainList(
    text
      .split('\n')
      .flatMap((line) => line.split(','))
      .map((item) => item.trim()),
  )
}

function normalizeSelectionList(input: unknown, allowedValues: string[]): string[] {
  const items = Array.isArray(input) ? input : []
  const allowed = new Set(allowedValues)
  const seen = new Set<string>()
  const values: string[] = []

  for (const item of items) {
    const value = String(item || '').trim()
    if (!value || !allowed.has(value) || seen.has(value)) continue
    seen.add(value)
    values.push(value)
  }

  return values
}

function parseStoredSelectionList(value: unknown, allowedValues: string[]): string[] {
  if (Array.isArray(value)) return normalizeSelectionList(value, allowedValues)
  if (typeof value !== 'string') return []

  const text = value.trim()
  if (!text) return []

  try {
    const parsed = JSON.parse(text)
    if (Array.isArray(parsed)) {
      return normalizeSelectionList(parsed, allowedValues)
    }
  } catch {}

  return normalizeSelectionList(
    text.split(',').map((item) => item.trim()),
    allowedValues,
  )
}

function ConfigField({ field }: { field: FieldConfig }) {
  const [showSecret, setShowSecret] = useState(false)
  const options = SELECT_FIELDS[field.key]
  const helpText =
    field.key === 'default_executor'
      ? '仅对支持的平台生效；ChatGPT、Cursor、Grok、Kiro、Tavily、Trae 支持浏览器模式，OpenBlockLabs 仅支持纯协议。'
      : undefined

  return (
    <Form.Item label={field.label} name={field.key} extra={helpText} preserve>
      {options ? (
        <Select options={options} style={{ width: '100%' }} />
      ) : field.secret ? (
        <Input.Password
          placeholder={field.placeholder}
          visibilityToggle={{
            visible: !showSecret,
            onVisibleChange: setShowSecret,
          }}
          iconRender={(visible) => (visible ? <EyeOutlined /> : <EyeInvisibleOutlined />)}
        />
      ) : (
        <Input placeholder={field.placeholder} />
      )}
    </Form.Item>
  )
}

function ConfigSection({ section }: { section: SectionConfig }) {
  return (
    <Card title={section.title} extra={section.desc && <span style={{ fontSize: 12, color: '#7a8ba3' }}>{section.desc}</span>} style={{ marginBottom: 16 }}>
      {section.fields.map((field) => (
        <ConfigField key={field.key} field={field} />
      ))}
    </Card>
  )
}

const MAILBOX_SERVICES: MailboxServiceConfig[] = [
  {
    key: 'laoudo',
    label: 'Laoudo（固定邮箱）',
    title: 'Laoudo',
    desc: '固定邮箱，手动配置',
    fields: [
      { key: 'laoudo_email', label: '邮箱地址', placeholder: 'xxx@laoudo.com' },
      { key: 'laoudo_account_id', label: 'Account ID', placeholder: '563' },
      { key: 'laoudo_auth', label: 'JWT Token', placeholder: 'eyJ...', secret: true },
    ],
  },
  {
    key: 'tempmail_lol',
    label: 'TempMail.lol（自动生成）',
    title: 'TempMail.lol',
    desc: '自动生成邮箱，无需配置，需要代理访问（CN IP 被封）',
    fields: [],
  },
  {
    key: 'skymail',
    label: 'SkyMail（CloudMail 接口）',
    title: 'SkyMail',
    desc: 'CloudMail 兼容接口（addUser / emailList）',
    fields: [
      { key: 'skymail_api_base', label: 'API Base', placeholder: 'https://api.skymail.ink' },
      { key: 'skymail_token', label: 'Authorization Token', secret: true },
      { key: 'skymail_domain', label: '邮箱域名', placeholder: 'mail.example.com' },
    ],
  },
  {
    key: 'duckmail',
    label: 'DuckMail（自动生成）',
    title: 'DuckMail',
    desc: '自动生成邮箱，随机创建账号',
    fields: [
      { key: 'duckmail_api_url', label: 'Web URL', placeholder: 'https://www.duckmail.sbs' },
      { key: 'duckmail_provider_url', label: 'Provider URL', placeholder: 'https://api.duckmail.sbs' },
      { key: 'duckmail_bearer', label: 'Bearer Token', placeholder: 'kevin273945', secret: true },
      { key: 'duckmail_domain', label: '自定义域名', placeholder: '留空则从 Provider URL 推导' },
      { key: 'duckmail_api_key', label: 'API Key（私有域名）', placeholder: 'dk_xxx（domain.duckmail.sbs 获取）', secret: true },
    ],
  },
  {
    key: 'moemail',
    label: 'MoeMail (sall.cc)',
    title: 'MoeMail',
    desc: '自动注册账号并生成临时邮箱',
    fields: [{ key: 'moemail_api_url', label: 'API URL', placeholder: 'https://sall.cc' }],
  },
  {
    key: 'maliapi',
    label: 'YYDS Mail / MaliAPI',
    title: 'YYDS Mail / MaliAPI',
    desc: '基于 API Key 创建临时邮箱并轮询收件箱消息',
    fields: [
      { key: 'maliapi_base_url', label: 'API URL', placeholder: 'https://maliapi.215.im/v1' },
      { key: 'maliapi_api_key', label: 'API Key', secret: true },
      { key: 'maliapi_domain', label: '邮箱域名（可选）', placeholder: 'example.com' },
      { key: 'maliapi_auto_domain_strategy', label: '自动域名策略', type: 'select' },
    ],
  },
  {
    key: 'freemail',
    label: 'Freemail（自建 CF Worker）',
    title: 'Freemail',
    desc: '基于 Cloudflare Worker 的自建邮箱，支持管理员令牌或账号密码认证',
    fields: [
      { key: 'freemail_api_url', label: 'API URL', placeholder: 'https://mail.example.com' },
      { key: 'freemail_admin_token', label: '管理员令牌', secret: true },
      { key: 'freemail_username', label: '用户名（可选）' },
      { key: 'freemail_password', label: '密码（可选）', secret: true },
    ],
  },
  {
    key: 'cfworker',
    label: 'CF Worker（自建域名）',
    title: 'CF Worker 自建邮箱',
    desc: '基于 Cloudflare Worker 的自建临时邮箱服务',
    fields: [
      { key: 'cfworker_api_url', label: 'API URL', placeholder: 'https://apimail.example.com' },
      { key: 'cfworker_admin_token', label: '管理员 Token', secret: true },
      { key: 'cfworker_custom_auth', label: '站点密码', secret: true },
      { key: 'cfworker_fingerprint', label: 'Fingerprint', placeholder: '6703363b...' },
    ],
  },
  {
    key: 'luckmail',
    label: 'LuckMail（订单接码 / 已购邮箱）',
    title: 'LuckMail',
    desc: 'ChatGPT 走购买邮箱，其他平台继续走订单接码老逻辑',
    fields: [
      { key: 'luckmail_base_url', label: '平台地址', placeholder: 'https://mails.luckyous.com' },
      { key: 'luckmail_api_key', label: 'API Key', secret: true },
      { key: 'luckmail_email_type', label: '邮箱类型（可选）', placeholder: 'ms_graph / ms_imap / self_built' },
      { key: 'luckmail_domain', label: '邮箱域名（可选）', placeholder: 'outlook.com / gmail.com' },
    ],
  },
  {
    key: 'api_mail',
    label: 'API Mail（Mail.tm 临时邮箱）',
    title: 'API Mail (Mail.tm)',
    desc: '基于 Mail.tm 的临时邮箱服务，自动生成邮箱并接收验证码',
    fields: [
      { key: 'api_mail_tm_password', label: '邮箱密码', secret: true, placeholder: '默认 MailTm123!' },
    ],
  },
]
const MAILBOX_SERVICE_KEYS = MAILBOX_SERVICES.map((service) => service.key)

function MailboxSettingsPanel({ form }: { form: FormInstance }) {
  const selectedServiceKey = Form.useWatch('mail_provider', form) || 'moemail'
  const enabledServiceKeys = normalizeSelectionList(
    Form.useWatch('mailbox_services_enabled', form) || [],
    MAILBOX_SERVICES.map((service) => service.key),
  )
  const effectiveServiceKeys = enabledServiceKeys.length > 0 ? enabledServiceKeys : [selectedServiceKey]
  const selectedServices = MAILBOX_SERVICES.filter((service) => effectiveServiceKeys.includes(service.key))
  const [inboxEmail, setInboxEmail] = useState('')
  const [inboxAccountId, setInboxAccountId] = useState('')
  const [inboxExtra, setInboxExtra] = useState<Record<string, unknown>>({})
  const [inboxProxy, setInboxProxy] = useState('')
  const [inboxLoading, setInboxLoading] = useState(false)
  const [inboxCreating, setInboxCreating] = useState(false)
  const [inboxLoaded, setInboxLoaded] = useState(false)
  const [inboxItems, setInboxItems] = useState<MailboxInboxItem[]>([])
  const [activeInboxItem, setActiveInboxItem] = useState<MailboxInboxItem | null>(null)
  const activeInboxHtml = buildMailboxHtmlDocument(activeInboxItem?.html || '')

  useEffect(() => {
    setInboxItems([])
    setInboxLoaded(false)
    setInboxExtra({})
    setActiveInboxItem(null)
  }, [selectedServiceKey])

  const createTestInbox = async () => {
    setInboxCreating(true)
    try {
      const data = await apiFetch('/mailbox/inbox/create', {
        method: 'POST',
        body: JSON.stringify({
          provider: selectedServiceKey,
          config: form.getFieldsValue(true),
          proxy: inboxProxy,
        }),
      })
      setInboxEmail(String(data.email || ''))
      setInboxAccountId(String(data.account_id || ''))
      setInboxExtra(data.extra && typeof data.extra === 'object' ? data.extra : {})
      setInboxItems([])
      setInboxLoaded(false)
      message.success('测试邮箱已生成')
    } catch (e: any) {
      message.error(e?.message || '生成测试邮箱失败')
    } finally {
      setInboxCreating(false)
    }
  }

  const loadInbox = async () => {
    setInboxLoading(true)
    try {
      const data = await apiFetch('/mailbox/inbox/messages', {
        method: 'POST',
        body: JSON.stringify({
          provider: selectedServiceKey,
          config: form.getFieldsValue(true),
          email: inboxEmail,
          account_id: inboxAccountId,
          extra: inboxExtra,
          proxy: inboxProxy,
          limit: 10,
        }),
      })
      setInboxEmail(String(data.email || inboxEmail || ''))
      setInboxAccountId(String(data.account_id || inboxAccountId || ''))
      setInboxItems(Array.isArray(data.items) ? data.items : [])
      setInboxLoaded(true)
      message.success(`收件箱刷新完成，共 ${Number(data.total || 0)} 封邮件`)
    } catch (e: any) {
      message.error(e?.message || '读取收件箱失败')
    } finally {
      setInboxLoading(false)
    }
  }

  return (
    <>
      <Modal
        open={Boolean(activeInboxItem)}
        title="邮件详情"
        width={900}
        onCancel={() => setActiveInboxItem(null)}
        onOk={() => setActiveInboxItem(null)}
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginBottom: 12 }}>
          <div>主题：{activeInboxItem?.subject || '-'}</div>
          <div>发件人：{activeInboxItem?.from || '-'}</div>
          <div>收件人：{activeInboxItem?.to || '-'}</div>
          <div>时间：{activeInboxItem?.created_at || '-'}</div>
          <div>验证码：{activeInboxItem?.verification_code || '-'}</div>
        </div>
        {activeInboxHtml ? (
          <Tabs
            key={activeInboxItem?.id || 'mailbox-detail'}
            defaultActiveKey="html"
            items={[
              {
                key: 'html',
                label: '渲染视图',
                children: (
                  <iframe
                    title="邮件 HTML 预览"
                    sandbox=""
                    srcDoc={activeInboxHtml}
                    style={{
                      width: '100%',
                      minHeight: 520,
                      border: '1px solid rgba(127,127,127,0.16)',
                      borderRadius: 8,
                      background: '#fff',
                    }}
                  />
                ),
              },
              {
                key: 'text',
                label: '文本视图',
                children: (
                  <pre
                    style={{
                      margin: 0,
                      maxHeight: 520,
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
                    {activeInboxItem?.content || activeInboxItem?.preview || '暂无内容'}
                  </pre>
                ),
              },
            ]}
          />
        ) : (
          <pre
            style={{
              margin: 0,
              maxHeight: 520,
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
            {activeInboxItem?.content || activeInboxItem?.preview || '暂无内容'}
          </pre>
        )}
      </Modal>

      <Card
        title="邮箱服务"
        extra={<span style={{ fontSize: 12, color: '#7a8ba3' }}>默认邮箱服务仍为单选；下方多选决定哪些服务会在设置页和注册页中显示</span>}
        style={{ marginBottom: 16 }}
      >
        <ConfigField field={{ key: 'mail_provider', label: '邮箱服务', type: 'select' }} />
        <Form.Item label="启用服务" name="mailbox_services_enabled" preserve extra="支持多选；注册页邮箱服务下拉只显示这些已启用项">
          <Select mode="multiple" options={MAILBOX_SERVICES.map((service) => ({ label: service.label, value: service.key }))} />
        </Form.Item>
      </Card>

      {selectedServices.map((service) => (
        <Card
          key={service.key}
          title={service.title}
          extra={<span style={{ fontSize: 12, color: '#7a8ba3' }}>{service.desc}</span>}
          style={{ marginBottom: 16 }}
        >
          {service.fields.length > 0 ? (
            service.fields.map((field) => <ConfigField key={field.key} field={field} />)
          ) : (
            <Typography.Text type="secondary">当前服务无需额外配置。</Typography.Text>
          )}
        </Card>
      ))}

      {effectiveServiceKeys.includes('cfworker') ? <CFWorkerDomainPoolSection form={form} /> : null}

      <Card
        title="收件箱测试"
        extra={<span style={{ fontSize: 12, color: '#7a8ba3' }}>用于单独测试某个邮箱账号的收件情况和验证码提取</span>}
        style={{ marginBottom: 16 }}
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <Typography.Text type="secondary">
            可直接生成测试邮箱，也可手动填写已有邮箱和账号 ID / Token 查看当前收件箱。部分服务如 TempMail.lol、DuckMail、API Mail 需要 Token。
          </Typography.Text>
          <Form.Item label="邮箱地址" style={{ marginBottom: 0 }}>
            <Input
              value={inboxEmail}
              onChange={(event) => setInboxEmail(event.target.value)}
              placeholder="demo@example.com"
            />
          </Form.Item>
          <Form.Item label="账号 ID / Token（可选）" style={{ marginBottom: 0 }}>
            <Input
              value={inboxAccountId}
              onChange={(event) => setInboxAccountId(event.target.value)}
              placeholder="account id / token"
            />
          </Form.Item>
          <Form.Item label="代理（可选）" style={{ marginBottom: 0 }}>
            <Input
              value={inboxProxy}
              onChange={(event) => setInboxProxy(event.target.value)}
              placeholder="http://127.0.0.1:7890"
            />
          </Form.Item>
          <Space wrap>
            <Button onClick={createTestInbox} loading={inboxCreating}>
              生成测试邮箱
            </Button>
            <Button type="primary" onClick={loadInbox} loading={inboxLoading}>
              刷新收件箱
            </Button>
          </Space>
          {(inboxEmail || inboxAccountId) ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              <div>当前测试邮箱：{inboxEmail || '-'}</div>
              <div>当前账号 ID / Token：{inboxAccountId || '-'}</div>
            </div>
          ) : null}
          {inboxItems.length > 0 ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              {inboxItems.map((item) => (
                <Card key={item.id || `${item.subject}-${item.created_at}`} size="small">
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                    <Space wrap>
                      <Tag>{item.subject || '无主题'}</Tag>
                      {item.verification_code ? <Tag color="success">验证码 {item.verification_code}</Tag> : null}
                      {item.created_at ? <Tag color="blue">{item.created_at}</Tag> : null}
                    </Space>
                    {item.from ? <div>发件人：{item.from}</div> : null}
                    <div style={{ color: '#7a8ba3' }}>{item.preview || item.content || '暂无预览'}</div>
                    <Space>
                      <Button type="link" style={{ padding: 0 }} onClick={() => setActiveInboxItem(item)}>
                        查看详情
                      </Button>
                    </Space>
                  </div>
                </Card>
              ))}
            </div>
          ) : inboxLoaded ? (
            <Typography.Text type="secondary">当前收件箱暂无邮件。</Typography.Text>
          ) : (
            <Typography.Text type="secondary">点击“刷新收件箱”后，这里会显示最近邮件列表。</Typography.Text>
          )}
        </div>
      </Card>
    </>
  )
}

function ChatGptSettingsPanel({ sections, form }: { sections: SectionConfig[]; form: FormInstance }) {
  const enabledModuleKeys = normalizeSelectionList(
    Form.useWatch('chatgpt_modules_enabled', form) || [],
    CHATGPT_MODULE_OPTIONS.map((option) => option.value),
  )
  const effectiveModuleKeys = enabledModuleKeys.length > 0 ? enabledModuleKeys : CHATGPT_MODULE_OPTIONS.map((option) => option.value)
  const visibleSections = sections.filter((section) => !section.key || effectiveModuleKeys.includes(section.key))

  return (
    <>
      <Card
        title="ChatGPT 模块"
        extra={<span style={{ fontSize: 12, color: '#7a8ba3' }}>支持多选；仅已启用模块会在这里展示，涉及自动同步的模块也只会执行这些已启用项</span>}
        style={{ marginBottom: 16 }}
      >
        <Form.Item label="启用模块" name="chatgpt_modules_enabled" preserve>
          <Select mode="multiple" options={CHATGPT_MODULE_OPTIONS} />
        </Form.Item>
      </Card>

      {visibleSections.map((section) => (
        <ConfigSection key={section.key || section.title} section={section} />
      ))}
    </>
  )
}

function CFWorkerDomainPoolSection({ form }: { form: FormInstance }) {
  const watchedDomains = Form.useWatch('cfworker_domains', form) || []
  const watchedEnabledDomains = Form.useWatch('cfworker_enabled_domains', form) || []
  const normalizedDomains = normalizeDomainList(watchedDomains)
  const enabledDomains = normalizeDomainList(watchedEnabledDomains).filter((domain) => normalizedDomains.includes(domain))

  const updateEnabledDomains = (nextDomains: string[]) => {
    form.setFieldValue('cfworker_enabled_domains', normalizeDomainList(nextDomains))
  }

  const toggleEnabledDomain = (domain: string, checked: boolean) => {
    if (checked) {
      updateEnabledDomains([...enabledDomains, domain])
      return
    }
    updateEnabledDomains(enabledDomains.filter((item) => item !== domain))
  }

  return (
    <Card
      title="CF Worker 域名池"
      extra={<span style={{ fontSize: 12, color: '#7a8ba3' }}>注册时会从已启用域名中随机选择一个</span>}
      style={{ marginBottom: 16 }}
    >
      <Form.List name="cfworker_domains">
        {(fields, { add, remove }) => (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            {fields.map((field) => (
              <Space key={field.key} align="start" style={{ display: 'flex' }}>
                <Form.Item
                  {...field}
                  label={field.name === 0 ? '全部域名' : ''}
                  style={{ flex: 1, marginBottom: 0 }}
                  rules={[
                    {
                      validator: async (_, value) => {
                        if (!String(value || '').trim()) {
                          throw new Error('请输入域名')
                        }
                      },
                    },
                  ]}
                >
                  <Input placeholder="example.com" />
                </Form.Item>
                <Button
                  danger
                  onClick={() => {
                    const currentDomains = Array.isArray(form.getFieldValue('cfworker_domains'))
                      ? [...form.getFieldValue('cfworker_domains')]
                      : []
                    const removedDomain = String(currentDomains[field.name] || '').trim().toLowerCase().replace(/^@/, '')
                    remove(field.name)
                    if (!removedDomain) return
                    const enabledDomains = normalizeDomainList(form.getFieldValue('cfworker_enabled_domains'))
                    form.setFieldValue(
                      'cfworker_enabled_domains',
                      enabledDomains.filter((domain) => domain !== removedDomain),
                    )
                  }}
                >
                  删除
                </Button>
              </Space>
            ))}
            {fields.length === 0 ? (
              <Typography.Text type="secondary">还没有配置域名。添加后即可在下方选择启用项。</Typography.Text>
            ) : null}
            <Button type="dashed" onClick={() => add('')} icon={<PlusOutlined />} block>
              添加域名
            </Button>
          </div>
        )}
      </Form.List>

      <Form.Item name="cfworker_enabled_domains" hidden>
        <Select mode="multiple" options={normalizedDomains.map((domain) => ({ label: domain, value: domain }))} />
      </Form.Item>

      <div style={{ marginTop: 16 }}>
        <div style={{ marginBottom: 8, fontWeight: 500 }}>已启用域名</div>
        {enabledDomains.length > 0 ? (
          <Space wrap>
            {enabledDomains.map((domain) => (
              <Tag
                key={domain}
                color="blue"
                closable
                onClose={(event) => {
                  event.preventDefault()
                  updateEnabledDomains(enabledDomains.filter((item) => item !== domain))
                }}
              >
                {domain}
              </Tag>
            ))}
          </Space>
        ) : (
          <Typography.Text type="secondary">暂无启用域名，点击下方域名即可启用。</Typography.Text>
        )}
      </div>

      <div style={{ marginTop: 16 }}>
        <div style={{ marginBottom: 8, fontWeight: 500 }}>点击切换启用状态</div>
        {normalizedDomains.length > 0 ? (
          <Space wrap>
            {normalizedDomains.map((domain) => (
              <Tag.CheckableTag
                key={domain}
                checked={enabledDomains.includes(domain)}
                onChange={(checked) => toggleEnabledDomain(domain, checked)}
              >
                {domain}
              </Tag.CheckableTag>
            ))}
          </Space>
        ) : (
          <Typography.Text type="secondary">请先在上方添加域名。</Typography.Text>
        )}
      </div>
      <Typography.Text type="secondary" style={{ display: 'block', marginTop: 12 }}>
        仅已启用域名会参与注册；点击已启用标签可直接移除。
      </Typography.Text>
    </Card>
  )
}

function SolverStatus() {
  type LogViewMode = 'live' | 'static'
  const [solver, setSolver] = useState<any | null>(null)
  const [loading, setLoading] = useState(false)
  const [restarting, setRestarting] = useState(false)
  const [logViewMode, setLogViewMode] = useState<LogViewMode>('live')
  const [logModal, setLogModal] = useState({
    open: false,
    path: '',
    content: '',
    loading: false,
    truncated: false,
    exists: false,
  })
  const logContainerRef = useRef<HTMLPreElement>(null)

  const loadSolver = async (options?: { silent?: boolean }) => {
    const silent = Boolean(options?.silent)
    if (!silent) {
      setLoading(true)
    }
    try {
      const data = await apiFetch('/solver/status')
      setSolver(data)
    } catch {
      setSolver({
        enabled: true,
        running: false,
        process_alive: false,
        pid: null,
        url: '',
        bind_host: '',
        browser_type: '',
        log_path: '',
        last_error: '读取 Solver 状态失败',
      })
    } finally {
      if (!silent) {
        setLoading(false)
      }
    }
  }

  const loadLog = async (options?: { silent?: boolean }) => {
    const silent = Boolean(options?.silent)
    if (!silent) {
      setLogModal((current) => ({ ...current, loading: true }))
    }
    try {
      const data = await apiFetch('/solver/logs?lines=400')
      setLogModal((current) => ({
        ...current,
        path: data.log_path || current.path,
        content: data.content || '',
        loading: false,
        truncated: Boolean(data.truncated),
        exists: data.exists !== false,
      }))
    } catch (e: any) {
      setLogModal((current) => ({
        ...current,
        loading: false,
        content: e?.message || '读取日志失败',
        truncated: false,
        exists: false,
      }))
    }
  }

  const restartSolver = async () => {
    setRestarting(true)
    try {
      const data = await apiFetch('/solver/restart', { method: 'POST' })
      setSolver(data)
      message.success('Solver 重启指令已发送')
      window.setTimeout(() => {
        void loadSolver({ silent: true })
      }, 2000)
    } catch (e: any) {
      message.error(e?.message || '重启 Solver 失败')
    } finally {
      setRestarting(false)
    }
  }

  const openLogModal = async () => {
    setLogModal({
      open: true,
      path: solver?.log_path || '',
      content: '',
      loading: true,
      truncated: false,
      exists: true,
    })
    await loadLog()
  }

  const copyLogContent = async () => {
    try {
      await navigator.clipboard.writeText(logModal.content)
      message.success('日志已复制')
    } catch {
      message.error('复制失败')
    }
  }

  useEffect(() => {
    void loadSolver()
    const timer = window.setInterval(() => {
      void loadSolver({ silent: true })
    }, 5000)
    return () => window.clearInterval(timer)
  }, [])

  useEffect(() => {
    if (!logModal.open || logViewMode !== 'live') return
    const timer = window.setInterval(() => {
      void loadLog({ silent: true })
    }, 1000)
    return () => window.clearInterval(timer)
  }, [logModal.open, logViewMode])

  useEffect(() => {
    if (!logModal.open || logViewMode !== 'live') return
    const node = logContainerRef.current
    if (node) {
      node.scrollTop = node.scrollHeight
    }
  }, [logModal.content, logModal.open, logViewMode])

  const statusColor = solver?.running ? 'green' : solver?.process_alive ? 'gold' : 'default'
  const statusText = solver?.running ? '运行中' : solver?.process_alive ? '启动中' : loading ? '检测中' : '未运行'

  return (
    <>
      <Modal
        open={logModal.open}
        title="Turnstile Solver 日志"
        onCancel={() => setLogModal((current) => ({ ...current, open: false }))}
        onOk={() => setLogModal((current) => ({ ...current, open: false }))}
        width={900}
        okText="关闭"
        cancelButtonProps={{ style: { display: 'none' } }}
      >
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8, gap: 12, flexWrap: 'wrap' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
            <div style={{ fontSize: 12, color: '#7a8ba3' }}>
              {logModal.path || '暂无日志文件路径'}
              {logModal.truncated ? ' · 已截取最近日志' : ''}
            </div>
            <Segmented<LogViewMode>
              size="small"
              value={logViewMode}
              onChange={(value) => setLogViewMode(value)}
              options={[
                { label: '实时日志', value: 'live' },
                { label: '静态日志', value: 'static' },
              ]}
            />
          </div>
          <Space>
            <Button size="small" onClick={() => loadLog()} loading={logModal.loading}>
              刷新日志
            </Button>
            <Button size="small" onClick={copyLogContent} disabled={!logModal.content}>
              复制日志
            </Button>
          </Space>
        </div>
        <pre
          ref={logContainerRef}
          style={{
            margin: 0,
            maxHeight: 520,
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
          {logModal.loading && !logModal.content
            ? '日志加载中...'
            : logModal.content || (logModal.exists ? '日志文件暂无内容。' : '日志文件尚未生成。')}
        </pre>
        <div style={{ marginTop: 8, fontSize: 12, color: '#7a8ba3' }}>
          {logViewMode === 'live' ? '实时日志模式：每秒自动刷新一次。' : '静态日志模式：仅在点击“刷新日志”时更新。'}
        </div>
      </Modal>

      <Card
        title="Turnstile Solver"
        extra={<span style={{ fontSize: 12, color: '#7a8ba3' }}>本地验证码服务，供浏览器注册流程复用</span>}
        style={{ marginBottom: 16 }}
      >
        <Space direction="vertical" style={{ width: '100%' }}>
          <div>
            状态：
            <Tag color={statusColor} style={{ marginLeft: 8 }}>
              {statusText}
            </Tag>
            <Tag color={solver?.enabled === false ? 'red' : 'blue'} style={{ marginLeft: 8 }}>
              {solver?.enabled === false ? '已禁用' : '已启用'}
            </Tag>
            {solver?.pid ? <span style={{ marginLeft: 8 }}>PID: {solver.pid}</span> : null}
          </div>
          {solver?.url ? <div>地址：<Typography.Text copyable>{solver.url}</Typography.Text></div> : null}
          <div>浏览器：{solver?.browser_type || '未配置'}{solver?.bind_host ? ` · 监听 ${solver.bind_host}` : ''}</div>
          <div>日志：<Typography.Text copyable>{solver?.log_path || '暂无日志路径'}</Typography.Text></div>
          {solver?.last_error ? <div style={{ color: '#ef4444' }}>最近错误：{solver.last_error}</div> : null}
          <Space wrap>
            <Button onClick={openLogModal}>
              查看日志
            </Button>
            <Button loading={restarting} onClick={restartSolver}>
              重启
            </Button>
            <Button loading={loading} onClick={() => loadSolver()}>
              刷新状态
            </Button>
          </Space>
        </Space>
      </Card>
    </>
  )
}

function ApplicationLogPanel() {
  type LogViewMode = 'live' | 'static'
  const [logViewMode, setLogViewMode] = useState<LogViewMode>('live')
  const [logModal, setLogModal] = useState({
    open: false,
    path: '',
    content: '',
    loading: false,
    truncated: false,
    exists: false,
  })
  const logContainerRef = useRef<HTMLPreElement>(null)

  const loadLog = async (options?: { silent?: boolean }) => {
    const silent = Boolean(options?.silent)
    if (!silent) {
      setLogModal((current) => ({ ...current, loading: true }))
    }
    try {
      const data = await apiFetch('/runtime/logs?lines=400')
      setLogModal((current) => ({
        ...current,
        path: data.path || data.log_path || current.path,
        content: data.content || '',
        loading: false,
        truncated: Boolean(data.truncated),
        exists: data.exists !== false,
      }))
    } catch (e: any) {
      setLogModal((current) => ({
        ...current,
        loading: false,
        content: e?.message || '读取后端应用日志失败',
        truncated: false,
        exists: false,
      }))
    }
  }

  const openLogModal = async () => {
    setLogModal({
      open: true,
      path: '',
      content: '',
      loading: true,
      truncated: false,
      exists: true,
    })
    await loadLog()
  }

  const copyLogContent = async () => {
    try {
      await navigator.clipboard.writeText(logModal.content)
      message.success('日志已复制')
    } catch {
      message.error('复制失败')
    }
  }

  useEffect(() => {
    if (!logModal.open || logViewMode !== 'live') return
    const timer = window.setInterval(() => {
      void loadLog({ silent: true })
    }, 1000)
    return () => window.clearInterval(timer)
  }, [logModal.open, logViewMode])

  useEffect(() => {
    if (!logModal.open || logViewMode !== 'live') return
    const node = logContainerRef.current
    if (node) {
      node.scrollTop = node.scrollHeight
    }
  }, [logModal.content, logModal.open, logViewMode])

  return (
    <>
      <Modal
        open={logModal.open}
        title="后端应用日志"
        onCancel={() => setLogModal((current) => ({ ...current, open: false }))}
        onOk={() => setLogModal((current) => ({ ...current, open: false }))}
        width={900}
        okText="关闭"
        cancelButtonProps={{ style: { display: 'none' } }}
      >
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8, gap: 12, flexWrap: 'wrap' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
            <div style={{ fontSize: 12, color: '#7a8ba3' }}>
              {logModal.path || '暂无日志文件路径'}
              {logModal.truncated ? ' · 已截取最近日志' : ''}
            </div>
            <Segmented<LogViewMode>
              size="small"
              value={logViewMode}
              onChange={(value) => setLogViewMode(value)}
              options={[
                { label: '实时日志', value: 'live' },
                { label: '静态日志', value: 'static' },
              ]}
            />
          </div>
          <Space>
            <Button size="small" onClick={() => loadLog()} loading={logModal.loading}>
              刷新日志
            </Button>
            <Button size="small" onClick={copyLogContent} disabled={!logModal.content}>
              复制日志
            </Button>
          </Space>
        </div>
        <pre
          ref={logContainerRef}
          style={{
            margin: 0,
            maxHeight: 520,
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
          {logModal.loading && !logModal.content
            ? '日志加载中...'
            : logModal.content || (logModal.exists ? '日志文件暂无内容。' : '日志文件尚未生成。')}
        </pre>
        <div style={{ marginTop: 8, fontSize: 12, color: '#7a8ba3' }}>
          {logViewMode === 'live' ? '实时日志模式：每秒自动刷新一次。' : '静态日志模式：仅在点击“刷新日志”时更新。'}
        </div>
      </Modal>

      <Card
        title="后端应用日志"
        extra={<span style={{ fontSize: 12, color: '#7a8ba3' }}>记录任务执行、接口异常和运行时信息</span>}
        style={{ marginBottom: 16 }}
      >
        <Space direction="vertical" style={{ width: '100%' }}>
          <div>范围：当前 FastAPI 后端进程日志</div>
          <div>模式：自动滚动文件，支持实时预览</div>
          <Space wrap>
            <Button onClick={openLogModal}>
              查看日志
            </Button>
          </Space>
        </Space>
      </Card>
    </>
  )
}

function RequestLogPanel({ form }: { form: FormInstance }) {
  type LogViewMode = 'live' | 'static'
  const loggingEnabled = String(Form.useWatch('request_logging_enabled', form) || '0')
  const [logViewMode, setLogViewMode] = useState<LogViewMode>('live')
  const [logModal, setLogModal] = useState({
    open: false,
    path: '',
    content: '',
    loading: false,
    truncated: false,
    exists: false,
  })
  const logContainerRef = useRef<HTMLPreElement>(null)

  const loadLog = async (options?: { silent?: boolean }) => {
    const silent = Boolean(options?.silent)
    if (!silent) {
      setLogModal((current) => ({ ...current, loading: true }))
    }
    try {
      const data = await apiFetch('/request/logs?lines=400')
      setLogModal((current) => ({
        ...current,
        path: data.path || data.log_path || current.path,
        content: data.content || '',
        loading: false,
        truncated: Boolean(data.truncated),
        exists: data.exists !== false,
      }))
    } catch (e: any) {
      setLogModal((current) => ({
        ...current,
        loading: false,
        content: e?.message || '读取接口请求日志失败',
        truncated: false,
        exists: false,
      }))
    }
  }

  const openLogModal = async () => {
    setLogModal({
      open: true,
      path: '',
      content: '',
      loading: true,
      truncated: false,
      exists: true,
    })
    await loadLog()
  }

  const copyLogContent = async () => {
    try {
      await navigator.clipboard.writeText(logModal.content)
      message.success('日志已复制')
    } catch {
      message.error('复制失败')
    }
  }

  useEffect(() => {
    if (!logModal.open || logViewMode !== 'live') return
    const timer = window.setInterval(() => {
      void loadLog({ silent: true })
    }, 1000)
    return () => window.clearInterval(timer)
  }, [logModal.open, logViewMode])

  useEffect(() => {
    if (!logModal.open || logViewMode !== 'live') return
    const node = logContainerRef.current
    if (node) {
      node.scrollTop = node.scrollHeight
    }
  }, [logModal.content, logModal.open, logViewMode])

  return (
    <>
      <Modal
        open={logModal.open}
        title="接口请求日志"
        onCancel={() => setLogModal((current) => ({ ...current, open: false }))}
        onOk={() => setLogModal((current) => ({ ...current, open: false }))}
        width={900}
        okText="关闭"
        cancelButtonProps={{ style: { display: 'none' } }}
      >
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8, gap: 12, flexWrap: 'wrap' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
            <div style={{ fontSize: 12, color: '#7a8ba3' }}>
              {logModal.path || '暂无日志文件路径'}
              {logModal.truncated ? ' · 已截取最近日志' : ''}
            </div>
            <Segmented<LogViewMode>
              size="small"
              value={logViewMode}
              onChange={(value) => setLogViewMode(value)}
              options={[
                { label: '实时日志', value: 'live' },
                { label: '静态日志', value: 'static' },
              ]}
            />
          </div>
          <Space>
            <Button size="small" onClick={() => loadLog()} loading={logModal.loading}>
              刷新日志
            </Button>
            <Button size="small" onClick={copyLogContent} disabled={!logModal.content}>
              复制日志
            </Button>
          </Space>
        </div>
        <pre
          ref={logContainerRef}
          style={{
            margin: 0,
            maxHeight: 520,
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
          {logModal.loading && !logModal.content
            ? '日志加载中...'
            : logModal.content || (logModal.exists ? '日志文件暂无内容。' : '日志文件尚未生成。')}
        </pre>
        <div style={{ marginTop: 8, fontSize: 12, color: '#7a8ba3' }}>
          {logViewMode === 'live' ? '实时日志模式：每秒自动刷新一次。' : '静态日志模式：仅在点击“刷新日志”时更新。'}
        </div>
      </Modal>

      <Card
        title="接口请求日志"
        extra={<span style={{ fontSize: 12, color: '#7a8ba3' }}>记录进入后端的 API 请求、请求体和响应体，敏感字段会自动脱敏</span>}
        style={{ marginBottom: 16 }}
      >
        <Space direction="vertical" style={{ width: '100%' }}>
          <ConfigField field={{ key: 'request_logging_enabled', label: '日志开关', type: 'select' }} />
          <div>范围：全局 HTTP API 请求，日志查看接口本身不会重复写入日志。</div>
          <div>当前状态：{loggingEnabled === '1' ? '已开启' : '已关闭'}</div>
          <div>说明：请求体和响应体会按内容类型做脱敏与截断，避免日志泄露密钥或被超大响应刷满。</div>
          <Space wrap>
            <Button onClick={openLogModal}>
              查看日志
            </Button>
          </Space>
        </Space>
      </Card>
    </>
  )
}

function IntegrationsPanel() {
  type LogViewMode = 'live' | 'static'
  const [items, setItems] = useState<any[]>([])
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState('')
  const [logViewMode, setLogViewMode] = useState<LogViewMode>('live')
  const [resultModal, setResultModal] = useState({
    open: false,
    title: '',
    ok: true,
    content: '',
  })
  const [logModal, setLogModal] = useState({
    open: false,
    name: '',
    title: '',
    path: '',
    content: '',
    loading: false,
    truncated: false,
    exists: false,
  })
  const logContainerRef = useRef<HTMLPreElement>(null)

  const showResultModal = (title: string, data: unknown, ok = true) => {
    setResultModal({
      open: true,
      title,
      ok,
      content: formatResultText(data),
    })
  }

  const loadLog = async (serviceName: string, options?: { silent?: boolean }) => {
    const silent = Boolean(options?.silent)
    if (!silent) {
      setLogModal((current) => {
        if (!current.open || current.name !== serviceName) return current
        return {
          ...current,
          loading: true,
        }
      })
    }
    try {
      const data = await apiFetch(`/integrations/services/${serviceName}/logs?lines=400`)
      setLogModal((current) => {
        if (!current.open || current.name !== serviceName) return current
        return {
          ...current,
          path: data.log_path || current.path,
          content: data.content || '',
          loading: false,
          truncated: Boolean(data.truncated),
          exists: data.exists !== false,
        }
      })
    } catch (e: any) {
      setLogModal((current) => {
        if (!current.open || current.name !== serviceName) return current
        return {
          ...current,
          loading: false,
          content: e?.message || '读取日志失败',
          truncated: false,
          exists: false,
        }
      })
    }
  }

  const load = async () => {
    setLoading(true)
    try {
      const d = await apiFetch('/integrations/services')
      setItems(d.items || [])
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
    const timer = window.setInterval(load, 5000)
    return () => window.clearInterval(timer)
  }, [])

  useEffect(() => {
    if (!logModal.open || !logModal.name) return
    loadLog(logModal.name)
  }, [logModal.open, logModal.name])

  useEffect(() => {
    if (!logModal.open || !logModal.name || logViewMode !== 'live') return
    const timer = window.setInterval(() => {
      loadLog(logModal.name, { silent: true })
    }, 1000)
    return () => window.clearInterval(timer)
  }, [logModal.open, logModal.name, logViewMode])

  useEffect(() => {
    if (!logModal.open || logViewMode !== 'live') return
    const node = logContainerRef.current
    if (node) {
      node.scrollTop = node.scrollHeight
    }
  }, [logModal.content, logModal.open, logViewMode])

  const doAction = async (key: string, request: Promise<any>) => {
    setBusy(key)
    try {
      const result = await request
      await load()
      message.success('操作完成')
      showResultModal('操作结果', result, true)
    } catch (e: any) {
      message.error(e?.message || '操作失败')
      showResultModal('操作结果', e?.message || e || '操作失败', false)
      await load()
    } finally {
      setBusy('')
    }
  }

  const backfill = async (platforms: string[], label: string, busyKey: string) => {
    setBusy(busyKey)
    try {
      const d = await apiFetch('/integrations/backfill', {
        method: 'POST',
        body: JSON.stringify({ platforms }),
      })
      message.success(`${label} 回填完成：成功 ${d.success} / ${d.total}`)
      showResultModal(`${label} 回填结果`, d, true)
    } catch (e: any) {
      message.error(e?.message || `${label} 回填失败`)
      showResultModal(`${label} 回填结果`, e?.message || e || `${label} 回填失败`, false)
    } finally {
      setBusy('')
    }
  }

  const openLogModal = (item: any) => {
    setLogModal({
      open: true,
      name: item.name,
      title: `${item.label} 日志`,
      path: item.log_path || '',
      content: '',
      loading: true,
      truncated: false,
      exists: true,
    })
  }

  const copyLogContent = async () => {
    try {
      await navigator.clipboard.writeText(logModal.content)
      message.success('日志已复制')
    } catch {
      message.error('复制失败')
    }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <Modal
        open={resultModal.open}
        title={resultModal.title}
        onCancel={() => setResultModal((v) => ({ ...v, open: false }))}
        onOk={() => setResultModal((v) => ({ ...v, open: false }))}
        width={760}
      >
        <Typography.Paragraph style={{ marginBottom: 8, color: resultModal.ok ? '#10b981' : '#ef4444' }}>
          {resultModal.ok ? '操作已完成。' : '操作失败。'}
        </Typography.Paragraph>
        <pre
          style={{
            margin: 0,
            maxHeight: 420,
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
          {resultModal.content}
        </pre>
      </Modal>

      <Modal
        open={logModal.open}
        title={logModal.title}
        onCancel={() => setLogModal((current) => ({ ...current, open: false }))}
        onOk={() => setLogModal((current) => ({ ...current, open: false }))}
        width={900}
        okText="关闭"
        cancelButtonProps={{ style: { display: 'none' } }}
      >
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8, gap: 12, flexWrap: 'wrap' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
            <div style={{ fontSize: 12, color: '#7a8ba3' }}>
              {logModal.path || '暂无日志文件路径'}
              {logModal.truncated ? ' · 已截取最近日志' : ''}
            </div>
            <Segmented<LogViewMode>
              size="small"
              value={logViewMode}
              onChange={(value) => setLogViewMode(value)}
              options={[
                { label: '实时日志', value: 'live' },
                { label: '静态日志', value: 'static' },
              ]}
            />
          </div>
          <Space>
            <Button size="small" onClick={() => loadLog(logModal.name)} loading={logModal.loading} disabled={!logModal.name}>
              刷新日志
            </Button>
            <Button size="small" onClick={copyLogContent} disabled={!logModal.content}>
              复制日志
            </Button>
          </Space>
        </div>
        <pre
          ref={logContainerRef}
          style={{
            margin: 0,
            maxHeight: 520,
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
          {logModal.loading && !logModal.content
            ? '日志加载中...'
            : logModal.content || (logModal.exists ? '日志文件暂无内容。' : '日志文件尚未生成。')}
        </pre>
        <div style={{ marginTop: 8, fontSize: 12, color: '#7a8ba3' }}>
          {logViewMode === 'live' ? '实时日志模式：每秒自动刷新一次。' : '静态日志模式：仅在点击“刷新日志”时更新。'}
        </div>
      </Modal>

      <Card title="批量操作">
        <Space wrap>
          <Button loading={busy === 'start-all'} onClick={() => doAction('start-all', apiFetch('/integrations/services/start-all', { method: 'POST' }))}>
            启动全部（已安装）
          </Button>
          <Button loading={busy === 'stop-all'} onClick={() => doAction('stop-all', apiFetch('/integrations/services/stop-all', { method: 'POST' }))}>
            停止全部
          </Button>
          <Button loading={loading} onClick={load}>
            刷新状态
          </Button>
        </Space>
      </Card>

      {items.map((item) => (
        <Card key={item.name} title={item.label}>
          <Space direction="vertical" style={{ width: '100%' }}>
            <div>
              状态：
              <Tag color={item.running ? 'green' : item.starting || item.process_alive ? 'gold' : 'default'} style={{ marginLeft: 8 }}>
                {item.running ? '运行中' : item.starting || item.process_alive ? '启动中' : '未运行'}
              </Tag>
              <Tag color={item.repo_exists ? 'blue' : 'orange'} style={{ marginLeft: 8 }}>
                {item.repo_exists ? '已安装' : '未安装'}
              </Tag>
              {item.pid ? <span style={{ marginLeft: 8 }}>PID: {item.pid}</span> : null}
            </div>
            <div>插件目录：<Typography.Text copyable>{item.repo_path}</Typography.Text></div>
            {item.url ? <div>地址：<Typography.Text copyable>{item.url}</Typography.Text></div> : null}
            {item.management_url ? <div>管理页：<Typography.Text copyable>{item.management_url}</Typography.Text></div> : null}
            {item.management_key ? <div>登录口令：<Typography.Text copyable>{item.management_key}</Typography.Text></div> : null}
            <div>日志：<Typography.Text copyable>{item.log_path}</Typography.Text></div>
            {item.last_error ? <div style={{ color: '#ef4444' }}>最近错误：{item.last_error}</div> : null}
            <Space wrap>
              {item.management_url ? (
                <Button onClick={() => window.open(item.management_url, '_blank')}>
                  打开管理页
                </Button>
              ) : null}
              <Button onClick={() => openLogModal(item)}>
                查看日志
              </Button>
              {!item.repo_exists ? (
                <Button
                  type="primary"
                  loading={busy === `install-${item.name}`}
                  onClick={() => doAction(`install-${item.name}`, apiFetch(`/integrations/services/${item.name}/install`, { method: 'POST' }))}
                >
                  安装
                </Button>
              ) : null}
              <Button
                loading={busy === `start-${item.name}`}
                disabled={!item.repo_exists || item.running || item.starting || item.process_alive}
                onClick={() => doAction(`start-${item.name}`, apiFetch(`/integrations/services/${item.name}/start`, { method: 'POST' }))}
              >
                {item.running ? '已运行' : item.starting || item.process_alive ? '启动中' : '启动'}
              </Button>
              <Button
                loading={busy === `stop-${item.name}`}
                onClick={() => doAction(`stop-${item.name}`, apiFetch(`/integrations/services/${item.name}/stop`, { method: 'POST' }))}
              >
                停止
              </Button>
              {item.name === 'grok2api' ? (
                <Button
                  loading={busy === 'backfill-grok'}
                  onClick={() => backfill(['grok'], 'Grok', 'backfill-grok')}
                >
                  回填现有 Grok 账号
                </Button>
              ) : null}
              {item.name === 'kiro-manager' ? (
                <Button
                  loading={busy === 'backfill-kiro'}
                  onClick={() => backfill(['kiro'], 'Kiro', 'backfill-kiro')}
                >
                  回填现有 Kiro 账号
                </Button>
              ) : null}
            </Space>
          </Space>
        </Card>
      ))}
    </div>
  )
}

export default function Settings() {
  const [form] = Form.useForm()
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [activeTab, setActiveTab] = useState(() => {
    if (typeof window === 'undefined') return 'register'
    const stored = window.localStorage.getItem(SETTINGS_ACTIVE_TAB_STORAGE_KEY) || 'register'
    return TAB_ITEMS.some((item) => item.key === stored) ? stored : 'register'
  })
  const [loadedConfig, setLoadedConfig] = useState<Record<string, unknown>>({})

  useEffect(() => {
    if (typeof window === 'undefined') return
    window.localStorage.setItem(SETTINGS_ACTIVE_TAB_STORAGE_KEY, activeTab)
  }, [activeTab])

  useEffect(() => {
    apiFetch('/config').then((data) => {
      if (!data.maliapi_base_url) {
        data.maliapi_base_url = 'https://maliapi.215.im/v1'
      }
      if (!data.luckmail_base_url) {
        data.luckmail_base_url = 'https://mails.luckyous.com/'
      }
      if (!data.mail_provider) {
        data.mail_provider = 'moemail'
      }
      if (!data.request_logging_enabled) {
        data.request_logging_enabled = '0'
      }
      data.mailbox_services_enabled = parseStoredSelectionList(data.mailbox_services_enabled, MAILBOX_SERVICE_KEYS)
      if ((data.mailbox_services_enabled as string[]).length === 0) {
        data.mailbox_services_enabled = [String(data.mail_provider || 'moemail')]
      }
      data.chatgpt_modules_enabled = parseStoredSelectionList(data.chatgpt_modules_enabled, CHATGPT_MODULE_KEYS)
      if ((data.chatgpt_modules_enabled as string[]).length === 0) {
        data.chatgpt_modules_enabled = [...CHATGPT_MODULE_KEYS]
      }
      data.cfworker_domains = parseStoredDomainList(data.cfworker_domains)
      data.cfworker_enabled_domains = parseStoredDomainList(data.cfworker_enabled_domains)
      setLoadedConfig(data)
      form.setFieldsValue(data)
    })
  }, [form])

  const save = async () => {
    setSaving(true)
    try {
      const formValues = form.getFieldsValue(true)
      const nextConfig = { ...loadedConfig, ...formValues }
      const mailboxServicesEnabled = normalizeSelectionList(nextConfig.mailbox_services_enabled, MAILBOX_SERVICE_KEYS)
      const chatgptModulesEnabled = normalizeSelectionList(nextConfig.chatgpt_modules_enabled, CHATGPT_MODULE_KEYS)
      const domains = normalizeDomainList(nextConfig.cfworker_domains)
      const enabledDomains = normalizeDomainList(nextConfig.cfworker_enabled_domains).filter((domain) => domains.includes(domain))

      if (mailboxServicesEnabled.length === 0) {
        setActiveTab('mailbox')
        message.error('邮箱服务至少需要启用一个')
        return
      }

      if (chatgptModulesEnabled.length === 0) {
        setActiveTab('chatgpt')
        message.error('ChatGPT 模块至少需要启用一个')
        return
      }

      const nextMailProvider = mailboxServicesEnabled.includes(String(nextConfig.mail_provider || ''))
        ? String(nextConfig.mail_provider || '')
        : mailboxServicesEnabled[0]

      if (domains.length > 0 && enabledDomains.length === 0) {
        setActiveTab('mailbox')
        message.error('CF Worker 至少需要启用一个域名')
        return
      }

      const normalizedConfig = {
        ...nextConfig,
        mail_provider: nextMailProvider,
        mailbox_services_enabled: mailboxServicesEnabled,
        chatgpt_modules_enabled: chatgptModulesEnabled,
        cfworker_domains: domains,
        cfworker_enabled_domains: enabledDomains,
      }

      if (domains.length > 0) {
        normalizedConfig.cfworker_domain = ''
      }

      await apiFetch('/config', {
        method: 'PUT',
        body: JSON.stringify({
          data: {
            ...normalizedConfig,
            mailbox_services_enabled: JSON.stringify(mailboxServicesEnabled),
            chatgpt_modules_enabled: JSON.stringify(chatgptModulesEnabled),
            cfworker_domains: JSON.stringify(domains),
            cfworker_enabled_domains: JSON.stringify(enabledDomains),
          },
        }),
      })

      setLoadedConfig(normalizedConfig)
      form.setFieldsValue({
        ...normalizedConfig,
        mail_provider: nextMailProvider,
        mailbox_services_enabled: mailboxServicesEnabled,
        chatgpt_modules_enabled: chatgptModulesEnabled,
        cfworker_domains: domains,
        cfworker_enabled_domains: enabledDomains,
        cfworker_domain: domains.length > 0 ? '' : normalizedConfig.cfworker_domain,
      })
      message.success('保存成功')
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
    } finally {
      setSaving(false)
    }
  }

  const currentTab = TAB_ITEMS.find((t) => t.key === activeTab) as TabConfig

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div>
        <h1 style={{ fontSize: 24, fontWeight: 'bold', margin: 0 }}>全局配置</h1>
        <p style={{ color: '#7a8ba3', marginTop: 4 }}>配置将持久化保存，注册任务自动使用</p>
      </div>

      <div style={{ display: 'flex', gap: 24 }}>
        <div style={{ width: 200 }}>
          <Tabs
            tabPosition="left"
            activeKey={activeTab}
            onChange={setActiveTab}
            items={TAB_ITEMS.map((t) => ({
              key: t.key,
              label: (
                <span>
                  {t.icon}
                  <span style={{ marginLeft: 8 }}>{t.label}</span>
                </span>
              ),
            }))}
          />
        </div>

        <div style={{ flex: 1 }}>
          {activeTab === 'integrations' ? (
            <IntegrationsPanel />
          ) : activeTab === 'logs' ? (
            <Form form={form} layout="vertical">
              <RequestLogPanel form={form} />
              <ApplicationLogPanel />
              <SolverStatus />
              <Button type="primary" icon={<SaveOutlined />} onClick={save} loading={saving} block>
                {saved ? '已保存 ✓' : '保存配置'}
              </Button>
            </Form>
          ) : activeTab === 'mailbox' ? (
            <Form form={form} layout="vertical">
              <MailboxSettingsPanel form={form} />
              <Button type="primary" icon={<SaveOutlined />} onClick={save} loading={saving} block>
                {saved ? '已保存 ✓' : '保存配置'}
              </Button>
            </Form>
          ) : activeTab === 'chatgpt' ? (
            <Form form={form} layout="vertical">
              <ChatGptSettingsPanel sections={currentTab.sections} form={form} />
              <Button type="primary" icon={<SaveOutlined />} onClick={save} loading={saving} block>
                {saved ? '已保存 ✓' : '保存配置'}
              </Button>
            </Form>
          ) : (
            <Form form={form} layout="vertical">
              {currentTab.sections.map((section) => (
                <ConfigSection key={section.key || section.title} section={section} />
              ))}
              <Button type="primary" icon={<SaveOutlined />} onClick={save} loading={saving} block>
                {saved ? '已保存 ✓' : '保存配置'}
              </Button>
            </Form>
          )}
        </div>
      </div>
    </div>
  )
}
