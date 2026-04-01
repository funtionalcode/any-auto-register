import { useState } from 'react'
import { Alert, Button, Card, Form, Input, Space, Typography, message } from 'antd'
import { LockOutlined, UserOutlined } from '@ant-design/icons'
import { useAuth } from '@/components/AuthProvider'

const { Title, Paragraph, Text } = Typography

export default function AuthPortal() {
  const { bootstrapped, login, bootstrap } = useAuth()
  const [submitting, setSubmitting] = useState(false)

  const handleFinish = async (values: { username: string; password: string; confirmPassword?: string }) => {
    if (!bootstrapped && values.password !== values.confirmPassword) {
      message.error('两次输入的密码不一致')
      return
    }

    setSubmitting(true)
    try {
      if (bootstrapped) {
        await login(values.username, values.password)
      } else {
        await bootstrap(values.username, values.password)
      }
      message.success(bootstrapped ? '登录成功' : '管理员初始化成功')
    } catch (error: any) {
      message.error(error?.message || (bootstrapped ? '登录失败' : '初始化失败'))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="auth-portal">
      <div className="auth-portal__bg auth-portal__bg--one" />
      <div className="auth-portal__bg auth-portal__bg--two" />
      <Card className="auth-portal__card" bordered={false}>
        <Space direction="vertical" size={20} style={{ width: '100%' }}>
          <div>
            <Text className="auth-portal__eyebrow">Access Control</Text>
            <Title level={2} style={{ marginTop: 8, marginBottom: 8 }}>
              {bootstrapped ? '登录控制台' : '初始化首个管理员'}
            </Title>
            <Paragraph style={{ marginBottom: 0, color: 'rgba(15,23,42,0.72)' }}>
              {bootstrapped
                ? '后台已经启用用户/角色鉴权，请使用管理员或已分配账号登录。'
                : '系统尚未 bootstrap，需要先创建首个管理员账号，之后才能创建 SK Key 并对外提供 OpenAI 协议接口。'}
            </Paragraph>
          </div>

          <Alert
            type={bootstrapped ? 'info' : 'warning'}
            showIcon
            message={bootstrapped ? '登录后可管理用户、SK Key、代理绑定与配额。' : '初始化完成后会自动进入后台。'}
          />

          <Form layout="vertical" onFinish={handleFinish} autoComplete="off">
            <Form.Item
              name="username"
              label="用户名"
              rules={[{ required: true, message: '请输入用户名' }]}
            >
              <Input prefix={<UserOutlined />} placeholder="admin" />
            </Form.Item>
            <Form.Item
              name="password"
              label="密码"
              rules={[{ required: true, message: '请输入密码' }]}
            >
              <Input.Password prefix={<LockOutlined />} placeholder="请输入密码" />
            </Form.Item>
            {!bootstrapped ? (
              <Form.Item
                name="confirmPassword"
                label="确认密码"
                rules={[{ required: true, message: '请再次输入密码' }]}
              >
                <Input.Password prefix={<LockOutlined />} placeholder="再次输入密码" />
              </Form.Item>
            ) : null}
            <Button type="primary" htmlType="submit" loading={submitting} block size="large">
              {bootstrapped ? '登录' : '初始化管理员'}
            </Button>
          </Form>
        </Space>
      </Card>
    </div>
  )
}
