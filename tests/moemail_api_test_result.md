# MoeMail API 测试结果

## 测试时间
2026-03-31

## 测试环境
- Base URL: https://sall.cc
- Python: 3.x
- 请求库：requests

---

## 测试 1: 完整注册流程测试

### 测试目的
验证从零开始注册账号并生成邮箱的完整流程

### 测试结果：**失败** ❌

### 详细过程

#### 步骤 1: 注册账号
```
POST /api/auth/register
{
  "username": "nfyu1ai034zj",
  "password": "Test86884251!",
  "turnstileToken": ""
}

响应：400 Bad Request
{
  "error": "请先完成安全验证"
}
```

**问题**: 注册时需要 Turnstile 验证码 token，不能直接跳过

#### Turnstile 验证码说明
根据错误提示和 NextAuth 的认证流程，该站点使用 Cloudflare Turnstile 进行人机验证。

Turnstile token 需要通过以下方式获取：
1. 在浏览器中加载登录页面
2. 执行 Turnstile 挑战（可能是隐式的）
3. 获取 challenge-response token

**纯 HTTP 请求无法完成此验证**

---

## 问题分析

### 1. Turnstile 验证阻挡自动化注册

MoeMail 使用 NextAuth + Cloudflare Turnstile 保护注册接口：

```typescript
// 典型的 NextAuth Turnstile 验证
POST /api/auth/register
{
  username: string,
  password: string,
  turnstileToken: string  // ← 必须有有效的 token
}
```

### 2. 可能的解决方案

#### 方案 A: 使用浏览器自动化 (推荐)
使用 Playwright/Selenium 模拟真实浏览器完成注册：
- 自动通过 Turnstile 验证
- 提取 Cookie 中的 session-token
- 后续 API 调用可复用 session

#### 方案 B: 手动注册，复用账号
1. 手动在 https://sall.cc 注册账号
2. 将账号密码配置到系统中
3. 测试脚本使用已有账号登录

#### 方案 C: 寻找无需验证的替代 API
检查是否有其他邮箱生成服务提供更开放的 API

---

## 下一步行动

### 选项 1: 使用浏览器自动化测试 MoeMail

修改 `base_mailbox.py` 使用 Playwright：

```python
from playwright.sync_api import sync_playwright

def _register_and_login(self):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(f"{self.api}/zh-CN/login")
        
        # 填写注册表单（Turnstile 会自动通过）
        page.fill('input[name="username"]', username)
        page.fill('input[name="password"]', password)
        page.click('button[type="submit"]')
        
        # 等待登录成功
        page.wait_for_selector('.user-avatar')
        
        # 提取 Cookie
        cookies = page.context.cookies()
        session_token = [c['value'] for c in cookies if 'session-token' in c['name']][0]
        
        # 后续可用 requests 继续
```

### 选项 2: 切换到其他邮箱服务

考虑使用不需要验证码的邮箱服务：
- tempmail.lol
- mail.tm
- guerrillamail.com

---

## 结论

当前 `MoeMailMailbox` 实现无法通过纯 HTTP 请求完成注册流程，因为：

1. **注册接口需要 Turnstile 验证码** - 无法绕过
2. **登录接口需要 CSRF + 表单提交** - 可实现但依赖注册成功

**建议**: 
- 如果有现成的 MoeMail 账号，可以测试登录后的邮箱生成功能
- 否则需要引入浏览器自动化来完成注册
- 或考虑切换到其他提供更开放 API 的邮箱服务
