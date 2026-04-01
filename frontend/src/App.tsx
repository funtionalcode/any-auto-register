import { BrowserRouter, Routes, Route, useLocation, useNavigate } from 'react-router-dom'
import { useState, useEffect } from 'react'
import { ConfigProvider, Layout, Menu, Button } from 'antd'
import {
  DashboardOutlined,
  UserOutlined,
  GlobalOutlined,
  HistoryOutlined,
  SettingOutlined,
  SafetyCertificateOutlined,
  SunOutlined,
  MoonOutlined,
  LogoutOutlined,
} from '@ant-design/icons'
import zhCN from 'antd/locale/zh_CN'
import Dashboard from '@/pages/Dashboard'
import Accounts from '@/pages/Accounts'
import ChatGPTConversation from '@/pages/ChatGPTConversation'
import Register from '@/pages/Register'
import Proxies from '@/pages/Proxies'
import Settings from '@/pages/Settings'
import TaskHistory from '@/pages/TaskHistory'
import AccessControl from '@/pages/AccessControl'
import AuthPortal from '@/pages/AuthPortal'
import { darkTheme, lightTheme } from './theme'
import { RegisterTaskCenterProvider } from '@/components/RegisterTaskCenter'
import { AuthProvider, useAuth } from '@/components/AuthProvider'

const { Sider, Content } = Layout

function AppContent() {
  const { loading, user, logout } = useAuth()
  const [themeMode, setThemeMode] = useState<'dark' | 'light'>(() =>
    (localStorage.getItem('theme') as 'dark' | 'light') || 'dark'
  )
  const [collapsed, setCollapsed] = useState(false)
  const [platforms, setPlatforms] = useState<{ key: string; label: string }[]>([])
  const location = useLocation()
  const navigate = useNavigate()

  useEffect(() => {
    document.documentElement.classList.toggle('light', themeMode === 'light')
    document.documentElement.style.setProperty(
      '--sider-trigger-border',
      themeMode === 'light' ? 'rgba(0,0,0,0.1)' : 'rgba(255,255,255,0.15)'
    )
    localStorage.setItem('theme', themeMode)
  }, [themeMode])

  useEffect(() => {
    if (!user) return
    fetch('/api/platforms')
      .then(r => r.json())
      .then(d => setPlatforms((d || [])
        .filter((p: any) => !['tavily', 'cursor'].includes(p.name))
        .map((p: any) => ({ key: p.name, label: p.display_name }))))
  }, [user])

  const isLight = themeMode === 'light'
  const currentTheme = isLight ? lightTheme : darkTheme

  if (loading) {
    return (
      <ConfigProvider theme={currentTheme} locale={zhCN}>
        <div style={{ minHeight: '100vh', display: 'grid', placeItems: 'center' }}>Loading...</div>
      </ConfigProvider>
    )
  }

  if (!user) {
    return (
      <ConfigProvider theme={currentTheme} locale={zhCN}>
        <AuthPortal />
      </ConfigProvider>
    )
  }

  const getSelectedKey = () => {
    const path = location.pathname
    if (path === '/') return ['/']
    if (path === '/accounts') return ['/accounts']
    if (path.startsWith('/accounts/')) {
      const parts = path.split('/').filter(Boolean)
      if (parts.length >= 2) {
        return [`/accounts/${parts[1]}`]
      }
      return ['/accounts']
    }
    if (path === '/history') return ['/history']
    if (path === '/proxies') return ['/proxies']
    if (path === '/settings') return ['/settings']
    if (path === '/access') return ['/access']
    return ['/']
  }

  const menuItems = [
    {
      key: '/',
      icon: <DashboardOutlined />,
      label: '仪表盘',
    },
    {
      key: '/accounts',
      icon: <UserOutlined />,
      label: '平台管理',
      children: platforms.map(p => ({
        key: `/accounts/${p.key}`,
        label: p.label,
      })),
    },
    {
      key: '/history',
      icon: <HistoryOutlined />,
      label: '任务历史',
    },
    {
      key: '/proxies',
      icon: <GlobalOutlined />,
      label: '代理管理',
    },
    {
      key: '/settings',
      icon: <SettingOutlined />,
      label: '全局配置',
    },
    {
      key: '/access',
      icon: <SafetyCertificateOutlined />,
      label: '访问控制',
    },
  ]

  return (
    <ConfigProvider theme={currentTheme} locale={zhCN}>
      <RegisterTaskCenterProvider>
        <Layout style={{ minHeight: '100vh' }}>
          <Sider
            collapsible
            collapsed={collapsed}
            onCollapse={setCollapsed}
            style={{
              background: currentTheme.token?.colorBgContainer,
              borderRight: `1px solid ${currentTheme.token?.colorBorder}`,
            }}
            width={220}
          >
            <div
              style={{
                height: 64,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                borderBottom: `1px solid ${currentTheme.token?.colorBorder}`,
              }}
            >
              <DashboardOutlined style={{ fontSize: 20, color: currentTheme.token?.colorPrimary }} />
              {!collapsed && (
                <span
                  style={{
                    marginLeft: 8,
                    fontWeight: 600,
                    fontSize: 14,
                    color: currentTheme.token?.colorText,
                  }}
                >
                  Account Manager
                </span>
              )}
            </div>
            <Menu
              mode="inline"
              selectedKeys={getSelectedKey()}
              defaultOpenKeys={['/accounts']}
              items={menuItems}
              onClick={({ key }) => navigate(key)}
              style={{
                borderRight: 0,
                background: 'transparent',
              }}
            />
            <div
              style={{
                position: 'absolute',
                bottom: 16,
                left: 0,
                right: 0,
                padding: '0 16px',
              }}
            >
              <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                {!collapsed ? (
                  <div
                    style={{
                      padding: '10px 12px',
                      borderRadius: 12,
                      border: `1px solid ${currentTheme.token?.colorBorder}`,
                      background: currentTheme.token?.colorBgLayout,
                    }}
                  >
                    <div style={{ fontWeight: 600, fontSize: 13 }}>{user.username}</div>
                    <div style={{ fontSize: 12, opacity: 0.72 }}>{user.role}</div>
                  </div>
                ) : null}
                <Button
                  block
                  icon={isLight ? <SunOutlined /> : <MoonOutlined />}
                  onClick={() => setThemeMode(isLight ? 'dark' : 'light')}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: collapsed ? 'center' : 'space-between',
                  }}
                >
                  {!collapsed && (isLight ? '亮色模式' : '暗色模式')}
                </Button>
                <Button
                  block
                  icon={<LogoutOutlined />}
                  onClick={logout}
                  danger
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: collapsed ? 'center' : 'space-between',
                  }}
                >
                  {!collapsed && '退出登录'}
                </Button>
              </div>
            </div>
          </Sider>
          <Content
            style={{
              padding: 24,
              overflow: 'auto',
              background: currentTheme.token?.colorBgLayout,
            }}
          >
            <Routes>
              <Route path="/" element={<Dashboard />} />
              <Route path="/accounts" element={<Accounts />} />
              <Route path="/accounts/chatgpt/:accountId/conversation" element={<ChatGPTConversation />} />
              <Route path="/accounts/:platform" element={<Accounts />} />
              <Route path="/register" element={<Register />} />
              <Route path="/history" element={<TaskHistory />} />
              <Route path="/proxies" element={<Proxies />} />
              <Route path="/settings" element={<Settings />} />
              <Route path="/access" element={<AccessControl />} />
            </Routes>
          </Content>
        </Layout>
      </RegisterTaskCenterProvider>
    </ConfigProvider>
  )
}

export default function App() {
  return (
    <AuthProvider>
      <BrowserRouter>
        <AppContent />
      </BrowserRouter>
    </AuthProvider>
  )
}
